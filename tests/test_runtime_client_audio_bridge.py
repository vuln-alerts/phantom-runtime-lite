"""
tests/test_runtime_client_audio_bridge.py
=============================================
Unit tests for src/runtime_client/audio_bridge.py's send gates:

- Silence gate (P5-4-1): block_rms() and AudioBridge._run_pump()'s
  decision to drop any block whose RMS falls below
  silence_rms_threshold, so the Server's VAD/Whisper never sees -- and
  can't repeatedly hallucinate on -- pure silence.
- Recording gate (P5-4-2): AudioBridge._run_pump()'s decision to drop
  every block while the caller-supplied recording_active Event is
  clear (RECORDING OFF), so audio stops reaching the Server -- and
  therefore STT/LLM/Typed Events -- the moment the operator toggles
  recording off, not just the on-screen status.
- Adaptive Speech Gate (P5-4 Phase 5 Integration): AudioBridge._run_pump()
  reading a live Speech Gate off a caller-supplied RecalibrationController
  (calibration.py, Phase 4, unmodified) instead of the fixed
  silence_rms_threshold, and falling back to that fixed threshold when
  no controller is supplied (default None -- every test above this
  section never passes one, so it also proves Phase 5 changed nothing
  about the pre-existing constructor contract).

Feeds synthetic int16 PCM blocks directly into AudioBridge's internal
_raw_queue (bypassing real sounddevice hardware) so this suite is
deterministic and CI-safe; a real hardware measurement (ambient RMS,
silence-gate effectiveness ratios) is documented separately in the
P5-4-1 investigation report, not reproduced here.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import asyncio
import os
import sys
import threading
import time
import unittest

import numpy as np

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.audio_bridge import AudioBridge, block_rms
from runtime_client.calibration import CalibrationEngine, CalibrationResult, RecalibrationController

_THRESHOLD = 120


def _silence_block(n=1600):
    return np.zeros((n, 1), dtype=np.int16)


def _loud_block(n=1600, amplitude=5000):
    rng = np.random.default_rng(seed=42)
    return rng.integers(-amplitude, amplitude, size=(n, 1), dtype=np.int16)


class TestBlockRms(unittest.TestCase):
    def test_all_zero_block_has_zero_rms(self):
        self.assertEqual(block_rms(_silence_block()), 0.0)

    def test_empty_block_has_zero_rms(self):
        self.assertEqual(block_rms(np.zeros((0, 1), dtype=np.int16)), 0.0)

    def test_constant_amplitude_block_rms_equals_amplitude(self):
        block = np.full((100, 1), 1000, dtype=np.int16)
        self.assertAlmostEqual(block_rms(block), 1000.0, places=3)

    def test_loud_block_rms_exceeds_default_threshold(self):
        self.assertGreater(block_rms(_loud_block()), _THRESHOLD)


def _make_bridge(loop, out_queue, silence_rms_threshold=_THRESHOLD, recording_active=None):
    if recording_active is None:
        recording_active = threading.Event()
        recording_active.set()  # ON by default, matching VADBuffer's own default
    bridge = AudioBridge(
        sample_rate=16000,
        channels=1,
        block_size=1600,
        device_id=None,
        loop=loop,
        out_queue=out_queue,
        on_status=lambda msg: None,
        silence_rms_threshold=silence_rms_threshold,
        recording_active=recording_active,
    )
    return bridge


class TestSilenceGate(unittest.TestCase):
    """
    Drives AudioBridge._run_pump directly against a live background
    event loop, feeding synthetic blocks into _raw_queue the same way
    AudioCapture's callback would -- verifies the gate's actual
    forwarding decision, not just block_rms() in isolation.
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.out_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=100)

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()

    def _drain_out_queue(self):
        async def _drain():
            items = []
            while not self.out_queue.empty():
                items.append(self.out_queue.get_nowait())
            return items

        return asyncio.run_coroutine_threadsafe(_drain(), self.loop).result(timeout=2)

    def test_silent_block_is_not_forwarded(self):
        bridge = _make_bridge(self.loop, self.out_queue)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            bridge._raw_queue.put(_silence_block())
            time.sleep(0.3)
            self.assertEqual(self._drain_out_queue(), [])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_loud_block_is_forwarded(self):
        bridge = _make_bridge(self.loop, self.out_queue)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            block = _loud_block()
            bridge._raw_queue.put(block)
            time.sleep(0.3)
            items = self._drain_out_queue()
            self.assertEqual(items, [block.tobytes()])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_mixed_stream_only_loud_blocks_forwarded(self):
        bridge = _make_bridge(self.loop, self.out_queue)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            loud = _loud_block()
            for block in (_silence_block(), loud, _silence_block(), _silence_block()):
                bridge._raw_queue.put(block)
            time.sleep(0.3)
            items = self._drain_out_queue()
            self.assertEqual(items, [loud.tobytes()])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_block_sent_callback_only_fires_for_forwarded_blocks(self):
        sent_count = {"n": 0}
        recording_active = threading.Event()
        recording_active.set()
        bridge = AudioBridge(
            sample_rate=16000,
            channels=1,
            block_size=1600,
            device_id=None,
            loop=self.loop,
            out_queue=self.out_queue,
            on_status=lambda msg: None,
            silence_rms_threshold=_THRESHOLD,
            recording_active=recording_active,
            on_block_sent=lambda: sent_count.__setitem__("n", sent_count["n"] + 1),
        )
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            for block in (_silence_block(), _silence_block(), _loud_block()):
                bridge._raw_queue.put(block)
            time.sleep(0.3)
            self._drain_out_queue()
            self.assertEqual(sent_count["n"], 1)
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_threshold_is_caller_supplied_not_hardcoded(self):
        # A block that is "loud" relative to a low threshold but still
        # below a stricter one -- proves the gate reads the threshold
        # from the constructor argument, not a module constant.
        rng = np.random.default_rng(seed=7)
        mid_block = rng.integers(-200, 200, size=(1600, 1), dtype=np.int16)
        rms = block_rms(mid_block)
        self.assertGreater(rms, 50)
        self.assertLess(rms, 500)

        strict_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=100)
        strict_bridge = _make_bridge(self.loop, strict_queue, silence_rms_threshold=int(rms) + 50)
        loose_bridge = _make_bridge(self.loop, self.out_queue, silence_rms_threshold=max(1, int(rms) - 50))

        strict_pump = threading.Thread(target=strict_bridge._run_pump, daemon=True)
        loose_pump = threading.Thread(target=loose_bridge._run_pump, daemon=True)
        strict_pump.start()
        loose_pump.start()
        try:
            strict_bridge._raw_queue.put(mid_block)
            loose_bridge._raw_queue.put(mid_block)
            time.sleep(0.3)

            async def _drain(q):
                items = []
                while not q.empty():
                    items.append(q.get_nowait())
                return items

            strict_items = asyncio.run_coroutine_threadsafe(_drain(strict_queue), self.loop).result(timeout=2)
            loose_items = self._drain_out_queue()
            self.assertEqual(strict_items, [])
            self.assertEqual(loose_items, [mid_block.tobytes()])
        finally:
            strict_bridge._shutdown.set()
            loose_bridge._shutdown.set()
            strict_pump.join(timeout=2)
            loose_pump.join(timeout=2)


