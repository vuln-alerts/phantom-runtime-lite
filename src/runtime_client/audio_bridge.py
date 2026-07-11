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

Adaptive Speech Gate (P5-4 Phase 5 Integration, design doc section 5/
section 10.1, Implementation Plan section 1.2's audio_bridge.py row):
the same pump thread's silence check now prefers a live
CalibrationResult.speech_gate -- read every time a block is evaluated,
never cached -- over the fixed `silence_rms_threshold`, when the caller
supplies a `calibration_controller` (calibration.py's
RecalibrationController, Phase 4, unmodified). This module does not
import calibration.py at runtime (would create a circular import, since
calibration.py imports block_rms from here) and does not derive a
Speech Gate itself -- it only reads the number CalibrationEngine already
computed (design doc section 5's "Gate導出ロジックは禁止... CalibrationEngine
の結果のみ利用"). `calibration_controller` defaults to None, which
preserves the exact prior behavior (the fixed `silence_rms_threshold`)
byte-for-byte -- every pre-Phase-5 constructor call site and test is
unaffected.

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
import os
import queue
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

import numpy as np

from audio.capture import AudioCapture
from audio.devices import resolve_device_id
from runtime_client import debug_sink
import runtime_trace

if TYPE_CHECKING:
    # Type-checking only -- see module docstring's "Adaptive Speech Gate"
    # note on why this is not a runtime import.
    from runtime_client.calibration import RecalibrationController


def resolve_input_device(name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    return resolve_device_id(name)


def block_rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


# ---------------------------------------------------------------------------
# Production Verification investigation instrumentation (TEMPORARY).
#
# Added to make it observable, during Production Verification, whether a
# "no Transcript" symptom is caused by captured audio never clearing the
# Speech Gate in _run_pump below. Diagnostic only: it reads the same rms/
# gate values _run_pump already computed for its (unmodified) gating
# decision and only ever prints/tees them -- it does not influence which
# blocks get forwarded. Inert unless PHANTOM_CALIBRATION_DEBUG=1 (the same
# flag --production-verification already turns on, see main.py), so
# default behavior is unchanged. Mirrors calibration.py's _debug_log /
# debug_sink.py convention so every line is grep-able via `[audio-debug]`
# and tees to the same Production Verification session log file when one
# is open.
# ---------------------------------------------------------------------------
_AUDIO_DEBUG_HEARTBEAT_SEC = 1.0


def _audio_debug_print(text: str) -> None:
    print(text, flush=True)
    debug_sink.write(text)


# ---------------------------------------------------------------------------
# Production Verification investigation instrumentation (TEMPORARY).
#
# PHANTOM_DISABLE_SPEECH_GATE=1 bypasses the `if rms < gate: continue` check
# in _run_pump below entirely, forwarding every block that already cleared
# the Recording Gate. Purpose: isolate where the Mic -> Calibration ->
# Speech Gate -> AudioBridge -> WebSocket -> Cloud Run -> VAD -> Whisper ->
# Transcript pipeline is failing during Production Verification. If
# Transcript still doesn't appear with the gate disabled, the Speech Gate
# was not the cause -- look downstream of AudioBridge instead. Diagnostic
# only: does not touch Calibration, does not change how the Speech Gate
# itself is derived, and does not change AudioBridge's public API. Default
# (unset) preserves _run_pump()'s exact pre-existing behavior byte-for-byte.
# ---------------------------------------------------------------------------
def _speech_gate_disabled() -> bool:
    return os.getenv("PHANTOM_DISABLE_SPEECH_GATE") == "1"


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
        calibration_controller: Optional["RecalibrationController"] = None,
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
        self._calibration_controller = calibration_controller
        self._shutdown = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._pump_thread: Optional[threading.Thread] = None
        self._debug_is_speech: Optional[bool] = None
        self._debug_last_heartbeat: float = 0.0
        self._debug_gate_disabled_announced: bool = False

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

    def _current_speech_gate(self) -> float:
        """P5-4 Phase 5: the RMS threshold this block is checked against.
        Live read (not cached) of calibration_controller.active_result.speech_gate
        when a controller was supplied, per design doc section 5's
        "AudioBridge は active_result.speech_gate を見るだけ" -- falls back
        to the fixed silence_rms_threshold both when no controller was
        supplied (pre-Phase-5 behavior) and in the defensive case where
        active_result.speech_gate is None (a calibration that reported
        failure without a caller-supplied Fallback value; see
        calibration.py's CalibrationResult docstring)."""
        if self._calibration_controller is None:
            return self._silence_rms_threshold
        gate = self._calibration_controller.active_result.speech_gate
        return gate if gate is not None else self._silence_rms_threshold

    def _run_pump(self) -> None:
        while not self._shutdown.is_set():
            try:
                block = self._raw_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            _trace_id = runtime_trace.next_event_id("blk") if runtime_trace.enabled() else ""
            if runtime_trace.enabled():
                runtime_trace.emit("Audio Received", event_id=_trace_id)
            if not self._recording_active.is_set():
                continue  # RECORDING OFF -- never forwarded, so the Server
                          # never sees, transcribes, or replies to it
            rms = block_rms(block)
            if _speech_gate_disabled():
                # Production Verification investigation instrumentation
                # (TEMPORARY) -- see _speech_gate_disabled()'s module-level
                # note. Every block that reaches this branch is forwarded
                # unconditionally; the Speech Gate is not consulted at all.
                if debug_sink.is_enabled():
                    self._log_speech_gate_disabled_debug(rms)
            else:
                gate = self._current_speech_gate()
                if debug_sink.is_enabled():
                    self._log_speech_gate_debug(rms, gate)
                if rms < gate:
                    continue  # silence -- never forwarded, so the Server's
                              # VAD/Whisper can't repeatedly hallucinate on it
            if runtime_trace.enabled():
                runtime_trace.emit("Speech Gate PASS", event_id=_trace_id, rms=rms)
            data = block.tobytes()
            self._loop.call_soon_threadsafe(self._enqueue, data)

    def _log_speech_gate_debug(self, rms: float, gate: float) -> None:
        """Production Verification instrumentation (TEMPORARY) -- see the
        module-level comment above _audio_debug_print(). Diagnostic only:
        prints/tees the rms/gate values _run_pump already computed: an
        edge-triggered Speech START/END line on state change, plus a
        throttled (~1/sec) state heartbeat. Does not affect gating."""
        is_speech = rms >= gate
        if self._debug_is_speech is None:
            self._debug_is_speech = is_speech  # establish baseline, no transition log
        elif is_speech != self._debug_is_speech:
            state = "START" if is_speech else "END"
            _audio_debug_print(f"[audio-debug] Speech {state} (RMS={rms:.0f} Gate={gate:.0f})")
            self._debug_is_speech = is_speech

        now = time.monotonic()
        if now - self._debug_last_heartbeat >= _AUDIO_DEBUG_HEARTBEAT_SEC:
            self._debug_last_heartbeat = now
            _audio_debug_print(
                f"[audio-debug]\nRMS={rms:.0f}\nGate={gate:.0f}\n"
                f"Speech={'YES' if is_speech else 'NO'}"
            )

    def _log_speech_gate_disabled_debug(self, rms: float) -> None:
        """Production Verification instrumentation (TEMPORARY) -- see
        _speech_gate_disabled()'s module-level comment. Diagnostic only:
        announces the bypass once, then prints a throttled (~1/sec)
        heartbeat confirming every block is being forwarded. Does not
        affect gating -- the caller has already skipped it; this only
        reports that fact."""
        if not self._debug_gate_disabled_announced:
            _audio_debug_print("[audio-debug] Speech Gate: DISABLED")
            self._debug_gate_disabled_announced = True

        now = time.monotonic()
        if now - self._debug_last_heartbeat >= _AUDIO_DEBUG_HEARTBEAT_SEC:
            self._debug_last_heartbeat = now
            _audio_debug_print(
                f"[audio-debug]\nRMS={rms:.0f}\nGate=DISABLED\nForward=YES"
            )

    def _enqueue(self, data: bytes) -> None:
        try:
            self._out_queue.put_nowait(data)
        except asyncio.QueueFull:
            pass  # live stream, not a durable log -- drop under sustained backpressure
        if self._on_block_sent is not None:
            self._on_block_sent()
