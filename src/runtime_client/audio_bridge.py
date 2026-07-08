"""
runtime_client/audio_bridge.py
================================
Bridges src/audio/capture.py's synchronous, thread + queue.Queue-based
AudioCapture into an asyncio.Queue of raw PCM16LE bytes, so the
WebSocket transport (asyncio) can consume captured audio without
duplicating any of the sounddevice/InputStream lifecycle logic already
in AudioCapture.

EXPORTED API:
  resolve_input_device(name) -- thin wrapper over audio.devices.resolve_device_id
  AudioBridge -- owns one AudioCapture instance + the pump thread that
                 feeds its queue.Queue into an asyncio.Queue
"""

import asyncio
import queue
import threading
from typing import Callable, Optional

from audio.capture import AudioCapture
from audio.devices import resolve_device_id


def resolve_input_device(name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    return resolve_device_id(name)


class AudioBridge:
    """
    Owns a background thread running AudioCapture.run(), and a second
    pump thread draining AudioCapture's queue.Queue into an asyncio.Queue
    of raw PCM16LE bytes via loop.call_soon_threadsafe -- the same
    thread-to-asyncio handoff idiom runtime.transport_gateway already
    uses for its own pipe-reader thread.
    """

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        block_size: int,
        device_id: Optional[int],
        loop: asyncio.AbstractEventLoop,
        out_queue: "asyncio.Queue[bytes]",
        on_status: Callable[[str], None],
        on_block_sent: Optional[Callable[[], None]] = None,
    ) -> None:
        self._raw_queue: "queue.Queue" = queue.Queue(maxsize=100)
        self._capture = AudioCapture(
            sample_rate=sample_rate,
            channels=channels,
            dtype="int16",
            block_size=block_size,
            rms_threshold=0,
            audio_queue=self._raw_queue,
            device_id=device_id,
            on_status=on_status,
            on_overflow=lambda count, rate: on_status(
                f"audio queue overflow: dropped {count} block(s) ({rate:.1f}/min)"
            ),
            on_info=on_status,
        )
        self._loop = loop
        self._out_queue = out_queue
        self._on_status = on_status
        self._on_block_sent = on_block_sent
        self._shutdown = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._pump_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._capture_thread = threading.Thread(
            target=self._run_capture, name="audio-capture", daemon=True
        )
        self._pump_thread = threading.Thread(
            target=self._run_pump, name="audio-pump", daemon=True
        )
        self._capture_thread.start()
        self._pump_thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=2.0)

    def _run_capture(self) -> None:
        try:
            self._capture.run(self._shutdown)
        except RuntimeError as exc:
            self._on_status(f"audio capture failed: {exc}")
            self._shutdown.set()

    def _run_pump(self) -> None:
        while not self._shutdown.is_set():
            try:
                block = self._raw_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            data = block.tobytes()
            self._loop.call_soon_threadsafe(self._enqueue, data)

    def _enqueue(self, data: bytes) -> None:
        try:
            self._out_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass  # live stream, not a durable log -- drop under sustained backpressure
        if self._on_block_sent is not None:
            self._on_block_sent()
