"""
runtime_client/audio_bridge.py
================================
Bridges src/audio/capture.py's synchronous, thread + queue.Queue-based
AudioCapture into an asyncio.Queue of raw PCM16LE bytes, so the
WebSocket transport (asyncio) can consume captured audio without
duplicating any of the sounddevice/InputStream lifecycle logic already
in AudioCapture.

Silence gating (P5-4-1): the pump thread is also this Client's only
send gate. AudioCapture streams 100% of captured mic audio
unconditionally (its rms_threshold param is accepted but never used),
and the Server's non-manual-flush VAD route
(phantom_runtime.py vad_loop's `_route_segment` -> `_enqueue_latest`)
finalizes and transcribes whatever segments its own RMS_THRESHOLD
(default 120, src/config.py) judges to contain speech. On-device
measurement during the P5-4-1 investigation found plain room noise
already sitting above that threshold on at least one real mic, so with
no Client-side gate, silence gets transcribed repeatedly -- Whisper
hallucinates a fixed phrase for near-empty audio, and it recurs every
VAD cycle. Since Server tuning is out of scope, the fix is to never
forward a block whose RMS falls below `silence_rms_threshold` (see
ClientConfig.silence_rms_threshold -- no default is hardcoded in this
module; the threshold is supplied by the caller).

Recording gate (P5-4-2): the same pump thread also enforces the
operator's RECORDING ON/OFF toggle ('r' key / "toggle_recording"
Control Event). That toggle previously only notified the Server (via
keyboard_bridge.py's NotifyingEvent -> Control Event send) -- it never
touched this module, so audio kept being forwarded, transcribed, and
replied to while RECORDING showed OFF. The Server's own non-manual-flush
route (`_route_segment` -> `_enqueue_latest`) has no recording_active
check either (only the manual-flush branch does), and Server changes
are out of scope, so this Client-side pump is the only place able to
enforce the toggle. `recording_active` is the *same* threading.Event
instance keyboard_bridge.py's NotifyingEvent wraps (passed in by the
caller, see main.py) -- not a second mirrored flag -- so there is only
ever one source of truth for recording state and no risk of the two
drifting out of sync.

EXPORTED API:
  resolve_input_device(name) -- thin wrapper over audio.devices.resolve_device_id
  block_rms(block)   -- RMS of one raw PCM16LE block; also reused by
                         the offline measurement harness used to
                         validate the silence gate against recorded
                         audio (see docs/P5-4-1 investigation notes)
  AudioBridge -- owns one AudioCapture instance + the pump thread that
                 feeds its queue.Queue into an asyncio.Queue
"""

import asyncio
import queue
import threading
from typing import Callable, Optional

import numpy as np

from audio.capture import AudioCapture
from audio.devices import resolve_device_id


def resolve_input_device(name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    return resolve_device_id(name)


def block_rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


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
        silence_rms_threshold: int,
        recording_active: threading.Event,
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
        self._silence_rms_threshold = silence_rms_threshold
        self._recording_active = recording_active
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
            if not self._recording_active.is_set():
                continue  # RECORDING OFF -- never forwarded, so the Server
                          # never sees, transcribes, or replies to it
            if block_rms(block) < self._silence_rms_threshold:
                continue  # silence -- never forwarded, so the Server's
                          # VAD/Whisper can't repeatedly hallucinate on it
            data = block.tobytes()
            self._loop.call_soon_threadsafe(self._enqueue, data)

    def _enqueue(self, data: bytes) -> None:
        try:
            self._out_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass  # live stream, not a durable log -- drop under sustained backpressure
        if self._on_block_sent is not None:
            self._on_block_sent()
