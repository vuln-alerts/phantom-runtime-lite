"""
runtime_client/debug_sink.py
================================
Production Verification investigation instrumentation (TEMPORARY).

Added to make Production Verification easier to run and hand back to
Claude Code for Root Cause Analysis (P5-4 Adaptive Runtime Calibration
Startup Calibration investigation -- see investigation notes). Not part
of the calibration algorithm and not Production Logic: this module only
decides *where* the PHANTOM_CALIBRATION_DEBUG=1 debug lines
calibration.py's _debug_log() and main.py's _calibration_debug_log()
already emit also get written to -- unchanged stdout output, plus
(when a session log has been opened) a plain-text copy in a file under
logs/. It has no opinion on calibration outcome and computes nothing --
callers pass it fully-formed strings.

Delete this module, its two call sites (calibration.py's _debug_log,
main.py's _calibration_debug_log), and main.py's --production-verification
wiring together once the Calibration Failed root cause is found -- see
those call sites' own comments.

EXPORTED API:
  is_enabled()             -- True if PHANTOM_CALIBRATION_DEBUG=1
  open_session_log(path)   -- start tee-ing write() to this file, in
                               addition to callers' own existing stdout
                               output (print()/show_info()) -- unchanged
  session_log_path()       -- the currently open path, or None
  write(message)           -- append one line to the open session log
                               file, if any; no-op otherwise (does NOT
                               print -- callers already do that
                               themselves via their own existing
                               print()/show_info() call, unchanged)
  write_file(path, content) -- one-shot file write (root_cause_summary.txt),
                               creating the parent directory if needed
"""

import os
import threading
from typing import Optional, TextIO

_ENV_VAR = "PHANTOM_CALIBRATION_DEBUG"

_lock = threading.Lock()
_file: Optional[TextIO] = None
_file_path: Optional[str] = None


def is_enabled() -> bool:
    return os.getenv(_ENV_VAR) == "1"


def open_session_log(path: str) -> None:
    """Opens path in append mode and starts tee-ing write() to it.
    Creates the parent directory if it doesn't exist. Closes any
    previously-open session log first."""
    global _file, _file_path
    with _lock:
        if _file is not None:
            _file.close()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _file = open(path, "a", encoding="utf-8")
        _file_path = path


def session_log_path() -> Optional[str]:
    return _file_path


def write(message: str) -> None:
    """Appends message + newline to the open session log file. No-op if
    no session log is open (e.g. PHANTOM_CALIBRATION_DEBUG=1 was set
    directly, without --production-verification)."""
    with _lock:
        if _file is not None:
            _file.write(message + "\n")
            _file.flush()


def write_file(path: str, content: str) -> None:
    """One-shot write of content to path (e.g. root_cause_summary.txt),
    creating the parent directory if needed. Independent of
    open_session_log()/write() above -- always overwrites path."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