class TestRecordingGate(unittest.TestCase):
    """
    P5-4-2: AudioBridge._run_pump() must stop forwarding blocks the
    instant recording_active is clear, and resume the instant it's set
    again -- regardless of loudness (the recording gate and the P5-4-1
    silence gate are independent checks).
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.out_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=100)

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()

    def _drain_out_queue(self):
        async def _drain():
            items = []
            while not self.out_queue.empty():
                items.append(self.out_queue.get_nowait())
            return items

        return asyncio.run_coroutine_threadsafe(_drain(), self.loop).result(timeout=2)

    def test_recording_on_forwards_loud_audio(self):
        recording_active = threading.Event()
        recording_active.set()
        bridge = _make_bridge(self.loop, self.out_queue, recording_active=recording_active)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            block = _loud_block()
            bridge._raw_queue.put(block)
            time.sleep(0.3)
            self.assertEqual(self._drain_out_queue(), [block.tobytes()])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_recording_off_blocks_forwarding_even_when_loud(self):
        recording_active = threading.Event()  # starts clear -- OFF
        bridge = _make_bridge(self.loop, self.out_queue, recording_active=recording_active)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            for _ in range(3):
                bridge._raw_queue.put(_loud_block())
            time.sleep(0.3)
            self.assertEqual(self._drain_out_queue(), [])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_off_then_on_resumes_forwarding(self):
        recording_active = threading.Event()
        recording_active.set()
        bridge = _make_bridge(self.loop, self.out_queue, recording_active=recording_active)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            # ON: forwarded.
            first = _loud_block(amplitude=4000)
            bridge._raw_queue.put(first)
            time.sleep(0.2)
            self.assertEqual(self._drain_out_queue(), [first.tobytes()])

            # OFF: dropped, no matter how loud or how many.
            recording_active.clear()
            for _ in range(3):
                bridge._raw_queue.put(_loud_block(amplitude=4500))
            time.sleep(0.2)
            self.assertEqual(self._drain_out_queue(), [])

            # ON again: forwarding resumes immediately, no leftover
            # OFF-period blocks trickle out afterwards.
            recording_active.set()
            last = _loud_block(amplitude=4800)
            bridge._raw_queue.put(last)
            time.sleep(0.2)
            self.assertEqual(self._drain_out_queue(), [last.tobytes()])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_recording_gate_independent_of_silence_gate(self):
        # ON + silent -> still dropped (P5-4-1 gate).
        # OFF + loud   -> still dropped (P5-4-2 gate).
        # Neither gate can substitute for the other.
        recording_active = threading.Event()
        recording_active.set()
        bridge = _make_bridge(self.loop, self.out_queue, recording_active=recording_active)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            bridge._raw_queue.put(_silence_block())
            recording_active.clear()
            bridge._raw_queue.put(_loud_block())
            time.sleep(0.3)
            self.assertEqual(self._drain_out_queue(), [])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_block_sent_callback_does_not_fire_while_recording_off(self):
        sent_count = {"n": 0}
        recording_active = threading.Event()  # OFF
        bridge = AudioBridge(
            sample_rate=16000,
            channels=1,
            block_size=1600,
            device_id=None,
            loop=self.loop,
            out_queue=self.out_queue,
            on_status=lambda msg: None,
            silence_rms_threshold=_THRESHOLD,
            recording_active=recording_active,
            on_block_sent=lambda: sent_count.__setitem__("n", sent_count["n"] + 1),
        )
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            for _ in range(3):
                bridge._raw_queue.put(_loud_block())
            time.sleep(0.3)
            self._drain_out_queue()
            self.assertEqual(sent_count["n"], 0)
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)


def _calibration_result(speech_gate):
    return CalibrationResult(
        success=speech_gate is not None,
        noise_floor=speech_gate,
        speech_gate=speech_gate,
        sample_count=25,
        attempts=1,
    )


class TestAdaptiveSpeechGate(unittest.TestCase):
    """
    P5-4 Phase 5 Integration: AudioBridge._run_pump() must read the
    Speech Gate from calibration_controller.active_result.speech_gate
    when a RecalibrationController is supplied, live on every block --
    not the fixed silence_rms_threshold, and not a value snapshotted
    once at construction time.
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.out_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=100)

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()

    def _drain_out_queue(self):
        async def _drain():
            items = []
            while not self.out_queue.empty():
                items.append(self.out_queue.get_nowait())
            return items

        return asyncio.run_coroutine_threadsafe(_drain(), self.loop).result(timeout=2)

    def _make_bridge(self, calibration_controller, silence_rms_threshold=_THRESHOLD):
        recording_active = threading.Event()
        recording_active.set()
        return AudioBridge(
            sample_rate=16000,
            channels=1,
            block_size=1600,
            device_id=None,
            loop=self.loop,
            out_queue=self.out_queue,
            on_status=lambda msg: None,
            silence_rms_threshold=silence_rms_threshold,
            recording_active=recording_active,
            calibration_controller=calibration_controller,
        )

    def test_gate_read_from_controller_active_result_not_static_threshold(self):
        # Controller's Speech Gate (900) is far above the static
        # silence_rms_threshold (120) supplied alongside it -- a block
        # that would pass the static gate must be rejected once a
        # controller is supplied, proving the controller wins.
        controller = RecalibrationController(CalibrationEngine(), _calibration_result(900.0))
        bridge = self._make_bridge(controller, silence_rms_threshold=120)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            rng = np.random.default_rng(seed=1)
            block = rng.integers(-500, 500, size=(1600, 1), dtype=np.int16)
            self.assertLess(block_rms(block), 900.0)
            self.assertGreater(block_rms(block), 120)
            bridge._raw_queue.put(block)
            time.sleep(0.3)
            self.assertEqual(self._drain_out_queue(), [])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_block_above_controller_gate_is_forwarded(self):
        controller = RecalibrationController(CalibrationEngine(), _calibration_result(150.0))
        bridge = self._make_bridge(controller, silence_rms_threshold=9999)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            block = np.full((1600, 1), 5000, dtype=np.int16)
            bridge._raw_queue.put(block)
            time.sleep(0.3)
            self.assertEqual(self._drain_out_queue(), [block.tobytes()])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_no_controller_preserves_static_threshold_behavior(self):
        # calibration_controller defaults to None -- byte-for-byte the
        # pre-Phase-5 behavior already covered by TestSilenceGate above.
        bridge = self._make_bridge(calibration_controller=None, silence_rms_threshold=_THRESHOLD)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            bridge._raw_queue.put(_silence_block())
            time.sleep(0.3)
            self.assertEqual(self._drain_out_queue(), [])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_gate_updates_live_after_successful_recalibration(self):
        # contamination_threshold is raised for this controller so a
        # 450-RMS "quiet" sample (still well under the 500-RMS probe
        # block below) doesn't itself count as contamination -- default
        # CalibrationEngine multiplier/clamp (1.2, [150, 2500]) -- see
        # docs/designs/ADAPTIVE_CALIBRATION_DESIGN_REVIEW.md Option 1 --
        # is used unmodified, so noise_floor=450 -> speech_gate=540.
        controller = RecalibrationController(
            CalibrationEngine(),
            _calibration_result(150.0),
            window_blocks=2,
            contamination_threshold=1000.0,
        )
        bridge = self._make_bridge(controller, silence_rms_threshold=9999)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            mid_block = np.full((1600, 1), 500, dtype=np.int16)

            # Initial gate (150) is below this block's RMS (500) -> forwarded.
            bridge._raw_queue.put(mid_block)
            time.sleep(0.2)
            self.assertEqual(self._drain_out_queue(), [mid_block.tobytes()])

            controller.begin_recalibration()
            for _ in range(2):
                controller.add_block(np.full((1600, 1), 450, dtype=np.int16))
            self.assertAlmostEqual(controller.active_result.speech_gate, 540.0, places=6)

            # Same block is now below the new, higher live gate -> dropped.
            bridge._raw_queue.put(mid_block)
            time.sleep(0.2)
            self.assertEqual(self._drain_out_queue(), [])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)

    def test_none_speech_gate_falls_back_to_static_threshold(self):
        # A CalibrationResult with speech_gate=None (e.g. a bare failed
        # observation, never handed a Fallback value) must not crash
        # the comparison -- falls back to silence_rms_threshold.
        controller = RecalibrationController(CalibrationEngine(), _calibration_result(None))
        bridge = self._make_bridge(controller, silence_rms_threshold=120)
        pump = threading.Thread(target=bridge._run_pump, daemon=True)
        pump.start()
        try:
            block = _loud_block()
            self.assertGreater(block_rms(block), 120)
            bridge._raw_queue.put(block)
            time.sleep(0.3)
            self.assertEqual(self._drain_out_queue(), [block.tobytes()])
        finally:
            bridge._shutdown.set()
            pump.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
