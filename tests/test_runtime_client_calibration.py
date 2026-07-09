"""
tests/test_runtime_client_calibration.py
=============================================
Unit tests for src/runtime_client/calibration.py -- Phase 1
(Environment Observation) and Phase 2 (Calibration Engine) of P5-4
Adaptive Runtime Calibration only. See
docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md section 6.2/6.3 and
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
    DEFAULT_SPEECH_GATE_MAX,
    DEFAULT_SPEECH_GATE_MIN,
    DEFAULT_SPEECH_GATE_MULTIPLIER,
    CalibrationEngine,
    CalibrationResult,
    EnvironmentObserver,
    NoiseFloorSampler,
    ObservationResult,
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


if __name__ == "__main__":
    unittest.main()
