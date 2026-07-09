"""
audio/capture.py
================
Audio capture for Phantom Runtime Lite.

EXPORTED API:
  AudioCapture — sounddevice InputStream lifecycle and overflow management

P5-4 Adaptive Runtime Calibration, Phase 3 (Runtime UI) addition: the
resolved input device name -- previously only ever passed to on_info()
as a log string -- is now also kept as a plain attribute
(resolved_device_name) so a caller can read it as a value, not just
observe it in a log line. This is needed for the Calibration Complete
screen's "Microphone: <name>" field (design doc section 8.3, UI-2).
Read-only exposure only; no capture/callback behavior changes.
"""

import queue
import time
import threading
from collections import deque
from typing import Callable, Optional

import numpy as np
import sounddevice as sd


class AudioCapture:
    """
    Manages a sounddevice InputStream for Phantom Runtime Lite.

    Puts captured audio blocks into audio_queue. Tracks overflow events and
    reports them via on_overflow. Calls on_open once the stream is open.
    """

    def __init__(
        self,
        sample_rate:   int,
        channels:      int,
        dtype:         str,
        block_size:    int,
        rms_threshold: int,
        audio_queue:   queue.Queue,
        device_id:     Optional[int],
        on_status:     Callable[[str], None],
        on_overflow:   Callable[[int, float], None],
        device_name:   str = "",
        on_info:       Optional[Callable[[str], None]] = None,
        on_open:       Optional[Callable[[], None]] = None,
    ) -> None:
        self._sample_rate  = sample_rate
        self._channels     = channels
        self._dtype        = dtype
        self._block_size   = block_size
        self._audio_queue  = audio_queue
        self._device_id    = device_id
        self._device_name  = device_name
        self._on_status    = on_status
        self._on_overflow  = on_overflow
        self._on_info      = on_info or on_status
        self._on_open      = on_open

        self._last_status:    Optional[str] = None
        self._overflow_count: int           = 0
        self._overflow_lock:  threading.Lock = threading.Lock()
        self._overflow_window: deque        = deque(maxlen=100)

        # Set once run() resolves the actual device (or left as "" if no
        # device_id was given / resolution failed) -- see module docstring.
        self.resolved_device_name: str = ""

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            self._last_status = str(status)
        try:
            self._audio_queue.put_nowait(indata.copy())
        except queue.Full:
            with self._overflow_lock:
                self._overflow_count += 1
                self._overflow_window.append(time.monotonic())

    def _reset_overflow_counter(self) -> int:
        with self._overflow_lock:
            count = self._overflow_count
            self._overflow_count = 0
        return count

    def _overflow_rate_per_min(self) -> float:
        now    = time.monotonic()
        cutoff = now - 60.0
        with self._overflow_lock:
            return sum(1 for t in self._overflow_window if t > cutoff)

    def run(self, shutdown: threading.Event) -> None:
        """
        Open the audio InputStream and monitor until shutdown is set.
        Raises RuntimeError on InputStream failure.
        """
        device_kwargs: dict = {}

        if self._device_id is not None:
            device_kwargs["device"] = self._device_id
            try:
                resolved_name = sd.query_devices()[self._device_id]["name"]
            except Exception:
                resolved_name = self._device_name
            self.resolved_device_name = resolved_name
            self._on_info(f"[audio] Input device: '{resolved_name}' -> id={self._device_id}")
        elif self._device_name:
            self._on_status(
                f"[audio] Device '{self._device_name}' not found — using system default"
            )
            self._on_status("[audio] Available input devices:")
            try:
                for dev in sd.query_devices():
                    if dev["max_input_channels"] > 0:
                        self._on_status(f"  [{dev['index']}] {dev['name']}")
            except Exception:
                pass

        try:
            with sd.InputStream(
                samplerate = self._sample_rate,
                dtype      = self._dtype,
                channels   = self._channels,
                blocksize  = self._block_size,
                callback   = self._audio_callback,
                latency    = "low",
                **device_kwargs,
            ):
                if self._on_open is not None:
                    self._on_open()

                _last_overflow_report = 0
                while not shutdown.is_set():
                    time.sleep(0.1)
                    if self._last_status:
                        self._on_status(f"Audio: {self._last_status}")
                        self._last_status = None
                    with self._overflow_lock:
                        count = self._overflow_count
                    if count > 0 and time.time() - _last_overflow_report > 10:
                        rate  = self._overflow_rate_per_min()
                        total = self._reset_overflow_counter()
                        self._on_overflow(total, rate)
                        _last_overflow_report = time.time()

        except Exception as e:
            raise RuntimeError(str(e)) from e
