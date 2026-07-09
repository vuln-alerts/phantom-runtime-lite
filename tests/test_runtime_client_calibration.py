"""
tests/test_runtime_client_calibration.py
=============================================
Unit tests for src/runtime_client/calibration.py -- Phase 1
(Environment Observation), Phase 2 (Calibration Engine), and Phase 4
(Re-calibration) of P5-4 Adaptive Runtime Calibration. See
docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md section 6.2/6.3/6.4
and
docs/designs/IMPLEMENTATION_PLAN_P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md
section 3.1/6.

Covers:
- NoiseFloorSampler: window completion, 90th-percentile noise_floor
  computation, contamination detection against the Noise Floor Safety
  Floor, and post-completion inertness.
- EnvironmentObserver: single-attempt success, retry-then-succeed on
  contamination, retry exhaustion (failure), and post-finish inertness.
- CalibrationEngine: Speech Gate derivation from an ObservationResult
  per section 6.3's clamp(noise_floor * 3.0, 150, 2500) formula (below
  min clamp, normal multiplication, above max clamp), CalibrationResult
  field integrity, and end-to-end use of EnvironmentObserver's own
  result as CalibrationEngine's input.
- RecalibrationController (Phase 4): active_result/last_result
  bookkeeping, is_recalibrating lifecycle, the 1.5s/15-block
  re-calibration window (design doc section 6.4/5.2), a cycle in
  progress being discarded and replaced by a fresh begin_recalibration()
  call, and request_manual_recalibration() being equivalent to
  begin_recalibration(). No test here exercises an automatic drift
  trigger -- design doc section 6.4/7/10.6's FR-6 condition has no
  concrete threshold specified in either design doc, so Phase 4
  deliberately does not implement or test one (see calibration.py's
  module/class docstrings).

Feeds synthetic int16 PCM blocks directly (no sounddevice/mic
hardware, no network, no Cloud Run, no OpenAI API), consistent with
this project's existing test conventions (see
tests/test_runtime_client_audio_bridge.py).

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import os
import sys
import unittest

import numpy as np

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.calibration import (
    DEFAULT_NOISE_FLOOR_SAFETY_FLOOR,
    DEFAULT_RECALIBRATION_WINDOW_BLOCKS,
    DEFAULT_RECALIBRATION_WINDOW_SECONDS,
    DEFAULT_SPEECH_GATE_MAX,
    DEFAULT_SPEECH_GATE_MIN,
    DEFAULT_SPEECH_GATE_MULTIPLIER,
    CalibrationEngine,
    CalibrationResult,
    EnvironmentObserver,
    NoiseFloorSampler,
    ObservationResult,
    RecalibrationController,
)


def _constant_block(amplitude, n=1600):
    """A block whose every sample equals amplitude -> block_rms() == amplitude exactly."""
    return np.full((n, 1), amplitude, dtype=np.int16)


class TestNoiseFloorSamplerWindowCompletion(unittest.TestCase):
    def test_incomplete_before_window_filled(self):
        sampler = NoiseFloorSampler(window_blocks=5)
        for _ in range(4):
            sampler.add_block(_constant_block(10))
        self.assertFalse(sampler.is_complete)
        self.assertEqual(sampler.sample_count, 4)

    def test_complete_after_window_filled(self):
        sampler = NoiseFloorSampler(window_blocks=5)
        for _ in range(5):
            sampler.add_block(_constant_block(10))
        self.assertTrue(sampler.is_complete)
        self.assertEqual(sampler.sample_count, 5)

    def test_extra_blocks_after_complete_are_ignored(self):
        sampler = NoiseFloorSampler(window_blocks=3)
        for _ in range(3):
            sampler.add_block(_constant_block(10))
        sampler.add_block(_constant_block(10))
        sampler.add_block(_constant_block(10))
        self.assertEqual(sampler.sample_count, 3)


class TestNoiseFloorSamplerComputation(unittest.TestCase):
    def test_noise_floor_none_before_complete(self):
        sampler = NoiseFloorSampler(window_blocks=5)
        sampler.add_block(_constant_block(10))
        self.assertIsNone(sampler.noise_floor())

    def test_noise_floor_matches_numpy_percentile90(self):
        # Distinct amplitudes so p90 is unambiguous; none reach the
        # default 150 safety floor, so the window stays uncontaminated.
        amplitudes = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        sampler = NoiseFloorSampler(window_blocks=len(amplitudes))
        for amp in amplitudes:
            sampler.add_block(_constant_block(amp))
        expected = float(np.percentile(amplitudes, 90))
        self.assertAlmostEqual(sampler.noise_floor(), expected, places=6)

    def test_noise_floor_uses_caller_supplied_percentile(self):
        amplitudes = [10, 20, 30, 40, 50]
        sampler = NoiseFloorSampler(window_blocks=len(amplitudes), percentile=50)
        for amp in amplitudes:
            sampler.add_block(_constant_block(amp))
        expected = float(np.percentile(amplitudes, 50))
        self.assertAlmostEqual(sampler.noise_floor(), expected, places=6)


class TestNoiseFloorSamplerContamination(unittest.TestCase):
    def test_default_safety_floor_is_150(self):
        self.assertEqual(DEFAULT_NOISE_FLOOR_SAFETY_FLOOR, 150.0)

    def test_block_below_threshold_not_contaminated(self):
        sampler = NoiseFloorSampler(window_blocks=1, contamination_threshold=150.0)
        sampler.add_block(_constant_block(149))
        self.assertFalse(sampler.is_contaminated)
        self.assertIsNotNone(sampler.noise_floor())

    def test_block_at_threshold_is_contaminated(self):
        sampler = NoiseFloorSampler(window_blocks=1, contamination_threshold=150.0)
        sampler.add_block(_constant_block(150))
        self.assertTrue(sampler.is_contaminated)

    def test_block_above_threshold_is_contaminated(self):
        sampler = NoiseFloorSampler(window_blocks=1, contamination_threshold=150.0)
        sampler.add_block(_constant_block(5000))
        self.assertTrue(sampler.is_contaminated)

    def test_contaminated_window_noise_floor_is_none_even_when_complete(self):
        sampler = NoiseFloorSampler(window_blocks=2, contamination_threshold=150.0)
        sampler.add_block(_constant_block(10))
        sampler.add_block(_constant_block(5000))
        self.assertTrue(sampler.is_complete)
        self.assertTrue(sampler.is_contaminated)
        self.assertIsNone(sampler.noise_floor())

    def test_single_contaminated_block_taints_whole_window(self):
        sampler = NoiseFloorSampler(window_blocks=3, contamination_threshold=150.0)
        sampler.add_block(_constant_block(10))
        sampler.add_block(_constant_block(5000))  # one loud block mid-window
        sampler.add_block(_constant_block(10))
        self.assertTrue(sampler.is_contaminated)


class TestEnvironmentObserverSuccess(unittest.TestCase):
    def test_succeeds_on_first_clean_window(self):
        observer = EnvironmentObserver(window_blocks=3, contamination_threshold=150.0)
        for _ in range(3):
            observer.add_block(_constant_block(10))
        self.assertTrue(observer.is_finished)
        result = observer.result()
        self.assertIsInstance(result, ObservationResult)
        self.assertTrue(result.success)
        self.assertAlmostEqual(result.noise_floor, 10.0, places=6)
        self.assertEqual(result.sample_count, 3)
        self.assertEqual(result.attempts, 1)

    def test_not_finished_mid_window(self):
        observer = EnvironmentObserver(window_blocks=3, contamination_threshold=150.0)
        observer.add_block(_constant_block(10))
        self.assertFalse(observer.is_finished)
        self.assertIsNone(observer.result())


class TestEnvironmentObserverRetry(unittest.TestCase):
    def test_retries_after_contaminated_window_then_succeeds(self):
        observer = EnvironmentObserver(
            window_blocks=2, contamination_threshold=150.0, max_attempts=3
        )
        # Attempt 1: contaminated.
        observer.add_block(_constant_block(10))
        observer.add_block(_constant_block(5000))
        self.assertFalse(observer.is_finished)
        self.assertEqual(observer.attempt, 2)

        # Attempt 2: clean -> success.
        observer.add_block(_constant_block(20))
        observer.add_block(_constant_block(20))
        self.assertTrue(observer.is_finished)
        result = observer.result()
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertAlmostEqual(result.noise_floor, 20.0, places=6)

    def test_fresh_window_after_retry_does_not_carry_over_old_samples(self):
        observer = EnvironmentObserver(
            window_blocks=2, contamination_threshold=150.0, max_attempts=3
        )
        observer.add_block(_constant_block(5000))  # contaminates attempt 1 immediately
        observer.add_block(_constant_block(10))    # completes (contaminated) attempt 1
        self.assertEqual(observer.attempt, 2)
        # Attempt 2 needs its own 2 blocks, not leftovers from attempt 1.
        observer.add_block(_constant_block(30))
        self.assertFalse(observer.is_finished)
        observer.add_block(_constant_block(30))
        self.assertTrue(observer.is_finished)
        self.assertEqual(observer.result().sample_count, 2)


class TestEnvironmentObserverExhaustion(unittest.TestCase):
    def test_fails_after_max_attempts_all_contaminated(self):
        observer = EnvironmentObserver(
            window_blocks=1, contamination_threshold=150.0, max_attempts=3
        )
        observer.add_block(_constant_block(5000))  # attempt 1: contaminated
        self.assertFalse(observer.is_finished)
        observer.add_block(_constant_block(5000))  # attempt 2: contaminated
        self.assertFalse(observer.is_finished)
        observer.add_block(_constant_block(5000))  # attempt 3: contaminated, exhausted
        self.assertTrue(observer.is_finished)

        result = observer.result()
        self.assertFalse(result.success)
        self.assertIsNone(result.noise_floor)
        self.assertEqual(result.attempts, 3)

    def test_attempts_never_exceeds_max_attempts(self):
        observer = EnvironmentObserver(
            window_blocks=1, contamination_threshold=150.0, max_attempts=2
        )
        observer.add_block(_constant_block(5000))
        observer.add_block(_constant_block(5000))
        self.assertTrue(observer.is_finished)
        self.assertEqual(observer.result().attempts, 2)

    def test_add_block_is_noop_after_finished(self):
        observer = EnvironmentObserver(window_blocks=1, contamination_threshold=150.0, max_attempts=1)
        observer.add_block(_constant_block(10))
        self.assertTrue(observer.is_finished)
        first_result = observer.result()
        observer.add_block(_constant_block(9999))  # must not mutate the finished result
        self.assertIs(observer.result(), first_result)


class TestCalibrationEngineDerivation(unittest.TestCase):
    def test_default_constants_match_design_doc_section_6_3(self):
        self.assertEqual(DEFAULT_SPEECH_GATE_MULTIPLIER, 3.0)
        self.assertEqual(DEFAULT_SPEECH_GATE_MIN, 150.0)
        self.assertEqual(DEFAULT_SPEECH_GATE_MAX, 2500.0)

    def test_noise_floor_below_min_clamps_speech_gate_to_150(self):
        # 10 * 3.0 == 30, below the 150 floor.
        engine = CalibrationEngine()
        observation = ObservationResult(
            success=True, noise_floor=10.0, sample_count=25, attempts=1
        )
        result = engine.calibrate(observation)
        self.assertEqual(result.speech_gate, 150.0)

    def test_normal_noise_floor_multiplies_by_3(self):
        # design doc section 8.3 example: 182 RMS -> 546 RMS.
        engine = CalibrationEngine()
        observation = ObservationResult(
            success=True, noise_floor=182.0, sample_count=25, attempts=1
        )
        result = engine.calibrate(observation)
        self.assertAlmostEqual(result.speech_gate, 546.0, places=6)

    def test_noise_floor_above_max_clamps_speech_gate_to_2500(self):
        # 1000 * 3.0 == 3000, above the 2500 ceiling.
        engine = CalibrationEngine()
        observation = ObservationResult(
            success=True, noise_floor=1000.0, sample_count=25, attempts=1
        )
        result = engine.calibrate(observation)
        self.assertEqual(result.speech_gate, 2500.0)

    def test_calibration_result_holds_all_fields_correctly(self):
        engine = CalibrationEngine()
        observation = ObservationResult(
            success=True, noise_floor=182.0, sample_count=25, attempts=2
        )
        result = engine.calibrate(observation)
        self.assertIsInstance(result, CalibrationResult)
        self.assertTrue(result.success)
        self.assertEqual(result.noise_floor, 182.0)
        self.assertAlmostEqual(result.speech_gate, 546.0, places=6)
        self.assertEqual(result.sample_count, 25)
        self.assertEqual(result.attempts, 2)

    def test_failed_observation_yields_failed_calibration_result(self):
        engine = CalibrationEngine()
        observation = ObservationResult(
            success=False, noise_floor=None, sample_count=1, attempts=3
        )
        result = engine.calibrate(observation)
        self.assertFalse(result.success)
        self.assertIsNone(result.noise_floor)
        self.assertIsNone(result.speech_gate)
        self.assertEqual(result.sample_count, 1)
        self.assertEqual(result.attempts, 3)

    def test_custom_multiplier_and_clamp_bounds_are_honored(self):
        engine = CalibrationEngine(multiplier=2.0, gate_min=50.0, gate_max=1000.0)
        observation = ObservationResult(
            success=True, noise_floor=100.0, sample_count=25, attempts=1
        )
        result = engine.calibrate(observation)
        self.assertAlmostEqual(result.speech_gate, 200.0, places=6)


class TestCalibrationEngineWithEnvironmentObserver(unittest.TestCase):
    def test_consumes_environment_observer_success_result(self):
        observer = EnvironmentObserver(window_blocks=3, contamination_threshold=150.0)
        for _ in range(3):
            observer.add_block(_constant_block(50))
        observation = observer.result()

        engine = CalibrationEngine()
        result = engine.calibrate(observation)

        self.assertTrue(result.success)
        self.assertAlmostEqual(result.noise_floor, 50.0, places=6)
        self.assertAlmostEqual(result.speech_gate, 150.0, places=6)
        self.assertEqual(result.sample_count, 3)
        self.assertEqual(result.attempts, 1)

    def test_consumes_environment_observer_exhaustion_result(self):
        observer = EnvironmentObserver(
            window_blocks=1, contamination_threshold=150.0, max_attempts=3
        )
        for _ in range(3):
            observer.add_block(_constant_block(5000))  # every attempt contaminated
        observation = observer.result()

        engine = CalibrationEngine()
        result = engine.calibrate(observation)

        self.assertFalse(result.success)
        self.assertIsNone(result.noise_floor)
        self.assertIsNone(result.speech_gate)
        self.assertEqual(result.attempts, 3)


def _make_initial_result(speech_gate=546.0, noise_floor=182.0):
    return CalibrationResult(
        success=True,
        noise_floor=noise_floor,
        speech_gate=speech_gate,
        sample_count=25,
        attempts=1,
    )


class TestRecalibrationControllerConstants(unittest.TestCase):
    def test_recalibration_window_matches_design_doc_section_6_4(self):
        # design doc section 6.4 / 5.2 sequence diagram: "1.5秒間" at
        # 100ms blocks -> 15 samples.
        self.assertEqual(DEFAULT_RECALIBRATION_WINDOW_SECONDS, 1.5)
        self.assertEqual(DEFAULT_RECALIBRATION_WINDOW_BLOCKS, 15)


class TestRecalibrationControllerInitialState(unittest.TestCase):
    def test_active_result_is_constructor_supplied_initial_result(self):
        initial = _make_initial_result()
        controller = RecalibrationController(CalibrationEngine(), initial)
        self.assertIs(controller.active_result, initial)

    def test_last_result_is_none_before_any_cycle(self):
        controller = RecalibrationController(CalibrationEngine(), _make_initial_result())
        self.assertIsNone(controller.last_result)

    def test_not_recalibrating_before_begin_recalibration(self):
        controller = RecalibrationController(CalibrationEngine(), _make_initial_result())
        self.assertFalse(controller.is_recalibrating)

    def test_add_block_is_noop_when_not_recalibrating(self):
        initial = _make_initial_result()
        controller = RecalibrationController(CalibrationEngine(), initial)
        controller.add_block(_constant_block(5000))  # loud, but no cycle in progress
        self.assertIs(controller.active_result, initial)
        self.assertIsNone(controller.last_result)


class TestRecalibrationControllerCycleLifecycle(unittest.TestCase):
    def test_begin_recalibration_starts_a_cycle(self):
        controller = RecalibrationController(
            CalibrationEngine(), _make_initial_result(), window_blocks=3
        )
        controller.begin_recalibration()
        self.assertTrue(controller.is_recalibrating)

    def test_cycle_uses_default_15_block_window(self):
        controller = RecalibrationController(CalibrationEngine(), _make_initial_result())
        controller.begin_recalibration()
        for _ in range(DEFAULT_RECALIBRATION_WINDOW_BLOCKS - 1):
            controller.add_block(_constant_block(10))
        self.assertTrue(controller.is_recalibrating)  # one block short
        controller.add_block(_constant_block(10))
        self.assertFalse(controller.is_recalibrating)  # window filled -> cycle ended

    def test_successful_cycle_replaces_active_result(self):
        initial = _make_initial_result(speech_gate=546.0)
        controller = RecalibrationController(
            CalibrationEngine(), initial, window_blocks=3, contamination_threshold=150.0
        )
        controller.begin_recalibration()
        for _ in range(3):
            controller.add_block(_constant_block(20))  # clean, quiet -> succeeds
        self.assertFalse(controller.is_recalibrating)
        self.assertIsNot(controller.active_result, initial)
        self.assertTrue(controller.active_result.success)
        self.assertAlmostEqual(controller.active_result.noise_floor, 20.0, places=6)
        self.assertAlmostEqual(controller.active_result.speech_gate, 150.0, places=6)
        self.assertIs(controller.last_result, controller.active_result)

    def test_exhausted_cycle_leaves_active_result_untouched(self):
        initial = _make_initial_result(speech_gate=546.0)
        controller = RecalibrationController(
            CalibrationEngine(),
            initial,
            window_blocks=1,
            contamination_threshold=150.0,
            max_attempts=2,
        )
        controller.begin_recalibration()
        controller.add_block(_constant_block(5000))  # attempt 1: contaminated
        self.assertTrue(controller.is_recalibrating)
        controller.add_block(_constant_block(5000))  # attempt 2: contaminated, exhausted
        self.assertFalse(controller.is_recalibrating)
        self.assertIs(controller.active_result, initial)  # unchanged
        self.assertIsNotNone(controller.last_result)
        self.assertFalse(controller.last_result.success)

    def test_begin_recalibration_while_in_progress_discards_partial_cycle(self):
        controller = RecalibrationController(
            CalibrationEngine(), _make_initial_result(), window_blocks=5, contamination_threshold=150.0
        )
        controller.begin_recalibration()
        controller.add_block(_constant_block(10))
        controller.add_block(_constant_block(10))  # 2/5 samples into the first cycle

        controller.begin_recalibration()  # restart -- old partial samples must not carry over
        for _ in range(4):
            controller.add_block(_constant_block(20))
        self.assertTrue(controller.is_recalibrating)  # only 4/5 of the *new* window
        controller.add_block(_constant_block(20))
        self.assertFalse(controller.is_recalibrating)
        self.assertAlmostEqual(controller.active_result.noise_floor, 20.0, places=6)


class TestRecalibrationControllerManualTrigger(unittest.TestCase):
    def test_request_manual_recalibration_starts_a_cycle(self):
        controller = RecalibrationController(CalibrationEngine(), _make_initial_result())
        controller.request_manual_recalibration()
        self.assertTrue(controller.is_recalibrating)

    def test_request_manual_recalibration_is_equivalent_to_begin_recalibration(self):
        controller = RecalibrationController(
            CalibrationEngine(), _make_initial_result(), window_blocks=2, contamination_threshold=150.0
        )
        controller.request_manual_recalibration()
        controller.add_block(_constant_block(30))
        controller.add_block(_constant_block(30))
        self.assertAlmostEqual(controller.active_result.noise_floor, 30.0, places=6)


if __name__ == "__main__":
    unittest.main()
