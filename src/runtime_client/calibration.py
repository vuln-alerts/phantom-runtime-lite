"""
runtime_client/calibration.py
================================
Environment Observation (P5-4 Adaptive Runtime Calibration, Phase 1 --
see docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md, section 6.2, and
docs/designs/IMPLEMENTATION_PLAN_P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md,
section 3.1).

Phase 1 scope: measuring the acoustic Noise Floor of the current
environment from a short window of captured audio blocks, detecting
contamination (speech leaking into what was supposed to be a silent
measurement window) and retrying, and reducing a clean window down to
a single Noise Floor value via its 90th percentile.

Phase 2 scope (this addition): deriving the Speech Gate from the Noise
Floor that Phase 1's EnvironmentObserver measured (design doc section
6.3's clamp(noise_floor * 3.0, min=150, max=2500) formula), and
reporting the outcome as a CalibrationResult. CalibrationEngine takes
an ObservationResult -- EnvironmentObserver's own output -- as its
only input; it does not sample audio itself.

Phase 4 scope (this addition -- see design doc section 6.4, section
5.2's sequence diagram, and Implementation Plan section 3.1's
RecalibrationController-shaped responsibilities): RecalibrationController,
which reuses EnvironmentObserver and CalibrationEngine exactly as Phase
1/2 built them -- no new sampling, contamination, percentile, or gate-
derivation logic is introduced -- to run a re-calibration cycle over
design doc section 6.4/5.2's shorter 1.5s ("1.5秒間 静寂区間を探索しつ
つ再サンプリング") observation window instead of the initial 2.5s one,
and to hold whichever CalibrationResult (initial or most recently
re-calibrated) is currently active. Also exposes
request_manual_recalibration(), the minimal API design doc FR-7's
future 'c'-key handler will call (see Implementation Plan section
1.2.1) -- not wired to keyboard_bridge.py/ui/keyboard.py/main.py as of
Phase 4, per this Phase's explicit boundary.

Explicitly NOT implemented in Phase 4 (do not infer these exist):
  - FR-6's automatic drift trigger (design doc section 6.4/7/10.6:
    "直近10秒間の Speech Gate 棄却率が...大きく乖離した場合"). The
    design doc states this condition in prose only -- no concrete
    threshold, formula, or window-comparison algorithm is specified
    anywhere in either design doc. Inventing one is explicitly out of
    scope for this Phase (confirmed instruction: no threshold not
    already in the design doc). RecalibrationController.
    begin_recalibration() is the extension point a future phase's
    drift detector will call once design doc section 6.4's threshold
    is actually specified -- until then it is only reachable via
    request_manual_recalibration().
  - CalibrationState / the full 8-state Runtime state machine (design
    doc section 7) -- was already out of scope as of Phase 2/3 (see
    below) and remains so; RecalibrationController tracks only
    "currently recalibrating or not" and the active/last CalibrationResult,
    not the full state machine.
  - Any wiring into AudioBridge, main.py, keyboard_bridge.py,
    ui/keyboard.py, websocket_client.py, or the Server.

Out of scope for this module as of Phase 2 (see the Implementation
Plan's Phase boundaries, and this Phase's own explicit exclusions --
these are Phase 3+ and deliberately not implemented here):
  - Runtime UI (design doc section 8)
  - CalibrationState / the Runtime state machine (design doc section 7)
  - Drift monitoring / re-calibration triggers (design doc section 6.4)
  - Fallback policy decisions beyond reporting success=False (design
    doc section 9) -- Phase 2 reports a failed calibration as-is; it
    does not adopt a substitute Fallback value
  - Any wiring into AudioBridge, main.py, keyboard_bridge.py,
    websocket_client.py, or the Server -- this module is not imported
    by, and does not import, any of those as of Phase 2.

On the block_rms() dependency below: it is a pure function (RMS of one
PCM16LE block, no dependency on any AudioBridge instance state), so it
is not really AudioBridge's responsibility -- it lives in
audio_bridge.py purely for historical reasons (introduced there for
P5-4-1's Silence Gate, before this module existed). This Phase
deliberately does not move or refactor it: Backward Compatibility is
prioritized over tidiness, since relocating it would touch
audio_bridge.py and its existing tests for zero behavioral gain.
Extracting it to a shared module (e.g. a future audio_utils.py) is a
legitimate future cleanup, not done here.

EXPORTED API:
  NoiseFloorSampler  -- one observation window: collects block RMS
                        samples, detects contamination, and reduces
                        the window to its 90th-percentile Noise Floor
                        once complete.
  EnvironmentObserver -- drives NoiseFloorSampler across up to
                        max_attempts windows, retrying on
                        contamination (design doc section 6.2's
                        "リトライ" bullet), and reports the final
                        ObservationResult.
  ObservationResult   -- outcome of an EnvironmentObserver run: whether
                        it succeeded, the measured noise_floor (or None
                        on failure), how many blocks were sampled in
                        the winning window, and how many attempts it
                        took.
  CalibrationEngine   -- derives the Speech Gate from an
                        ObservationResult via design doc section 6.3's
                        clamp(noise_floor * 3.0, 150, 2500) formula.
  CalibrationResult   -- outcome of a CalibrationEngine run: whether
                        the underlying observation succeeded, the
                        noise_floor it was derived from, the derived
                        speech_gate (or None on failure), and the
                        sample_count/attempts carried over from the
                        ObservationResult it was derived from.
  RecalibrationController -- (Phase 4) runs re-calibration cycles (design
                        doc section 6.4's 1.5s window) on top of
                        EnvironmentObserver/CalibrationEngine, holding
                        whichever CalibrationResult is currently active
                        and exposing request_manual_recalibration() (FR-7)
                        plus begin_recalibration() as the trigger-agnostic
                        entry point a future FR-6 drift detector will use.
"""

