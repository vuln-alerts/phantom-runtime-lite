"""
audio.vad_buffering
====================
VAD Buffering — Phantom Conversational Runtime.

Extracted from phantom_conversational_runtime_v22.py (M2 High-Risk Extraction).
Original location: _manual_buf_* globals and functions, _recording_active,
_vad_force_flush_* events, _vad_inline_active flag, _TAIL_PADDING_* constants,
and inline VAD fallback frame accumulation state annotated [MODULE: audio.vad]
in v22.

Public API
----------
VADBuffer  -- single owner of all VAD buffering state

This module is independently importable.
It carries no dependency on the main runtime file.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, List, Optional

import numpy as np


class VADBuffer:
    """
    VAD Buffering — Phantom Conversational Runtime.

    Single mutable owner of:
    - manual audio buffer (manual-flush mode segments)
    - recording gate (push-to-buffer enable/disable)
    - force-flush coordination events (inline VAD ↔ manual flush)
    - inline VAD fallback frame accumulation state

    Instantiated once at runtime startup.
    Parent runtime holds one reference and delegates all buffering operations.
    No other module may own or mirror buffer state.
    """

    def __init__(
        self,
        sample_rate:           int,
        pre_buffer_blocks:     int,
        min_samples:           int,
        max_samples:           int,
        silence_blocks:        int,
        max_manual_buffer_sec: float,
        tail_padding_sec:      float,
        info_fn:               Callable[[str], None],
        warn_fn:               Callable[[str], None],
        print_fn:              Callable[..., None],
        green:                 str,
        bold:                  str,
        gray:                  str,
        reset:                 str,
    ) -> None:
        # ── Configuration (immutable after construction) ──────────────────
        self._sample_rate           = sample_rate
        self._pre_buffer_blocks     = pre_buffer_blocks
        self._min_samples           = min_samples
        self._max_samples           = max_samples
        self._silence_blocks        = silence_blocks
        self._max_manual_buffer_sec = max_manual_buffer_sec
        self._tail_padding_sec      = tail_padding_sec
        self._tail_padding_samples  = int(sample_rate * tail_padding_sec)

        # ── Callbacks ─────────────────────────────────────────────────────
        self._info_fn  = info_fn
        self._warn_fn  = warn_fn
        self._print_fn = print_fn
        self._green    = green
        self._bold     = bold
        self._gray     = gray
        self._reset    = reset

        # ── Manual buffer state ───────────────────────────────────────────
        self.lock:           threading.Lock = threading.Lock()
        self.audio_buffer:   List           = []   # list[np.ndarray]
        self._duration:      float          = 0.0
        self._segments:      int            = 0
        self._last_flush_ts: str            = "—"

        # ── Force-flush coordination (inline VAD ↔ manual flush) ──────────
        self._force_flush_event: threading.Event = threading.Event()
        self._force_flush_done:  threading.Event = threading.Event()

        # True only while the inline VAD fallback loop is running
        self.inline_active: bool = False

        # ── Recording gate (push-to-buffer enable/disable) ────────────────
        self.recording_active: threading.Event = threading.Event()
        self.recording_active.set()   # starts ON — backward-compatible

        # ── Inline VAD fallback frame accumulation ────────────────────────
        self._blocks:         List  = []
        self._total_samples:  int   = 0
        self._silence_streak: int   = 0
        self._has_speech:     bool  = False
        self._pre_buf:        deque = deque(maxlen=pre_buffer_blocks)

    # ── Manual buffer operations ──────────────────────────────────────────────

    def append_segment(self, audio: np.ndarray) -> None:
        """
        Append one finalized audio segment to the manual buffer.

        Enforces max_manual_buffer_sec hard limit:
          - At 20s: warn operator
          - At ceiling: warn and truncate oldest segment
        """
        dur = len(audio) / self._sample_rate
        with self.lock:
            if self._duration + dur > self._max_manual_buffer_sec:
                if self.audio_buffer:
                    oldest     = self.audio_buffer.pop(0)
                    oldest_dur = len(oldest) / self._sample_rate
                    self._duration -= oldest_dur
                    self._segments = max(0, self._segments - 1)
                    self._warn_fn(
                        f"[buf] MAX_MANUAL_BUFFER_SEC ({self._max_manual_buffer_sec:.0f}s) reached — "
                        f"oldest segment ({oldest_dur:.1f}s) dropped. Press 'g' to flush."
                    )
            self.audio_buffer.append(audio)
            self._duration += dur
            self._segments += 1
            seg_n = self._segments
            dur_n = self._duration

        self._info_fn(
            f"[seg] +{dur:.2f}s "
            f"total={dur_n:.1f}s "
            f"segments={seg_n}"
        )
        if dur_n >= 20.0:
            self._warn_fn(f"[buf] {dur_n:.1f}s buffered (segments={seg_n}) — consider flushing soon")
        else:
            self._info_fn(f"[buf] {dur_n:.1f}s (segments={seg_n}) — press g to process")

    def flush(self) -> Optional[np.ndarray]:
        """
        Atomically drain the manual buffer and return merged audio.

        When the inline VAD loop is active, signals it to force-finalize any
        in-progress audio block so the tail is not lost before the merge.
        Wait up to 1.0 s (covers queue.get timeout of 0.5 s).
        Returns None if the buffer is empty.
        """
        if self.inline_active:
            self._force_flush_done.clear()
            self._force_flush_event.set()
            self._force_flush_done.wait(timeout=1.0)

        with self.lock:
            if not self.audio_buffer:
                return None
            merged = np.concatenate(self.audio_buffer)
            self.audio_buffer.clear()
            self._duration = 0.0
            self._segments = 0
        self._last_flush_ts = time.strftime("%H:%M:%S")
        return merged

    def status(self) -> str:
        """Return a human-readable buffer status string."""
        with self.lock:
            return f"{self._duration:.1f}s  segments={self._segments}"

    def get_stats(self) -> tuple:
        """
        Return (duration_sec, segment_count, last_flush_ts).

        duration and segments are read under lock; last_flush_ts is read
        outside lock to match original access pattern.
        """
        with self.lock:
            dur  = self._duration
            segs = self._segments
        return dur, segs, self._last_flush_ts

    def show_recording_status(self) -> None:
        """Display recording gate status with current buffer duration."""
        with self.lock:
            dur  = self._duration
            segs = self._segments
        if self.recording_active.is_set():
            self._print_fn(
                f"{self._green}{self._bold}● RECORDING{self._reset}  "
                f"{self._gray}buf={dur:.1f}s (segments={segs}){self._reset}"
            )
        else:
            self._print_fn(
                f"{self._gray}○ IDLE{self._reset}      "
                f"{self._gray}buf={dur:.1f}s (segments={segs}){self._reset}"
            )

    # ── Inline VAD fallback frame processing ──────────────────────────────────

    def handle_force_flush(
        self,
        manual_flush_enabled: bool,
        recording_is_set:     bool,
    ) -> bool:
        """
        Check and handle a force-flush event from the manual flush path.

        Called each iteration of the inline VAD loop before queue.get.
        Returns True if the event was handled (caller should continue the loop).
        """
        if not self._force_flush_event.is_set():
            return False
        self._force_flush_event.clear()
        if self._has_speech and self._blocks:
            self._info_fn("[flush] force_finalize_current_audio")
            forced     = np.concatenate(self._blocks)
            forced_dur = len(forced) / self._sample_rate
            self._reset_inline()
            if manual_flush_enabled and recording_is_set:
                self.append_segment(forced)
            self._info_fn(f"[seg] finalized reason=manual dur={forced_dur:.2f}s")
        self._force_flush_done.set()
        return True

    def process_frame(
        self,
        flat:   np.ndarray,
        silent: bool,
    ) -> Optional[np.ndarray]:
        """
        Process one audio frame through the inline VAD buffer.

        Accumulates speech frames and pre-speech silence frames.
        Returns a finalized audio segment when an utterance boundary is reached,
        or None when accumulation should continue.

        Tail padding is appended on force-flush to prevent Whisper from seeing
        an abrupt speech cut-off at the max-samples boundary.
        """
        if silent:
            self._silence_streak += 1
            if not self._has_speech:
                self._pre_buf.append(flat)
        else:
            if not self._has_speech and self._pre_buf:
                self._blocks.extend(self._pre_buf)
                self._total_samples += sum(len(b) for b in self._pre_buf)
                self._pre_buf.clear()
            self._has_speech     = True
            self._silence_streak = 0

        self._blocks.append(flat)
        self._total_samples += len(flat)

        force_flush   = self._total_samples >= self._max_samples
        silence_flush = (
            self._has_speech
            and self._silence_streak >= self._silence_blocks
            and self._total_samples  >= self._min_samples
        )

        if not (force_flush or silence_flush):
            return None

        chunk_had_speech = self._has_speech
        audio            = np.concatenate(self._blocks)

        if force_flush:
            flush_reason = "force"
            if chunk_had_speech:
                tail  = np.zeros(self._tail_padding_samples, dtype=audio.dtype)
                audio = np.concatenate([audio, tail])
                self._info_fn(f"[flush] tail_padding_sec={self._tail_padding_sec:.2f}")
        else:
            flush_reason = "silence"

        self._reset_inline()

        if not chunk_had_speech:
            return None

        self._info_fn(f"[seg] finalized reason={flush_reason} dur={len(audio)/self._sample_rate:.2f}s")
        return audio

    # ── Internal ──────────────────────────────────────────────────────────────

    def _reset_inline(self) -> None:
        """Reset inline VAD frame accumulation state to initial values."""
        self._blocks         = []
        self._total_samples  = 0
        self._silence_streak = 0
        self._has_speech     = False
        self._pre_buf.clear()
