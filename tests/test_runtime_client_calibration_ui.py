"""
tests/test_runtime_client_calibration_ui.py
=============================================
Unit tests for P5-4 Adaptive Runtime Calibration Runtime UI:
src/runtime_client/typed_event.py's calibration screens.

Phase 3 (design doc section 8.1-8.4):
  show_calibration_start     -- section 8.1 (startup)
  show_calibration_progress  -- section 8.2 (in progress)
  show_calibration_complete  -- section 8.3 (complete)
  show_calibration_failed    -- section 8.4 (failed / fallback)

Phase 4 (design doc section 8.5):
  show_environment_changed   -- section 8.5 (re-calibration in progress)

These are pure renderers -- every number is passed in by the caller, so
each test supplies literal values (mirroring the design doc's own
worked examples where practical) and asserts the rendered text contains
the fixed strings/labels the design doc mandates. Neither Phase 3 nor
Phase 4 implements any 'c'-key handling or the automatic drift-detection
trigger that would decide *when* to call show_environment_changed (see
Implementation Plan's Phase 4 boundary, and calibration.py's
RecalibrationController docstring for why FR-6's condition specifically
is not implemented) -- no tests for those here, only for what the
screen renders once called.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.typed_event import (
    show_calibration_complete,
    show_calibration_failed,
    show_calibration_progress,
    show_calibration_start,
    show_environment_changed,
)


class TestShowCalibrationStart(unittest.TestCase):
    def test_shows_header_and_zero_of_window_blocks(self):
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_start(window_blocks=25)
        out = buf.getvalue()
        self.assertIn("🎤 Audio Calibration", out)
        self.assertIn("環境ノイズを測定しています", out)
        self.assertIn("サンプル取得中: 0/25 blocks", out)

    def test_respects_custom_window_blocks(self):
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_start(window_blocks=10)
        self.assertIn("サンプル取得中: 0/10 blocks", buf.getvalue())


class TestShowCalibrationProgress(unittest.TestCase):
    def test_matches_design_doc_worked_example(self):
        # design doc section 8.2's own example: 15/25 blocks, 1.5s/2.5s,
        # provisional noise floor 174 RMS.
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_progress(
                sample_count=15,
                window_blocks=25,
                elapsed_seconds=1.5,
                window_seconds=2.5,
                noise_floor_estimate=174,
            )
        out = buf.getvalue()
        self.assertIn("サンプル取得中: 15/25 blocks", out)
        self.assertIn("1.5s / 2.5s", out)
        self.assertIn("現在の推定 Noise Floor: 174 RMS (暫定)", out)
        # 1.5/2.5 = 60% of a 10-wide bar -> 6 filled, 4 empty
        self.assertIn("■■■■■■□□□□", out)

    def test_omits_noise_floor_line_when_none(self):
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_progress(
                sample_count=0,
                window_blocks=25,
                elapsed_seconds=0.0,
                window_seconds=2.5,
                noise_floor_estimate=None,
            )
        self.assertNotIn("Noise Floor", buf.getvalue())

    def test_progress_bar_never_overfills_past_window(self):
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_progress(
                sample_count=25,
                window_blocks=25,
                elapsed_seconds=3.0,  # past the window
                window_seconds=2.5,
                noise_floor_estimate=None,
            )
        self.assertIn("■■■■■■■■■■", buf.getvalue())  # fully filled, not overflowed


class TestShowCalibrationComplete(unittest.TestCase):
    def test_matches_design_doc_worked_example(self):
        # design doc section 8.3's own example values.
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_complete(
                noise_floor=182,
                speech_gate=546,
                sample_count=25,
                percentile=90,
                multiplier=3.0,
                microphone_name="USB Audio Device",
            )
        out = buf.getvalue()
        self.assertIn("✓ Calibration Complete", out)
        self.assertIn("Noise Floor  : 182 RMS  (p90, 25 samples)", out)
        self.assertIn("Speech Gate  : 546 RMS  (floor x 3)", out)
        self.assertIn("Microphone   : USB Audio Device", out)
        self.assertIn("Recalibrate  : press 'c' anytime", out)
        self.assertIn("● RECORDING", out)
        self.assertIn("(gate: 546 RMS)", out)

    def test_falls_back_to_system_default_label_when_no_mic_name(self):
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_complete(
                noise_floor=100,
                speech_gate=300,
                sample_count=25,
                percentile=90,
                multiplier=3.0,
                microphone_name="",
            )
        self.assertIn("Microphone   : (system default)", buf.getvalue())


class TestShowCalibrationFailed(unittest.TestCase):
    def test_matches_design_doc_worked_example(self):
        # design doc section 8.4's own example: 3 attempts out of 3, 900
        # RMS fallback gate.
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_failed(attempts=3, max_attempts=3, fallback_gate=900)
        out = buf.getvalue()
        self.assertIn("⚠ Calibration Incomplete", out)
        self.assertIn("3回中3回、静寂区間中に音声を検出しました", out)
        self.assertIn("Fallback Gate : 900 RMS  (保守的推定・未確定)", out)
        self.assertIn("この値は実測ではなく安全側のフォールバックです", out)
        self.assertIn("静かな環境で 'c' を押すと再測定できます", out)

    def test_value_labeled_as_estimate_not_measured(self):
        # AC-10 / design doc section 9.1: must never read as a silent,
        # confirmed value.
        with redirect_stdout(io.StringIO()) as buf:
            show_calibration_failed(attempts=3, max_attempts=3, fallback_gate=700)
        out = buf.getvalue()
        self.assertIn("推定", out)
        self.assertNotIn("Speech Gate  :", out)  # that label is reserved for a confirmed value


class TestShowEnvironmentChanged(unittest.TestCase):
    def test_matches_design_doc_worked_example(self):
        # design doc section 8.5's own example: 3% -> 96% reject rate,
        # 0.5s/1.5s elapsed.
        with redirect_stdout(io.StringIO()) as buf:
            show_environment_changed(
                previous_reject_rate=3,
                current_reject_rate=96,
                elapsed_seconds=0.5,
                window_seconds=1.5,
            )
        out = buf.getvalue()
        self.assertIn("⟳ Environment Changed", out)
        self.assertIn("直近10秒で棄却率が急上昇 (3% -> 96%)", out)
        self.assertIn("マイクまたは環境が変化した可能性", out)
        self.assertIn("裏で再測定します", out)
        self.assertIn("0.5s / 1.5s", out)
        self.assertIn("録音は継続中 (発話を止める必要はありません)", out)
        # 0.5/1.5 = 33% of a 10-wide bar -> 3 filled, 7 empty
        self.assertIn("■■■□□□□□□□", out)

    def test_progress_bar_never_overfills_past_window(self):
        with redirect_stdout(io.StringIO()) as buf:
            show_environment_changed(
                previous_reject_rate=3,
                current_reject_rate=96,
                elapsed_seconds=2.0,  # past the 1.5s window
                window_seconds=1.5,
            )
        self.assertIn("■■■■■■■■■■", buf.getvalue())  # fully filled, not overflowed

    def test_recording_continues_note_always_present(self):
        with redirect_stdout(io.StringIO()) as buf:
            show_environment_changed(
                previous_reject_rate=5,
                current_reject_rate=80,
                elapsed_seconds=0.0,
                window_seconds=1.5,
            )
        self.assertIn("録音は継続中", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
