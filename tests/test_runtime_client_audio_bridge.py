"""
tests/test_runtime_client_audio_bridge.py
=============================================
Unit tests for src/runtime_client/audio_bridge.py's silence gate
(P5-4-1): block_rms() and AudioBridge._run_pump()'s decision to drop
any block whose RMS falls below silence_rms_threshold, so the Server's
VAD/Whisper never sees -- and can't repeatedly hallucinate on -- pure
silence.

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


def _make_bridge(loop, out_queue, silence_rms_threshold=_THRESHOLD):
    bridge = AudioBridge(
        sample_rate=16000,
        channels=1,
        block_size=1600,
        device_id=None,
        loop=loop,
        out_queue=out_queue,
        on_status=lambda msg: None,
        silence_rms_threshold=silence_rms_threshold,
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
        bridge = AudioBridge(
            sample_rate=16000,
            channels=1,
            block_size=1600,
            device_id=None,
            loop=self.loop,
            out_queue=self.out_queue,
            on_status=lambda msg: None,
            silence_rms_threshold=_THRESHOLD,
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


if __name__ == "__main__":
    unittest.main()
