"""
runtime/logging.py
===================
Structured runtime logging for the Phantom Conversational Runtime.

DESIGN PHILOSOPHY:
  - No heavy frameworks (no loguru, no structlog, no handlers hierarchy)
  - Structured output with consistent format
  - Optional JSON mode for log aggregation / future dashboard
  - Level filtering: DEBUG / INFO / WARN / ERROR
  - Replay-safe timestamps on every line
  - Thread-safe (single write lock)
  - All output goes to stderr by default (stdout is reserved for user-facing display)

EXPORTED API:
  RuntimeLogger(level, json_mode, print_fn)
  logger.info(msg)
  logger.warn(msg)
  logger.error(msg, exc=None)
  logger.debug(msg)
  logger.event(event_type, **fields)   — structured event log
"""

import json
import sys
import threading
import time
from typing import Any, Callable, Optional


# Log levels as integers for fast comparison
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}


class RuntimeLogger:
    """
    Structured, thread-safe runtime logger.

    All messages include: ISO timestamp, level, and message.
    event() adds arbitrary fields for structured log aggregation.
    JSON mode emits each log line as a valid JSON object.
    """

    def __init__(
        self,
        level:    str = "INFO",
        json_mode: bool = False,
        output:    Any  = sys.stderr,
        lock:      Optional[threading.Lock] = None,
    ) -> None:
        self._min_level = _LEVELS.get(level.upper(), 20)
        self._json      = json_mode
        self._output    = output
        self._lock      = lock or threading.Lock()

    def _write(self, level: str, msg: str, fields: Optional[dict] = None) -> None:
        if _LEVELS.get(level, 20) < self._min_level:
            return
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        if self._json:
            row = {"ts": ts, "level": level, "msg": msg}
            if fields:
                row.update(fields)
            line = json.dumps(row, ensure_ascii=False)
        else:
            field_str = ""
            if fields:
                field_str = "  " + "  ".join(f"{k}={v}" for k, v in fields.items())
            line = f"{ts} [{level:5s}] {msg}{field_str}"
        with self._lock:
            print(line, file=self._output, flush=True)

    def debug(self, msg: str, **fields) -> None:
        self._write("DEBUG", msg, fields or None)

    def info(self, msg: str, **fields) -> None:
        self._write("INFO",  msg, fields or None)

    def warn(self, msg: str, **fields) -> None:
        self._write("WARN",  msg, fields or None)

    def error(self, msg: str, exc: Optional[Exception] = None, **fields) -> None:
        if exc:
            fields["exc"] = str(exc)
        self._write("ERROR", msg, fields or None)

    def event(self, event_type: str, **fields) -> None:
        """
        Structured event log — used for latency, state changes, queue events.
        Always emits at INFO level.

        Example:
          logger.event("flush_complete", dur_sec=4.2, segments=3, session_id="...")
        """
        self._write("INFO", f"[event:{event_type}]", fields or None)

    def set_level(self, level: str) -> None:
        self._min_level = _LEVELS.get(level.upper(), 20)


# Module-level default logger — writes to stderr at INFO
# Replace with RuntimeLogger(json_mode=True) for log aggregation
default_logger = RuntimeLogger(level="INFO", json_mode=False)
