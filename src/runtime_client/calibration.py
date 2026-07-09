"""
runtime_client/calibration.py
================================
Environment Observation (P5-4 Adaptive Runtime Calibration, Phase 1 --
see docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md, section 6.2, and
docs/designs/IMPLEMENTATION_PLAN_P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md,
section 3.1).

Phase 1 scope only: measuring the acoustic Noise Floor of the current
environment from a short window of captured audio blocks, detecting
contamination (speech leaking into what was supposed to be a silent
measurement window) and retrying, and reducing a clean window down to
a single Noise Floor value via its 90th percentile.

Out of scope for this module as of Phase 1 (see the Implementation
Plan's Phase boundaries -- these are Phase 2+ and deliberately not
implemented here):
  - Speech Gate derivation (design doc section 6.3: noise_floor -> gate)
  - CalibrationState / the Runtime state machine (design doc section 7)
  - Drift monitoring / re-calibration triggers (design doc section 6.4)
  - Fallback policy decisions (design doc section 9)
  - Any wiring into AudioBridge, main.py, keyboard_bridge.py,
    websocket_client.py, or the Server -- this module is not imported
    by, and does not import, any of those as of Phase 1.

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
"""

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

# Historical placement, not a design endorsement -- see module docstring above.
from runtime_client.audio_bridge import block_rms

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
        if rms >= self._contamination_threshold:
            self._contaminated = True
        self._samples.append(rms)

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
            return

        if self._attempt >= self._max_attempts:
            self._result = ObservationResult(
                success=False,
                noise_floor=None,
                sample_count=self._sampler.sample_count,
                attempts=self._attempt,
            )
            return

        self._attempt += 1
        self._sampler = self._new_sampler()

    def result(self) -> Optional[ObservationResult]:
        """None until is_finished is True, then the final outcome."""
        return self._result
