"""
audio/vad.py
============
VAD orchestration for Phantom Runtime Lite.

EXPORTED API:
  VADOrchestrator — drives the VAD frame accumulation loop
"""

import queue
import threading
import time
from typing import Callable

import numpy as np

from audio.vad_buffering import VADBuffer

_TAIL_PADDING_SEC      = 0.80
_MAX_MANUAL_BUFFER_SEC = 30.0


class VADOrchestrator:
    """
    Drives the VAD frame accumulation loop for Phantom Runtime Lite.
    """

    def __init__(
        self,
        sample_rate:       int,
        block_size:        int,
        rms_threshold:     int,
        min_samples:       int,
        max_samples:       int,
        silence_blocks:    int,
        pre_buffer_blocks: int,
        audio_queue:       queue.Queue,
        on_segment_ready:  Callable[[np.ndarray], None],
        on_info:           Callable[[str], None],
        on_warn:           Callable[[str], None],
    ) -> None:
        self._rms_threshold    = rms_threshold
        self._audio_queue      = audio_queue
        self._on_segment_ready = on_segment_ready

        # Client Speech Gate (runtime_client/audio_bridge.py) never forwards
        # a block during silence, so this Server VAD never sees the silent
        # frames process_frame's frame-count-based silence_streak depends
        # on -- see VADBuffer.check_idle_timeout's docstring. silence_blocks
        # is reused here (not a new config knob) to derive the equivalent
        # wall-clock idle threshold: block_size/sample_rate is the duration
        # one block represents, same unit silence_blocks was already counted
        # in, so the two stay in lockstep with whatever --silence-sec/
        # RuntimeConfig.silence_sec derived them from.
        self._silence_timeout_sec = silence_blocks * block_size / sample_rate
        self._last_block_ts       = time.monotonic()

        self._vad_buf = VADBuffer(
            sample_rate           = sample_rate,
            pre_buffer_blocks     = pre_buffer_blocks,
            min_samples           = min_samples,
            max_samples           = max_samples,
            silence_blocks        = silence_blocks,
            max_manual_buffer_sec = _MAX_MANUAL_BUFFER_SEC,
            tail_padding_sec      = _TAIL_PADDING_SEC,
            info_fn               = on_info,
            warn_fn               = on_warn,
            print_fn              = lambda *_: None,
            green                 = "",
            bold                  = "",
            gray                  = "",
            reset                 = "",
        )

    def _is_silent(self, block: np.ndarray) -> bool:
        return float(np.sqrt(np.mean(block.astype(np.float32) ** 2))) < self._rms_threshold

    def run(self, shutdown: threading.Event) -> None:
        while not shutdown.is_set():
            try:
                block = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                # No block has arrived -- either true silence (Client Speech
                # Gate is withholding audio) or the stream is idle. Either
                # way, check whether an in-progress segment has gone quiet
                # long enough to finalize as reason=silence.
                idle_sec = time.monotonic() - self._last_block_ts
                audio = self._vad_buf.check_idle_timeout(idle_sec, self._silence_timeout_sec)
                if audio is not None:
                    self._on_segment_ready(audio)
                continue
            self._last_block_ts = time.monotonic()
            flat   = block.flatten()
            silent = self._is_silent(flat)
            audio  = self._vad_buf.process_frame(flat, silent)
            if audio is not None:
                self._on_segment_ready(audio)