import os
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

# Historical placement, not a design endorsement -- see module docstring above.
from runtime_client.audio_bridge import block_rms
from runtime_client import debug_sink

# ---------------------------------------------------------------------------
# Production Verification investigation instrumentation (TEMPORARY).
#
# Added to trace why Adaptive Runtime Calibration's Startup Calibration is
# reportedly always Calibration Failed on Production Verification while all
# 429 unit tests pass -- see investigation notes. Not part of the
# calibration algorithm: inert unless PHANTOM_CALIBRATION_DEBUG=1 is set,
# so default behavior (including every existing test) is unchanged.
#
# Deliberately funneled through this single helper (matching the existing
# show_info()/show_warn()/_print() display convention -- see main.py -- not
# the stdlib `logging` module) so every call site this investigation adds
# can be found with `grep -rn PHANTOM_CALIBRATION_DEBUG` and deleted
# together, along with this function, once the root cause is found.
#
# Also tees to debug_sink's session log file (Production Verification
# Support: --production-verification, see main.py) when one is open --
# stdout output/gating is otherwise identical to before that addition.
# ---------------------------------------------------------------------------
def _debug_log(message: str) -> None:
    if os.getenv("PHANTOM_CALIBRATION_DEBUG") == "1":
        line = f"[calibration-debug] {message}"
        print(line, flush=True)
        debug_sink.write(line)

# design doc section 6.2: 2.5s observation window, 100ms blocks -> ~25 samples
DEFAULT_WINDOW_BLOCKS = 25

# design doc section 6.2: 90th percentile, not mean or max
DEFAULT_PERCENTILE = 90

# Noise Floor Safety Floor: the design doc's section 6.3 min clamp bound
# (150 RMS), reused here -- unchanged -- as the contamination-detection
# threshold per section 6.2's own fallback clause ("直近の Speech Gate
# 仮値、または既定の安全下限"). Phase 1 has no CalibrationEngine/Speech
# Gate yet, so this is always the "既定の安全下限" branch: a sample at or
# above this floor is treated as too loud to be silence. Borrows the
# constant value only, not the Speech Gate derivation formula
# (clamp(noise_floor * 3.0, 150, 2500)), which is Phase 2's concern.
DEFAULT_NOISE_FLOOR_SAFETY_FLOOR = 150.0

# design doc section 6.2: "最大3回" -- 3 total attempts, matching section
# 9.4's "2.5秒 x リトライ(最大3回) = 最大7.5秒" arithmetic
DEFAULT_MAX_ATTEMPTS = 3

