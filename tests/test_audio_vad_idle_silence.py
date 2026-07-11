"""
tests/test_audio_vad_idle_silence.py
=====================================
Unit tests for the Speech Gate x Server VAD design-mismatch fix:

- Client Speech Gate (runtime_client/audio_bridge.py) never forwards a
  block while the caller is silent, so the Server VAD (audio/vad.py's
  VADOrchestrator + audio/vad_buffering.py's VADBuffer) never receives
  the silent frames its frame-count-based silence_streak depends on.
  Before this fix, silence_flush was therefore unreachable and every
  segment ended via force_flush at max_samples.
- VADBuffer.check_idle_timeout() is the wall-clock counterpart to
  process_frame's silence_streak: it finalizes an in-progress speech
  segment as reason=silence once idle_sec (time since the last block
  actually arrived, tracked by VADOrchestrator.run) clears
  silence_timeout_sec, without requiring a synthetic silent frame.
- VADOrchestrator.run() drives check_idle_timeout() from its existing
  queue.get(timeout=0.5) Empty branch -- no new queue, thread, or
  wire-level contract.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a required dependency (see
tests/test_runtime_client_audio_bridge.py's module docstring).
"""

import os
import queue
import sys
import threading
import time
import unittest

import numpy as np

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from audio.vad import VADOrchestrator
from audio.vad_buffering import VADBuffer


def _buf(min_samples=800, max_samples=16000, silence_blocks=2, sample_rate=16000):
    return VADBuffer(
        sample_rate=sample_rate,
        pre_buffer_blocks=5,
        min_samples=min_samples,
        max_samples=max_samples,
        silence_blocks=silence_blocks,
        max_manual_buffer_sec=30.0,
        tail_padding_sec=0.8,
        info_fn=lambda *_: None,
        warn_fn=lambda *_: None,
        print_fn=lambda *_, **__: None,
        green="", bold="", gray="", reset="",
    )


def _speech_block(n=1600):
    rng = np.random.default_rng(seed=1)
    return rng.integers(-5000, 5000, size=n, dtype=np.int16)


class TestCheckIdleTimeout(unittest.TestCase):
    def test_no_speech_yet_returns_none(self):
        b = _buf()
        self.assertIsNone(b.check_idle_timeout(idle_sec=10.0, silence_timeout_sec=0.25))

    def test_idle_below_threshold_returns_none(self):
        b = _buf()
        b.process_frame(_speech_block(), silent=False)
        self.assertIsNone(b.check_idle_timeout(idle_sec=0.1, silence_timeout_sec=0.25))

    def test_idle_below_min_samples_returns_none(self):
        # min_samples set high so one 1600-sample block isn't enough yet.
        b = _buf(min_samples=100_000)
        b.process_frame(_speech_block(), silent=False)
        self.assertIsNone(b.check_idle_timeout(idle_sec=1.0, silence_timeout_sec=0.25))

    def test_idle_past_threshold_finalizes_as_silence(self):
        b = _buf(min_samples=800)
        b.process_frame(_speech_block(), silent=False)
        audio = b.check_idle_timeout(idle_sec=0.3, silence_timeout_sec=0.25)
        self.assertIsNotNone(audio)
        self.assertEqual(len(audio), 1600)

    def test_finalizing_resets_inline_state(self):
        b = _buf(min_samples=800)
        b.process_frame(_speech_block(), silent=False)
        b.check_idle_timeout(idle_sec=0.3, silence_timeout_sec=0.25)
        # Buffer state should be fully reset -- a second speech block starts
        # a fresh segment, not a continuation of the flushed one.
        self.assertIsNone(b.check_idle_timeout(idle_sec=10.0, silence_timeout_sec=0.25))

    def test_never_fires_without_calling_it(self):
        """No behavior change for callers that never call check_idle_timeout
        (e.g. any other VADBuffer consumer): process_frame's own
        force_flush/silence_flush logic is untouched."""
        b = _buf(max_samples=1600)
        audio = b.process_frame(_speech_block(), silent=False)
        self.assertIsNotNone(audio)  # force_flush at max_samples, as before


class TestVADOrchestratorIdleDriven(unittest.TestCase):
    """End-to-end: Client Speech Gate withholds all audio after a burst of
    speech (as it does in production); the Server VAD must still finalize
    the segment via the idle timeout, not just sit waiting for force_flush."""

    def test_silence_reason_reachable_when_no_further_blocks_arrive(self):
        q: "queue.Queue" = queue.Queue()
        segments = []
        orchestrator = VADOrchestrator(
            sample_rate=16000,
            block_size=1600,
            rms_threshold=120,
            min_samples=800,
            max_samples=16_000_000,  # effectively unreachable -- isolates silence path
            silence_blocks=2,        # -> silence_timeout_sec = 2*1600/16000 = 0.2s
            pre_buffer_blocks=5,
            audio_queue=q,
            on_segment_ready=segments.append,
            on_info=lambda *_: None,
            on_warn=lambda *_: None,
        )
        self.assertAlmostEqual(orchestrator._silence_timeout_sec, 0.2, places=6)

        # Simulate the Client Speech Gate: one loud block, then nothing --
        # exactly what happens when the caller falls silent.
        q.put(_speech_block().reshape(-1, 1))

        shutdown = threading.Event()
        t = threading.Thread(target=orchestrator.run, args=(shutdown,), daemon=True)
        t.start()
        try:
            deadline = time.monotonic() + 3.0
            while not segments and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            shutdown.set()
            t.join(timeout=2.0)

        self.assertEqual(len(segments), 1)
        self.assertEqual(len(segments[0]), 1600)


if __name__ == "__main__":
    unittest.main()
