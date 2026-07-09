"""
tests/test_audio_capture_device_name.py
=============================================
Unit tests for P5-4 Adaptive Runtime Calibration, Phase 3 (Runtime UI)'s
one src/audio/capture.py change: AudioCapture.resolved_device_name, a
public attribute exposing the resolved input device's name as a value
(not just a log string), needed for the Calibration Complete screen's
"Microphone: <name>" field (design doc section 8.3, UI-2).

Mocks sounddevice entirely (no real mic hardware, no real InputStream) --
consistent with this project's existing test conventions (see
tests/test_runtime_client_calibration.py, tests/test_runtime_client_tts.py).

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import os
import queue
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from audio.capture import AudioCapture


def _make_capture(device_id):
    return AudioCapture(
        sample_rate=16000,
        channels=1,
        dtype="int16",
        block_size=1600,
        rms_threshold=700,
        audio_queue=queue.Queue(maxsize=10),
        device_id=device_id,
        on_status=lambda msg: None,
        on_overflow=lambda total, rate: None,
    )


class TestResolvedDeviceName(unittest.TestCase):
    def test_empty_before_run(self):
        capture = _make_capture(device_id=3)
        self.assertEqual(capture.resolved_device_name, "")

    @patch("audio.capture.sd")
    def test_resolved_from_query_devices_on_run(self, mock_sd):
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Microphone"},
            {"name": "Built-in Microphone"},
            {"name": "Built-in Microphone"},
            {"name": "USB Audio Device"},
        ]
        mock_sd.InputStream.return_value = MagicMock(
            __enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)
        )

        capture = _make_capture(device_id=3)
        shutdown = threading.Event()
        shutdown.set()  # already-set: run() opens the stream, then exits immediately

        capture.run(shutdown)

        self.assertEqual(capture.resolved_device_name, "USB Audio Device")

    @patch("audio.capture.sd")
    def test_falls_back_to_given_device_name_if_query_fails(self, mock_sd):
        mock_sd.query_devices.side_effect = RuntimeError("no devices")
        mock_sd.InputStream.return_value = MagicMock(
            __enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)
        )

        capture = AudioCapture(
            sample_rate=16000,
            channels=1,
            dtype="int16",
            block_size=1600,
            rms_threshold=700,
            audio_queue=queue.Queue(maxsize=10),
            device_id=2,
            device_name="Fallback Mic",
            on_status=lambda msg: None,
            on_overflow=lambda total, rate: None,
        )
        shutdown = threading.Event()
        shutdown.set()

        capture.run(shutdown)

        self.assertEqual(capture.resolved_device_name, "Fallback Mic")

    @patch("audio.capture.sd")
    def test_no_device_id_leaves_resolved_name_empty(self, mock_sd):
        mock_sd.InputStream.return_value = MagicMock(
            __enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)
        )

        capture = _make_capture(device_id=None)
        shutdown = threading.Event()
        shutdown.set()

        capture.run(shutdown)

        self.assertEqual(capture.resolved_device_name, "")
        mock_sd.query_devices.assert_not_called()


if __name__ == "__main__":
    unittest.main()