# Recalibrated per docs/designs/ADAPTIVE_CALIBRATION_DESIGN_REVIEW.md
# (Option 1), superseding design doc section 6.3's original 3.0. Production
# Verification measured actual conversational RMS against measured Noise
# Floor on two real mics and found the ratio topped out at 1.7-1.8x
# (MacBook Pro internal: 325/180 = 1.8x; USB external: 1700/985 = 1.73x)
# -- never reaching 3x, so a 3.0 multiplier put the Speech Gate above any
# normal conversational RMS the Runtime ever actually observed (internal
# mic: Gate 540 vs. a 120-325 conversational range that never crosses it;
# external mic: Gate clamped to the 2500 ceiling vs. a 400-1700 range that
# cannot reach it), which is the root cause behind Production
# Verification's "no Speech START" / "Speech START only momentary, no
# Transcript" symptoms. Further recalibrated from an intermediate 1.5 to
# 1.2 after continued Production Verification, while remaining a relative
# multiplier over the measured Noise Floor (Runtime Philosophy: still
# derived from Runtime Observation, not a fixed absolute RMS value) --
# same formula shape as before, only the coefficient changed. A distinct
# constant from Phase 1's DEFAULT_NOISE_FLOOR_SAFETY_FLOOR above; see
# DEFAULT_CONTAMINATION_THRESHOLD_MULTIPLIER below for why Startup
# Calibration's contamination detection is kept as a separate constant
# from this one.
DEFAULT_SPEECH_GATE_MULTIPLIER = 1.2

# design doc section 6.3: "`min=150` は...Gate が過敏になりすぎて僅かな
# 環境音にも反応することを防ぐ安全下限" -- the clamp's lower bound.
DEFAULT_SPEECH_GATE_MIN = 150.0

# design doc section 6.3: "`max=2500` は...Gate が現実的に到達不能な値
# まで跳ね上がることを防ぐ安全上限" -- the clamp's upper bound.
DEFAULT_SPEECH_GATE_MAX = 2500.0

# docs/designs/ADAPTIVE_CALIBRATION_DESIGN_REVIEW.md, Option 1: a separate
# constant on purpose, independent of DEFAULT_SPEECH_GATE_MULTIPLIER above,
# even though continued Production Verification has since brought both to
# the same 1.2 value. main.py's Startup Calibration reuses CalibrationEngine's
# own clamp(floor * multiplier, ...) formula to derive its dynamic
# Contamination Threshold (see _derive_dynamic_contamination_threshold
# there) -- a *different* concern from the Speech Gate above ("is this
# block loud enough to be someone talking during what should be a silent
# window?" vs. "is this block loud enough to be worth transcribing during
# normal operation?"). Keeping this as its own constant means future
# recalibration of either value does not silently change the other, even
# when they happen to coincide numerically today.
DEFAULT_CONTAMINATION_THRESHOLD_MULTIPLIER = 1.2

# design doc section 6.4 / 5.2 sequence diagram: "1.5秒間 静寂区間を探索
# しつつ再サンプリング" -- the re-calibration observation window is
# shorter than the initial 2.5s one (DEFAULT_WINDOW_BLOCKS above), same
# 100ms block granularity -> ~15 samples. Distinct constant from
# DEFAULT_WINDOW_BLOCKS because the design doc gives these two windows
# two different lengths for two different situations (initial blocking
# calibration vs. background re-calibration that must not interrupt
# recording, NFR-2).
DEFAULT_RECALIBRATION_WINDOW_SECONDS = 1.5
DEFAULT_RECALIBRATION_WINDOW_BLOCKS = 15


