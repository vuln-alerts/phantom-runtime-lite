"""
audio/vad.py
============
VAD orchestration for the Phantom Conversational Runtime.

Relocated from phantom_conversational_runtime_v22.py (M5 Runtime Core Separation).
Original location: vad_loop() delegation path annotated [MODULE: audio.vad] in v22.

EXPORTED API:
  VADOrchestrator — drives the VAD frame accumulation loop
"""

import queue
import threading
from typing import Callable

import numpy as np

from audio.vad_buffering import VADBuffer

_TAIL_PADDING_SEC      = 0.80
_MAX_MANUAL_BUFFER_SEC = 30.0


class VADOrchestrator:
    """
    Drives the VAD frame accumulation loop for the Phantom Conversational Runtime.

    Verbatim extraction of vad_loop() delegation path from v22.
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
                continue
            flat   = block.flatten()
            silent = self._is_silent(flat)
            audio  = self._vad_buf.process_frame(flat, silent)
            if audio is not None:
                self._on_segment_ready(audio)
