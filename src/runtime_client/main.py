"""
runtime_client/main.py
========================
Entrypoint wiring for the Runtime Client.

Phase 1-4 scope: mic capture -> PCM16LE -> WebSocket (Phase 1-2), Control
Events (Phase 1-3), and the full Keyboard UX ported from
src/ui/keyboard.py (KeyboardController/RuntimeContext, reused verbatim)
plus Typed Event parsing/rendering/local mirroring (typed_event.py).
Phase 3 adds client-side TTS playback (tts.py) and virtual/macOS output
device selection (output_device.py) -- 'reply' Typed Events are now
actually spoken, routed to whichever output device (built-in speaker,
BlackHole, a Multi-Output Device, ...) --output-device selects.

EXPORTED API:
  main(argv=None) -- process entrypoint
                     (invoked as: python -m runtime_client [args], or
                     python -m src.runtime_client [args] from the repo
                     root, matching phantom_runtime.py's convention)
"""

import asyncio
import os
import signal
import sys
from typing import Optional

# Mirrors phantom_runtime.py's own _SCRIPT_DIR_EARLY bootstrap: makes
# `audio.*` / `ui.*` / `runtime.*` importable (bare, not `src.audio.*`)
# regardless of whether this is launched as `python -m runtime_client`
# from inside src/, or `python -m src.runtime_client` from the repo root.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.audio_bridge import AudioBridge, resolve_input_device
from runtime_client.config import ClientConfig, build_ws_url, parse_args
from runtime_client.keyboard_bridge import build_keyboard_thread
from runtime_client.output_device import print_output_devices, resolve_output_device_id
from runtime_client.tts import build_tts_provider
from runtime_client.typed_event import TypedEventStore, show_info, show_warn
from runtime_client.websocket_client import RuntimeWebSocketClient


def _print(message: str) -> None:
    print(message, flush=True)


def _list_input_devices() -> None:
    import sounddevice as sd

    try:
        devices = sd.query_devices()
    except Exception as exc:
        _print(f"[runtime_client] could not query input devices: {exc}")
        return
    for dev in devices:
        if dev["max_input_channels"] > 0:
            _print(f"  [{dev['index']}] {dev['name']}")


async def _bridge_keyboard_shutdown(kb_shutdown, stop_event: asyncio.Event) -> None:
    """Poll the keyboard thread's threading.Event and mirror it onto the
    asyncio stop_event ('q' key should shut down the whole client, not
    just the keyboard thread)."""
    while not stop_event.is_set():
        if kb_shutdown.is_set():
            stop_event.set()
            return
        await asyncio.sleep(0.1)


async def _amain(config: ClientConfig) -> None:
    import threading

    loop = asyncio.get_running_loop()
    audio_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=100)
    control_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=100)
    stop_event = asyncio.Event()
    kb_shutdown = threading.Event()

    def _handle_stop_signal() -> None:
        _print("\n[runtime_client] shutdown signal received, closing...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_stop_signal)

    device_id = resolve_input_device(config.input_device)
    if config.input_device and device_id is None:
        show_warn(
            f"input device '{config.input_device}' not found "
            "-- using system default. Available devices:"
        )
        _list_input_devices()

    output_device_id = resolve_output_device_id(config.output_device)
    if config.output_device and output_device_id is None:
        show_warn(
            f"output device '{config.output_device}' not found "
            "-- using system default. Available devices:"
        )
        print_output_devices(_print)

    tts_interrupt_event = threading.Event()
    tts = build_tts_provider(
        config.tts,
        voice=config.voice,
        rate=config.rate,
        volume=config.volume,
        device_id=output_device_id,
        on_warn=show_warn,
    )

    store = TypedEventStore(tts=tts, tts_interrupt_event=tts_interrupt_event)

    bridge = AudioBridge(
        sample_rate=config.sample_rate,
        channels=config.channels,
        block_size=config.block_size,
        device_id=device_id,
        loop=loop,
        out_queue=audio_queue,
        on_status=lambda msg: show_info(f"[audio] {msg}"),
        on_block_sent=lambda: setattr(store, "audio_blocks_sent", store.audio_blocks_sent + 1),
    )
    bridge.start()

    kb_thread = build_keyboard_thread(config, store, loop, control_queue, kb_shutdown)
    kb_thread.start()

    watcher = asyncio.ensure_future(_bridge_keyboard_shutdown(kb_shutdown, stop_event))

    ws_url = build_ws_url(config.url, config.provider)
    client = RuntimeWebSocketClient(
        url=ws_url,
        max_reconnect_attempts=config.max_reconnect_attempts,
        backoff_base_seconds=config.backoff_base_seconds,
    )

    try:
        await client.run(audio_queue, control_queue, stop_event, store.handle_line)
    finally:
        watcher.cancel()
        bridge.stop()
        tts.stop()


def main(argv: Optional[list] = None) -> None:
    config = parse_args(argv)

    if config.list_devices:
        _print("[runtime_client] available input devices:")
        _list_input_devices()
        return

    if config.list_output_devices:
        _print("[runtime_client] available output devices:")
        print_output_devices(_print)
        return

    ws_url = build_ws_url(config.url, config.provider)
    tts_line = (
        f"  tts:          off\n" if config.tts == "none" else
        f"  tts:          {config.tts}  voice={config.voice}  "
        f"rate={config.rate or '(default)'}  volume={config.volume}\n"
        f"  output device: {config.output_device or '(system default)'}\n"
    )
    _print(
        f"[runtime_client] Phantom Runtime Client\n"
        f"  target:       {ws_url}\n"
        f"  input device: {config.input_device or '(system default)'}\n"
        f"  sample rate:  {config.sample_rate} Hz, {config.channels}ch, "
        f"block={config.block_size} frames\n"
        f"{tts_line}"
        f"  (Ctrl+C or 'q' to quit)\n"
    )

    asyncio.run(_amain(config))


if __name__ == "__main__":
    main(sys.argv[1:])