class NoiseFloorSampler:
    """
    One observation window. Caller feeds it raw PCM16LE blocks one at a
    time via add_block(); once window_blocks samples have been
    collected, the window is complete. If any sampled block's RMS met
    or exceeded contamination_threshold (the Noise Floor Safety Floor,
    see DEFAULT_NOISE_FLOOR_SAFETY_FLOOR above), the window is
    contaminated -- noise_floor() then returns None even though the
    window is complete, since a contaminated window cannot be trusted
    as a silence measurement (design doc section 6.2's "汚染検出").
    """

    def __init__(
        self,
        window_blocks: int = DEFAULT_WINDOW_BLOCKS,
        contamination_threshold: float = DEFAULT_NOISE_FLOOR_SAFETY_FLOOR,
        percentile: float = DEFAULT_PERCENTILE,
    ) -> None:
        self._window_blocks = window_blocks
        self._contamination_threshold = contamination_threshold
        self._percentile = percentile
        self._samples: List[float] = []
        self._contaminated = False

    def add_block(self, block: np.ndarray) -> None:
        """
        Feed one raw audio block into the current window. Ignored once
        the window is already complete (caller is expected to check
        is_complete and stop feeding, but this stays inert rather than
        raising if it doesn't).
        """
        if self.is_complete:
            return
        rms = block_rms(block)
        contaminated_this_block = rms >= self._contamination_threshold
        if contaminated_this_block:
            self._contaminated = True
        self._samples.append(rms)

        _debug_log(
            f"Block {len(self._samples):02d} RMS={rms:.1f}"
            + (" -> contamination" if contaminated_this_block else "")
        )
        if self.is_complete:
            _debug_log(
                f"Window end: sample_count={len(self._samples)} "
                f"min={min(self._samples):.1f} max={max(self._samples):.1f} "
                f"percentile_target={self._percentile} "
                f"contaminated={self._contaminated} "
                f"noise_floor={self.noise_floor()}"
            )

    @property
    def is_complete(self) -> bool:
        return len(self._samples) >= self._window_blocks

    @property
    def is_contaminated(self) -> bool:
        return self._contaminated

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def noise_floor(self) -> Optional[float]:
        """
        The window's 90th-percentile RMS (design doc section 6.2), or
        None if the window isn't complete yet or was contaminated.
        """
        if not self.is_complete or self._contaminated:
            return None
        return float(np.percentile(self._samples, self._percentile))


@dataclass(frozen=True)
class ObservationResult:
    """
    Outcome of an EnvironmentObserver run. success=False means every
    attempt up to max_attempts was contaminated (design doc section
    6.2's retry exhaustion, section 9.4's timeout) -- noise_floor is
    None in that case. attempts fully reconstructs the "N回中N回失敗"
    accounting design doc section 9.1/8.4 need (attempts == max_attempts
    exactly when success is False), so no separate contaminated flag or
    per-attempt history is tracked here -- see the Phase 1 completion
    report for why that was considered and not added. Deciding what to
    do about a failed observation (Fallback policy, UI messaging) is
    explicitly Phase 2+ (see module docstring); this dataclass only
    reports what was measured.
    """

    success: bool
    noise_floor: Optional[float]
    sample_count: int
    attempts: int


