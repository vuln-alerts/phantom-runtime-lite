"""
tests/test_runtime_client_main.py
=====================================
Unit tests for P5-4 Adaptive Runtime Calibration, Phase 5 (Integration)'s
additions to src/runtime_client/main.py:

- _run_initial_calibration: the hardware-independent loop that drains a
  queue.Queue of raw PCM16LE blocks into an EnvironmentObserver
  (calibration.py, Phase 1, unmodified) and renders Phase 3's
  show_calibration_start/show_calibration_progress screens as it goes.
  Feeds synthetic blocks directly into a plain queue.Queue (no
  sounddevice/AudioCapture, no real microphone), the same pattern
  tests/test_runtime_client_calibration.py and
  tests/test_runtime_client_audio_bridge.py already use for
  hardware-independent, deterministic, CI-safe coverage.
- _build_fallback_calibration_result: the design doc section 9.1
  Fallback CalibrationResult construction (reuses the caller-supplied
  fallback_gate verbatim -- introduces no new derivation formula).
- _perform_startup_calibration's AudioCapture construction (PV-1 Blocker
  Fix only): verifies device_name=config.input_device is passed through,
  the other half of the Production Verification root cause fix (see
  tests/test_audio_devices.py for the resolve_device_id() half). Mocks
  AudioCapture entirely -- no real sounddevice/threading race, just an
  assertion on the constructor call's kwargs.

Not covered here (consistent with this project's existing test
conventions, see Implementation Plan section 6's Unit Test Plan, which
marks real-hardware paths as Production E2E only, not Unit Test):
_perform_startup_calibration's own real AudioCapture/threading glue
against actual sounddevice, and _amain's end-to-end startup sequence.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import io
import os
import queue
import sys
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import numpy as np

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.calibration import EnvironmentObserver
from runtime_client.config import ClientConfig
from runtime_client.main import (
    _build_fallback_calibration_result,
    _perform_startup_calibration,
    _run_initial_calibration,
)


def _constant_block(amplitude, n=1600):
    return np.full((n, 1), amplitude, dtype=np.int16)


class TestRunInitialCalibration(unittest.TestCase):
    def test_succeeds_on_clean_window(self):
        observer = EnvironmentObserver(window_blocks=3, contamination_threshold=150.0)
        q: "queue.Queue" = queue.Queue()
        for _ in range(3):
            q.put(_constant_block(10))
        shutdown = threading.Event()

        with redirect_stdout(io.StringIO()):
            _run_initial_calibration(
                observer, q, shutdown, window_seconds=0.3, window_blocks=3
            )

        self.assertTrue(observer.is_finished)
        result = observer.result()
        self.assertTrue(result.success)
        self.assertAlmostEqual(result.noise_floor, 10.0, places=6)
        self.assertEqual(result.attempts, 1)

    def test_retries_after_contamination_then_succeeds(self):
        observer = EnvironmentObserver(window_blocks=2, contamination_threshold=150.0, max_attempts=3)
        q: "queue.Queue" = queue.Queue()
        # Attempt 1: contaminated.
        q.put(_constant_block(10))
        q.put(_constant_block(5000))
        # Attempt 2: clean.
        q.put(_constant_block(20))
        q.put(_constant_block(20))
        shutdown = threading.Event()

        with redirect_stdout(io.StringIO()):
            _run_initial_calibration(
                observer, q, shutdown, window_seconds=0.2, window_blocks=2
            )

        self.assertTrue(observer.is_finished)
        result = observer.result()
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertAlmostEqual(result.noise_floor, 20.0, places=6)

    def test_exhausts_retries_and_reports_failure(self):
        observer = EnvironmentObserver(window_blocks=1, contamination_threshold=150.0, max_attempts=2)
        q: "queue.Queue" = queue.Queue()
        q.put(_constant_block(5000))
        q.put(_constant_block(5000))
        shutdown = threading.Event()

        with redirect_stdout(io.StringIO()):
            _run_initial_calibration(
                observer, q, shutdown, window_seconds=0.1, window_blocks=1
            )

        self.assertTrue(observer.is_finished)
        result = observer.result()
        self.assertFalse(result.success)
        self.assertIsNone(result.noise_floor)
        self.assertEqual(result.attempts, 2)

    def test_shutdown_stops_the_loop_before_completion(self):
        observer = EnvironmentObserver(window_blocks=5, contamination_threshold=150.0)
        q: "queue.Queue" = queue.Queue()
        q.put(_constant_block(10))
        q.put(_constant_block(10))
        shutdown = threading.Event()
        shutdown.set()  # already set -- loop must exit without draining further

        with redirect_stdout(io.StringIO()):
            _run_initial_calibration(
                observer, q, shutdown, window_seconds=0.5, window_blocks=5
            )

        self.assertFalse(observer.is_finished)

    def test_renders_start_and_progress_screens(self):
        observer = EnvironmentObserver(window_blocks=2, contamination_threshold=150.0)
        q: "queue.Queue" = queue.Queue()
        q.put(_constant_block(10))
        q.put(_constant_block(10))
        shutdown = threading.Event()

        buf = io.StringIO()
        with redirect_stdout(buf):
            _run_initial_calibration(
                observer, q, shutdown, window_seconds=0.2, window_blocks=2
            )

        output = buf.getvalue()
        self.assertIn("Audio Calibration", output)
        self.assertIn("0/2 blocks", output)  # show_calibration_start's initial frame


class TestBuildFallbackCalibrationResult(unittest.TestCase):
    def test_fields_reflect_fallback_inputs_not_a_measurement(self):
        result = _build_fallback_calibration_result(
            fallback_gate=700.0, sample_count=3, attempts=3
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.noise_floor)
        self.assertEqual(result.speech_gate, 700.0)
        self.assertEqual(result.sample_count, 3)
        self.assertEqual(result.attempts, 3)


def _make_config(input_device):
    return ClientConfig(
        url="https://example.run.app",
        provider="openai",
        input_device=input_device,
        output_device=None,
        sample_rate=16000,
        channels=1,
        block_size=1600,
        max_reconnect_attempts=3,
        backoff_base_seconds=1.0,
        manual_flush=False,
        silence_rms_threshold=700,
        tts="none",
        voice="Samantha",
        rate=None,
        volume=1.0,
    )


class TestPerformStartupCalibrationDeviceName(unittest.TestCase):
    """PV-1 Blocker Fix: main.py's Startup Calibration AudioCapture must
    be given device_name=config.input_device, so capture.py's own
    'device not found -- using system default' fallback path (previously
    dead code, since device_name was never supplied) actually fires when
    resolve_input_device() returns None. Mocks AudioCapture entirely --
    no real sounddevice, no real microphone."""

    @patch("runtime_client.main.AudioCapture")
    def test_device_name_passed_through_to_audiocapture(self, mock_audio_capture_cls):
        mock_capture = MagicMock()
        mock_capture.resolved_device_name = ""

        def _fake_run(shutdown):
            shutdown.set()  # mimic a capture thread that returns immediately

        mock_capture.run.side_effect = _fake_run
        mock_audio_capture_cls.return_value = mock_capture

        config = _make_config(input_device="1")

        with redirect_stdout(io.StringIO()):
            _perform_startup_calibration(config, device_id=None)

        _, kwargs = mock_audio_capture_cls.call_args
        self.assertEqual(kwargs["device_name"], "1")
        self.assertEqual(kwargs["device_id"], None)

    @patch("runtime_client.main.AudioCapture")
    def test_none_input_device_passes_empty_string(self, mock_audio_capture_cls):
        # config.input_device is None (no --input-device given) --
        # AudioCapture's device_name param is typed str, not Optional,
        # so this must not pass None through.
        mock_capture = MagicMock()
        mock_capture.resolved_device_name = ""

        def _fake_run(shutdown):
            shutdown.set()

        mock_capture.run.side_effect = _fake_run
        mock_audio_capture_cls.return_value = mock_capture

        config = _make_config(input_device=None)

        with redirect_stdout(io.StringIO()):
            _perform_startup_calibration(config, device_id=None)

        _, kwargs = mock_audio_capture_cls.call_args
        self.assertEqual(kwargs["device_name"], "")


if __name__ == "__main__":
    unittest.main()
