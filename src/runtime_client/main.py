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

P5-4 Adaptive Runtime Calibration, Phase 5 (Integration) addition (see
docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md section 5.1 and
docs/designs/IMPLEMENTATION_PLAN_P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md
section 1.2/2's Phase 5 row): before the real AudioBridge is built,
_perform_startup_calibration() runs a short-lived, dedicated AudioCapture
through Phase 1's EnvironmentObserver / Phase 2's CalibrationEngine
(both reused verbatim, no new sampling/derivation logic), rendering
Phase 3's four calibration screens as it goes. The resulting
CalibrationResult seeds a Phase 4 RecalibrationController (also reused
verbatim), which is then handed to AudioBridge so its send gate reads
CalibrationResult.speech_gate live instead of the fixed
silence_rms_threshold (see audio_bridge.py's "Adaptive Speech Gate" note).

Deliberately NOT wired in this pass (do not infer these exist):
  - FR-7 manual re-calibration ('c' key or any other key). Design doc
    section 6.4's stated key ('c') already has an unrelated, existing
    binding in src/ui/keyboard.py ("clear transcript log + buffer"),
    and this task's own target-file list excludes ui/keyboard.py --
    wiring FR-7 would require either changing that existing binding's
    behavior or touching a file outside this pass's scope. Flagged to
    the operator and explicitly deferred, not silently dropped.
    RecalibrationController.request_manual_recalibration() exists
    (Phase 4) and is fully usable by a future pass; nothing here calls it.
  - FR-6 automatic drift detection / reject-rate comparison -- no
    concrete threshold or formula is specified anywhere in either
    design doc (see calibration.py's RecalibrationController docstring);
    inventing one is out of scope.
  - show_environment_changed() (design doc section 8.5) is therefore
    never called from this module: its only two possible triggers
    (FR-7 manual, FR-6 automatic) are both out of scope above, and its
    reject-rate arguments cannot be supplied without FR-6's judgment
    logic, which is explicitly forbidden.

EXPORTED API:
  main(argv=None) -- process entrypoint
                     (invoked as: python -m runtime_client [args], or
                     python -m src.runtime_client [args] from the repo
                     root, matching phantom_runtime.py's convention)
"""

import asyncio
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Optional

# Mirrors phantom_runtime.py's own _SCRIPT_DIR_EARLY bootstrap: makes
# `audio.*` / `ui.*` / `runtime.*` importable (bare, not `src.audio.*`)
# regardless of whether this is launched as `python -m runtime_client`
# from inside src/, or `python -m src.runtime_client` from the repo root.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from audio.capture import AudioCapture

from runtime_client.audio_bridge import AudioBridge, block_rms, resolve_input_device
from runtime_client.calibration import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_NOISE_FLOOR_SAFETY_FLOOR,
    DEFAULT_PERCENTILE,
    DEFAULT_SPEECH_GATE_MULTIPLIER,
    DEFAULT_WINDOW_BLOCKS,
    CalibrationEngine,
    CalibrationResult,
    EnvironmentObserver,
    NoiseFloorSampler,
    ObservationResult,
    RecalibrationController,
)
from runtime_client.config import ClientConfig, build_ws_url, parse_args
from runtime_client import debug_sink
from runtime_client.keyboard_bridge import build_keyboard_thread
from runtime_client.output_device import print_output_devices, resolve_output_device_id
from runtime_client.tts import build_tts_provider
from runtime_client.typed_event import (
    TypedEventStore,
    show_calibration_complete,
    show_calibration_failed,
    show_calibration_progress,
    show_calibration_start,
    show_info,
    show_warn,
)
from runtime_client.websocket_client import RuntimeWebSocketClient


def _print(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Production Verification investigation instrumentation (TEMPORARY).
#
# Added to trace why Adaptive Runtime Calibration's Startup Calibration is
# reportedly always Calibration Failed on Production Verification while all
# 429 unit tests pass -- see investigation notes. Not part of the
# calibration algorithm: inert unless PHANTOM_CALIBRATION_DEBUG=1 is set, so
# default behavior is unchanged. Mirrors calibration.py's own _debug_log
# (same env var, same "single helper, single call-site pattern per file" so
# every call site is findable via `grep -rn PHANTOM_CALIBRATION_DEBUG` and
# deletable together with that function once the root cause is found) --
# routed through show_info() here rather than a bare print(), matching this
# module's existing Runtime UI display convention. Also tees to
# debug_sink's session log file (Production Verification Support:
# --production-verification below) when one is open -- stdout
# output/gating is otherwise identical to before that addition.
# ---------------------------------------------------------------------------
def _calibration_debug_log(message: str) -> None:
    if os.getenv("PHANTOM_CALIBRATION_DEBUG") == "1":
        line = f"[calibration-debug] {message}"
        show_info(line)
        debug_sink.write(line)


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


def _build_fallback_calibration_result(
    fallback_gate: float, sample_count: int, attempts: int
) -> CalibrationResult:
    """
    P5-4 Phase 5, design doc section 9.1: CalibrationEngine deliberately
    leaves speech_gate=None when the underlying observation failed (see
    its own docstring -- Fallback policy is explicitly out of scope for
    Phase 2). AudioBridge needs a real number to gate on regardless, so
    this is the one place Phase 5 supplies it -- reusing
    ClientConfig.silence_rms_threshold (DEFAULT_SILENCE_RMS_THRESHOLD,
    config.py) in the Fallback role the Implementation Plan's config.py
    row already assigns it, not a newly invented value or formula.
    success stays False so show_calibration_failed() (already shown by
    the caller before this is built) and any future consumer keep
    treating this as an estimate, never a measured value (design doc
    section 9.1 / AC-10).
    """
    return CalibrationResult(
        success=False,
        noise_floor=None,
        speech_gate=fallback_gate,
        sample_count=sample_count,
        attempts=attempts,
    )


def _write_root_cause_summary(
    observation,
    result: CalibrationResult,
    is_fallback: bool,
    resolved_device_name: str,
) -> None:
    """
    Production Verification Support (TEMPORARY, --production-verification
    Task3): a plain-text, at-a-glance companion to the
    calibration_<timestamp>.log session log -- written once, right after
    Startup Calibration finishes (success or Calibration Failed/
    Fallback). Purely descriptive: reports what ObservationResult/
    CalibrationResult already computed; computes nothing itself and
    never guesses a root cause -- Candidate Cause is always "Unknown"
    (per investigation policy: root cause must not be asserted without
    reviewing the accompanying session log's per-block/per-attempt
    evidence).
    """
    lines = [
        "Production Verification - Root Cause Summary",
        f"generated_at: {datetime.now().isoformat()}",
        f"session_log: {debug_sink.session_log_path() or '(none)'}",
        "",
        f"Attempts: {result.attempts}",
        f"ObservationResult: {observation!r}",
        f"NoiseFloor: {result.noise_floor}",
        f"SpeechGate: {result.speech_gate}",
        f"Fallback: {is_fallback}",
        f"Calibration: {'SUCCESS' if result.success else 'FAILED'}",
        "Resolved Input Device: "
        + (resolved_device_name or "(unknown -- empty when the input device "
           "wasn't explicitly resolved; see AudioCapture.run())"),
        "Candidate Cause: Unknown (root cause not yet determined -- do not "
        "infer from this summary; review the accompanying "
        "calibration_<timestamp>.log for per-block/per-attempt evidence)",
    ]
    debug_sink.write_file(
        os.path.join("logs", "root_cause_summary.txt"), "\n".join(lines) + "\n"
    )


def _run_initial_calibration(
    observer: EnvironmentObserver,
    block_queue: "queue.Queue",
    shutdown: threading.Event,
    window_seconds: float,
    window_blocks: int,
    poll_timeout: float = 0.2,
) -> None:
    """
    P5-4 Phase 5, design doc section 5.1 steps 3-5 / section 8.1-8.2:
    drains raw PCM16LE blocks from block_queue into observer
    (EnvironmentObserver, Phase 1, unmodified) until it finishes or
    shutdown is set, rendering Phase 3's startup/in-progress screens as
    it goes. All sampling/contamination/retry/percentile decisions are
    observer.add_block()'s alone -- this function only drains a queue
    and renders what EnvironmentObserver already computed (is_finished,
    attempt), the same "feed synthetic blocks, assert on the resulting
    state" shape as tests/test_runtime_client_calibration.py already
    uses, so it can be driven by a synthetic queue in tests without
    real sounddevice (see tests/test_runtime_client_main.py).

    noise_floor_estimate is intentionally left at show_calibration_progress's
    own None default: computing a provisional p90 would require reading
    NoiseFloorSampler's private sample list, which calibration.py does
    not expose and which this pass must not modify to add.
    """
    show_calibration_start(window_blocks)
    _calibration_debug_log(
        f"_run_initial_calibration start: window_seconds={window_seconds:.2f} "
        f"window_blocks={window_blocks}"
    )
    start = time.monotonic()
    last_attempt = observer.attempt
    sample_count = 0
    first_block_seen = False
    while not observer.is_finished and not shutdown.is_set():
        try:
            block = block_queue.get(timeout=poll_timeout)
        except queue.Empty:
            continue

        if not first_block_seen:
            first_block_seen = True
            _calibration_debug_log(
                "First Block: "
                f"shape={block.shape} dtype={block.dtype} "
                f"min={block.min()} max={block.max()} mean={block.mean():.2f} "
                f"RMS={block_rms(block):.1f}"
            )

        observer.add_block(block)

        if observer.attempt != last_attempt:
            # A contaminated window just finished and a fresh attempt
            # started (design doc section 6.2's retry) -- the fresh
            # NoiseFloorSampler has 0 samples, so the progress display
            # restarts from 0/window_blocks and 0.0s, matching it.
            _calibration_debug_log(
                f"_run_initial_calibration: attempt {last_attempt} -> {observer.attempt} "
                f"(elapsed={time.monotonic() - start:.2f}s, sample_count was {sample_count})"
            )
            last_attempt = observer.attempt
            start = time.monotonic()
            sample_count = 0
        else:
            sample_count += 1

        if observer.is_finished:
            break

        show_calibration_progress(
            sample_count=sample_count,
            window_blocks=window_blocks,
            elapsed_seconds=time.monotonic() - start,
            window_seconds=window_seconds,
        )

    _calibration_debug_log(
        f"_run_initial_calibration exit: observer.is_finished={observer.is_finished} "
        f"shutdown.is_set()={shutdown.is_set()} final_attempt={observer.attempt} "
        f"result={observer.result()}"
    )


def _run_baseline_observation(
    block_queue: "queue.Queue",
    shutdown: threading.Event,
    poll_timeout: float = 0.2,
) -> Optional[NoiseFloorSampler]:
    """
    Production Blocker Fix (P5-4): a single, unconditional (no
    contamination gating -- contamination_threshold=+inf) sample of the
    *current* environment, run once before the real (gated, retryable)
    EnvironmentObserver in _perform_startup_calibration() below.

    Reuses NoiseFloorSampler (calibration.py, unmodified) -- same
    percentile statistic (DEFAULT_PERCENTILE=90) as the real
    observation -- with a shorter window_blocks than
    DEFAULT_WINDOW_BLOCKS (25) so this baseline pass does not add a
    full extra observation window's worth of startup latency on top of
    Startup Calibration's own window. Mirrors the precedent already set
    by RecalibrationController's DEFAULT_RECALIBRATION_WINDOW_BLOCKS
    (15, a shorter window than 25 for a different re-use of this same
    class): a smaller window_blocks is an existing, supported
    constructor argument of NoiseFloorSampler, not a new algorithm.
    Still a percentile over multiple samples (not a single instantaneous
    reading), still resistant to any one loud block dominating the
    result (design doc section 10.2's rationale for p90 over max). No
    new sampling/statistics logic is introduced; only
    contamination_threshold is overridden to +inf, so no sample can
    ever mark this particular window "contaminated".

    Returns None (not a partial/best-effort sampler) if shutdown fires
    before the window fills -- caller falls back to the unmodified
    fixed DEFAULT_NOISE_FLOOR_SAFETY_FLOOR default in that case,
    exactly today's pre-fix behavior.
    """
    sampler = NoiseFloorSampler(window_blocks=10, contamination_threshold=float("inf"))
    while not sampler.is_complete:
        if shutdown.is_set():
            return None
        try:
            block = block_queue.get(timeout=poll_timeout)
        except queue.Empty:
            continue
        sampler.add_block(block)
    return sampler


def _derive_dynamic_contamination_threshold(
    baseline: Optional[NoiseFloorSampler], engine: CalibrationEngine
) -> float:
    """
    Temporary reuse of CalibrationEngine's Speech Gate derivation for
    Startup Calibration only.

    Startup Calibration needs an adaptive Contamination Threshold
    derived from the environment actually observed at this launch
    (Runtime Philosophy: observe the environment, then derive Runtime
    parameters from that observation -- not from a value fixed at
    design time). CalibrationEngine.calibrate() (Phase 2, unmodified)
    already implements exactly this kind of derivation --
    clamp(noise_floor * 3.0, 150, 2500) -- so this reuses it verbatim
    on _run_baseline_observation()'s result rather than inventing a
    second formula.

    This is intentionally scoped to Startup Calibration only.
    RecalibrationController (Phase 4, unmodified) is not touched by
    this function and keeps using its own fixed
    DEFAULT_NOISE_FLOOR_SAFETY_FLOOR default for re-calibration cycles.
    Should a future phase want the same adaptive-threshold treatment
    for Recalibration, this function is the extraction point -- it
    lives here, next to its only caller, precisely because that
    extension is not part of this fix.

    Falls back to DEFAULT_NOISE_FLOOR_SAFETY_FLOOR -- today's pre-fix
    fixed value -- if baseline is None (shutdown interrupted the
    baseline pass) or its window never completed, so the worst case is
    unchanged (pre-fix) behavior, never worse.
    """
    if baseline is None or not baseline.is_complete:
        return DEFAULT_NOISE_FLOOR_SAFETY_FLOOR
    baseline_noise_floor = baseline.noise_floor()
    if baseline_noise_floor is None:
        return DEFAULT_NOISE_FLOOR_SAFETY_FLOOR
    baseline_result = engine.calibrate(
        ObservationResult(
            success=True,
            noise_floor=baseline_noise_floor,
            sample_count=baseline.sample_count,
            attempts=1,
        )
    )
    return baseline_result.speech_gate


def _perform_startup_calibration(
    config: ClientConfig, device_id: Optional[int]
) -> "tuple[CalibrationEngine, CalibrationResult, str]":
    """
    P5-4 Phase 5, design doc section 5.1 steps 2-6: runs the blocking
    initial calibration through a short-lived AudioCapture dedicated to
    this phase -- separate from the AudioBridge instance the caller
    builds afterward for normal operation, mirroring design doc section
    5.2's sequence diagram where "EO->>Mic" samples directly, ahead of
    (and independent from) the Silence/Recording Gate pump. Reuses
    EnvironmentObserver/CalibrationEngine (Phase 1/2) and the four
    Phase 3 screens verbatim; introduces no new sampling, contamination,
    percentile, or Speech Gate derivation logic -- only supplies the
    Implementation Plan's already-planned config.py Fallback value
    (see _build_fallback_calibration_result) when calibration doesn't
    succeed, per design doc section 9.1/9.2/9.4 (contamination
    exhaustion, mic open failure, and timeout all collapse to the same
    "observer never finished successfully" case here).

    Returns (engine, initial_result, resolved_device_name) for the
    caller to seed a RecalibrationController and pass to AudioBridge.
    """
    block_seconds = config.block_size / config.sample_rate
    window_seconds = block_seconds * DEFAULT_WINDOW_BLOCKS

    cal_queue: "queue.Queue" = queue.Queue(maxsize=100)
    cal_shutdown = threading.Event()
    capture = AudioCapture(
        sample_rate=config.sample_rate,
        channels=config.channels,
        dtype="int16",
        block_size=config.block_size,
        rms_threshold=0,
        audio_queue=cal_queue,
        device_id=device_id,
        device_name=config.input_device or "",
        on_status=lambda msg: show_info(f"[audio] {msg}"),
        on_overflow=lambda count, rate: show_info(
            f"[audio] calibration queue overflow: dropped {count} block(s) ({rate:.1f}/min)"
        ),
        on_info=lambda msg: show_info(f"[audio] {msg}"),
    )

    def _run_capture() -> None:
        try:
            capture.run(cal_shutdown)
        except RuntimeError as exc:
            # design doc section 9.2: device open failure -- surface it
            # via the existing on_status path and fall through to the
            # Fallback branch below rather than hanging.
            show_warn(f"audio capture failed during calibration: {exc}")
            cal_shutdown.set()

    capture_thread = threading.Thread(
        target=_run_capture, name="calibration-capture", daemon=True
    )
    capture_thread.start()

    engine = CalibrationEngine()

    # Temporary reuse of CalibrationEngine's Speech Gate derivation for
    # Startup Calibration only -- see _derive_dynamic_contamination_threshold()
    # docstring. Not applied to Recalibration; RecalibrationController
    # (unmodified) keeps its own fixed default.
    baseline = _run_baseline_observation(cal_queue, cal_shutdown)
    dynamic_threshold = _derive_dynamic_contamination_threshold(baseline, engine)
    _calibration_debug_log(
        f"Dynamic Contamination Threshold: {dynamic_threshold:.1f} RMS "
        f"(baseline_sample_count={baseline.sample_count if baseline else 0})"
    )

    observer = EnvironmentObserver(contamination_threshold=dynamic_threshold)
    try:
        _run_initial_calibration(
            observer,
            cal_queue,
            cal_shutdown,
            window_seconds=window_seconds,
            window_blocks=DEFAULT_WINDOW_BLOCKS,
        )
    finally:
        cal_shutdown.set()
        capture_thread.join(timeout=2.0)

    observation = observer.result()
    result = engine.calibrate(observation) if observation is not None else None

    if result is not None and result.success:
        show_calibration_complete(
            noise_floor=result.noise_floor,
            speech_gate=result.speech_gate,
            sample_count=result.sample_count,
            percentile=DEFAULT_PERCENTILE,
            multiplier=DEFAULT_SPEECH_GATE_MULTIPLIER,
            microphone_name=capture.resolved_device_name,
        )
        if config.production_verification:
            _write_root_cause_summary(
                observation, result, is_fallback=False,
                resolved_device_name=capture.resolved_device_name,
            )
        return engine, result, capture.resolved_device_name

    fallback_gate = float(config.silence_rms_threshold)
    attempts = result.attempts if result is not None else 0
    show_calibration_failed(
        attempts=attempts,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        fallback_gate=fallback_gate,
    )
    fallback_result = _build_fallback_calibration_result(
        fallback_gate=fallback_gate,
        sample_count=result.sample_count if result is not None else 0,
        attempts=attempts,
    )
    if config.production_verification:
        _write_root_cause_summary(
            observation, fallback_result, is_fallback=True,
            resolved_device_name=capture.resolved_device_name,
        )
    return engine, fallback_result, capture.resolved_device_name


async def _amain(config: ClientConfig) -> None:
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

    # P5-4 Adaptive Runtime Calibration, Phase 5 (Integration): blocking
    # initial calibration (design doc section 5.1 steps 2-6) runs before
    # normal operation begins, ahead of AudioBridge/keyboard/WS setup --
    # this is the design's one deliberate "make the user wait" phase
    # (NFR-1: <=3s in the common case). Runs synchronously on this
    # coroutine's own thread (nothing else -- no AudioBridge, no
    # keyboard thread, no WebSocket connection -- has started yet, so
    # briefly blocking the event loop here has no other task to starve).
    engine, initial_calibration, _ = _perform_startup_calibration(config, device_id)
    calibration_controller = RecalibrationController(engine, initial_calibration)

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

    # Built (not started) before AudioBridge so the same recording_active
    # Event the 'r' key toggles can be handed to AudioBridge's send gate
    # (P5-4-2) -- one source of truth, no duplicated/mirrored flag.
    kb_thread, recording_active = build_keyboard_thread(
        config, store, loop, control_queue, kb_shutdown
    )

    bridge = AudioBridge(
        sample_rate=config.sample_rate,
        channels=config.channels,
        block_size=config.block_size,
        device_id=device_id,
        loop=loop,
        out_queue=audio_queue,
        on_status=lambda msg: show_info(f"[audio] {msg}"),
        silence_rms_threshold=config.silence_rms_threshold,
        recording_active=recording_active,
        on_block_sent=lambda: setattr(store, "audio_blocks_sent", store.audio_blocks_sent + 1),
        calibration_controller=calibration_controller,
    )
    bridge.start()
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

    # Production Verification Support (TEMPORARY -- see debug_sink.py's
    # module docstring): --production-verification is the one-flag
    # equivalent of setting PHANTOM_CALIBRATION_DEBUG=1 by hand, plus
    # tee-ing that debug output to a timestamped session log file so a
    # human running Production Verification doesn't have to capture/
    # redirect stdout themselves. Does not change Calibration behavior --
    # only where its existing debug lines (calibration.py's _debug_log,
    # this module's _calibration_debug_log, both unmodified in what they
    # compute) get written to.
    if config.production_verification:
        os.environ["PHANTOM_CALIBRATION_DEBUG"] = "1"
        log_path = os.path.join(
            "logs", f"calibration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        debug_sink.open_session_log(log_path)
        _print(f"[runtime_client] Production Verification mode: debug log -> {log_path}")

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