class EnvironmentObserver:
    """
    Drives NoiseFloorSampler across up to max_attempts observation
    windows, restarting a fresh window on contamination (design doc
    section 6.2's "リトライ" bullet). Caller feeds blocks one at a time
    via add_block(), exactly as it would to a single NoiseFloorSampler
    -- this class transparently manages the retry-on-contamination
    loop underneath that same feed interface.
    """

    def __init__(
        self,
        window_blocks: int = DEFAULT_WINDOW_BLOCKS,
        contamination_threshold: float = DEFAULT_NOISE_FLOOR_SAFETY_FLOOR,
        percentile: float = DEFAULT_PERCENTILE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._window_blocks = window_blocks
        self._contamination_threshold = contamination_threshold
        self._percentile = percentile
        self._max_attempts = max_attempts
        self._attempt = 1
        self._sampler = self._new_sampler()
        self._result: Optional[ObservationResult] = None
        _debug_log(f"Attempt {self._attempt} started")

    def _new_sampler(self) -> NoiseFloorSampler:
        return NoiseFloorSampler(
            window_blocks=self._window_blocks,
            contamination_threshold=self._contamination_threshold,
            percentile=self._percentile,
        )

    @property
    def is_finished(self) -> bool:
        return self._result is not None

    @property
    def attempt(self) -> int:
        return self._attempt

    def add_block(self, block: np.ndarray) -> None:
        """
        Feed one raw audio block into the current attempt's window.
        No-op once is_finished is True.
        """
        if self.is_finished:
            return

        self._sampler.add_block(block)
        if not self._sampler.is_complete:
            return

        if not self._sampler.is_contaminated:
            self._result = ObservationResult(
                success=True,
                noise_floor=self._sampler.noise_floor(),
                sample_count=self._sampler.sample_count,
                attempts=self._attempt,
            )
            _debug_log(
                f"Attempt {self._attempt}: window clean -> ObservationResult "
                f"success=True noise_floor={self._result.noise_floor} "
                f"sample_count={self._result.sample_count} attempts={self._result.attempts}"
            )
            return

        if self._attempt >= self._max_attempts:
            self._result = ObservationResult(
                success=False,
                noise_floor=None,
                sample_count=self._sampler.sample_count,
                attempts=self._attempt,
            )
            _debug_log(
                f"Attempt {self._attempt}: window contaminated, max_attempts="
                f"{self._max_attempts} reached -> ObservationResult success=False "
                f"sample_count={self._result.sample_count} attempts={self._result.attempts}"
            )
            return

        _debug_log(f"Attempt {self._attempt}: window contaminated -> retrying")
        self._attempt += 1
        self._sampler = self._new_sampler()
        _debug_log(f"Attempt {self._attempt} started")

    def result(self) -> Optional[ObservationResult]:
        """None until is_finished is True, then the final outcome."""
        return self._result


@dataclass(frozen=True)
class CalibrationResult:
    """
    Outcome of a CalibrationEngine.calibrate() call. Mirrors the
    ObservationResult it was derived from (success, sample_count,
    attempts carried over verbatim) plus the derived speech_gate.
    success=False (observation failed) means noise_floor and
    speech_gate are both None -- design doc section 6.3's derivation
    has nothing to operate on without a measured Noise Floor. Deciding
    what to do about a failed calibration (Fallback policy, UI
    messaging) is explicitly Phase 3+ (see module docstring); this
    dataclass only reports what was derived.
    """

    success: bool
    noise_floor: Optional[float]
    speech_gate: Optional[float]
    sample_count: int
    attempts: int


class CalibrationEngine:
    """
    Derives the Speech Gate from an EnvironmentObserver's
    ObservationResult, per design doc section 6.3:

        speech_gate = clamp(noise_floor * multiplier, gate_min, gate_max)

    Takes only an ObservationResult as input (design doc's Runtime
    Philosophy: derive Runtime parameters from Runtime Observation, not
    from assumed fixed values) -- it does not sample audio itself and
    has no dependency on NoiseFloorSampler.
    """

    def __init__(
        self,
        multiplier: float = DEFAULT_SPEECH_GATE_MULTIPLIER,
        gate_min: float = DEFAULT_SPEECH_GATE_MIN,
        gate_max: float = DEFAULT_SPEECH_GATE_MAX,
    ) -> None:
        self._multiplier = multiplier
        self._gate_min = gate_min
        self._gate_max = gate_max

    def _derive_speech_gate(self, noise_floor: float) -> float:
        return max(self._gate_min, min(noise_floor * self._multiplier, self._gate_max))

    def calibrate(self, observation: ObservationResult) -> CalibrationResult:
        """
        Derive a CalibrationResult from an ObservationResult. If the
        observation failed (design doc section 6.2's retry exhaustion),
        the calibration fails too -- there is no noise_floor to derive
        a speech_gate from.
        """
        _debug_log(
            f"CalibrationEngine input: success={observation.success} "
            f"noise_floor={observation.noise_floor} "
            f"sample_count={observation.sample_count} attempts={observation.attempts}"
        )

        if not observation.success or observation.noise_floor is None:
            _debug_log(
                "CalibrationEngine result: success=False "
                "(observation failed or noise_floor is None -- nothing to derive gate from)"
            )
            return CalibrationResult(
                success=False,
                noise_floor=None,
                speech_gate=None,
                sample_count=observation.sample_count,
                attempts=observation.attempts,
            )

        result = CalibrationResult(
            success=True,
            noise_floor=observation.noise_floor,
            speech_gate=self._derive_speech_gate(observation.noise_floor),
            sample_count=observation.sample_count,
            attempts=observation.attempts,
        )
        _debug_log(
            f"CalibrationEngine result: success=True "
            f"noise_floor={result.noise_floor} speech_gate={result.speech_gate}"
        )
        return result


class RecalibrationController:
    """
    Re-calibration Controller (P5-4 Phase 4, design doc section 6.4 /
    5.2's sequence diagram). Reuses EnvironmentObserver and
    CalibrationEngine exactly as Phase 1/2 built them to run background
    re-calibration cycles over a 1.5s window (DEFAULT_RECALIBRATION_WINDOW_BLOCKS,
    shorter than the initial 2.5s calibration's DEFAULT_WINDOW_BLOCKS) --
    no new sampling/contamination/percentile/gate-derivation algorithm
    is introduced here, only a different window length the design doc
    itself specifies for this situation.

    Holds whichever CalibrationResult is currently active: the
    caller-supplied initial_result until a re-calibration cycle
    succeeds, at which point active_result becomes that cycle's result
    (design doc section 6.4's "明確な環境変化が確認された時点で Speech
    Gate のみを差し替える"). A cycle that finishes without success
    (contamination exhausted every retry, mirroring EnvironmentObserver's
    own failure mode) leaves active_result untouched -- see last_result
    to distinguish "no cycle has run yet" from "the last cycle failed".

    Exposes a single, trigger-source-agnostic entry point,
    begin_recalibration(), for starting a new cycle. Design doc section
    6.4 defines two distinct trigger sources for that same action:
      - FR-7 (manual): the 'c' key -- see request_manual_recalibration()
        below, the minimal API a future keyboard_bridge.py handler will
        call (Implementation Plan section 1.2.1's "ctx.recalibrate_fn()
        相当"). Not wired to any keyboard code as of Phase 4.
      - FR-6 (automatic): a reject-rate drift detector. The design doc
        never specifies a concrete threshold/formula for "大きく乖離した
        場合" (see module docstring's Phase 4 scope note), so Phase 4
        does not implement it -- begin_recalibration() is the extension
        point a future phase's drift detector will call once that
        threshold is defined; it is unreachable automatically until then.
    """

    def __init__(
        self,
        engine: CalibrationEngine,
        initial_result: CalibrationResult,
        window_blocks: int = DEFAULT_RECALIBRATION_WINDOW_BLOCKS,
        contamination_threshold: float = DEFAULT_NOISE_FLOOR_SAFETY_FLOOR,
        percentile: float = DEFAULT_PERCENTILE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._engine = engine
        self._window_blocks = window_blocks
        self._contamination_threshold = contamination_threshold
        self._percentile = percentile
        self._max_attempts = max_attempts
        self._active_result = initial_result
        self._last_result: Optional[CalibrationResult] = None
        self._observer: Optional[EnvironmentObserver] = None

    @property
    def active_result(self) -> CalibrationResult:
        """The currently active CalibrationResult: initial_result until
        a re-calibration cycle succeeds, then that cycle's result."""
        return self._active_result

    @property
    def last_result(self) -> Optional[CalibrationResult]:
        """The most recently completed re-calibration cycle's
        CalibrationResult (success or failure), or None if no cycle has
        completed yet. Distinct from active_result, which only ever
        reflects a successful outcome."""
        return self._last_result

    @property
    def is_recalibrating(self) -> bool:
        """True from begin_recalibration() until the in-progress cycle's
        window (across all its contamination retries) finishes."""
        return self._observer is not None

    def request_manual_recalibration(self) -> None:
        """FR-7: the manual-trigger equivalent API (design doc section
        6.4's "手動トリガー...専用キー(例: c)でいつでも明示的に要求で
        きる"). Semantically identical to begin_recalibration() -- see
        class docstring -- named separately so a future keyboard handler
        has an explicit, self-describing call target."""
        self.begin_recalibration()

    def begin_recalibration(self) -> None:
        """Starts a fresh re-calibration cycle (design doc section 6.4's
        1.5s window). If a cycle is already in progress, it is discarded
        and replaced -- partial samples from the old cycle are not
        carried over, matching EnvironmentObserver's own per-attempt
        behavior on contamination."""
        self._observer = EnvironmentObserver(
            window_blocks=self._window_blocks,
            contamination_threshold=self._contamination_threshold,
            percentile=self._percentile,
            max_attempts=self._max_attempts,
        )

    def add_block(self, block: np.ndarray) -> None:
        """Feed one raw audio block into the in-progress re-calibration
        cycle. No-op if is_recalibrating is False. Once the underlying
        EnvironmentObserver finishes (success or retry exhaustion), the
        cycle ends: on success, active_result is replaced; on failure,
        active_result is left as-is (see class docstring) and last_result
        reports the failure so a caller can surface it."""
        if self._observer is None:
            return

        self._observer.add_block(block)
        if not self._observer.is_finished:
            return

        result = self._engine.calibrate(self._observer.result())
        self._last_result = result
        if result.success:
            self._active_result = result
        self._observer = None
