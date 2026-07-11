"""
runtime_trace.py
=================
Shared, debug-only structured trace logger for the Runtime Pipeline stall
investigation (Session ID / Event ID / Timestamp per pipeline stage).

Built on top of the existing, unmodified runtime.runtime_logger.RuntimeLogger
-- this module adds no new logging mechanism, only a stage-tagged, opt-in
wrapper around it. Emits nothing unless PHANTOM_TRACE=1 is set; with the
env var unset (the default / production state), every call in this module
is a no-op and no other module's behavior changes.

Does not alter any Runtime decision logic -- observational only. Each
call site importing this module is expected to call emit() purely for
side-effect logging, never to branch on its return value in a way that
changes control flow.

Session/event identity, by design:
  SESSION_ID   -- this process's own identity (pid-based, or overridden via
                  PHANTOM_TRACE_SESSION_ID for the one case where a caller
                  already knows the correlating id -- see
                  runtime.transport_gateway, which tags its own trace lines
                  with the spawned Runtime child's pid so the parent Shell
                  process and the child Runtime process share one session
                  id even though they are different OS processes).
  next_event_id -- a per-stage-name monotonic counter, not propagated
                  across stages. This project's components run across
                  independent OS processes/threads (Client, Shell,
                  Runtime child) with no existing wire-level correlation
                  id; adding one would mean changing the WebSocket/typed
                  event contract, which this investigation must not do.
                  Instead, each stage's own sequence-number-plus-timestamp
                  is enough to answer the diagnostic question this trace
                  exists for: which stage's counter stops advancing first.

EXPORTED API:
  enabled() -> bool
  next_event_id(stage) -> str
  emit(stage, session_id=None, event_id="", **fields) -> None
"""

import itertools
import os
import sys
import threading

from runtime.runtime_logger import RuntimeLogger

_ENABLED = os.getenv("PHANTOM_TRACE") == "1"
_TRACE_FILE = os.getenv("PHANTOM_TRACE_FILE", "").strip()

SESSION_ID = os.getenv("PHANTOM_TRACE_SESSION_ID") or f"pid{os.getpid()}"

_output = sys.stderr
if _ENABLED and _TRACE_FILE:
    _output = open(_TRACE_FILE, "a", encoding="utf-8")

_logger = RuntimeLogger(level="DEBUG", json_mode=True, output=_output)

_counters: dict = {}
_counters_lock = threading.Lock()


def enabled() -> bool:
    return _ENABLED


def next_event_id(stage: str) -> str:
    with _counters_lock:
        n = _counters.get(stage, 0) + 1
        _counters[stage] = n
    return f"{SESSION_ID}-{stage}-{n}"


def emit(stage: str, session_id: str = None, event_id: str = "", **fields) -> None:
    if not _ENABLED:
        return
    # `fields` is nested under one fixed key, never splatted into
    # RuntimeLogger.event()'s own **kwargs -- a caller-supplied field
    # sharing a name with one of event()'s own parameters (e.g. a field
    # literally named "event_type", which _emit_event's caller does use)
    # would otherwise raise TypeError: got multiple values for argument.
    _logger.event(stage, session_id=session_id or SESSION_ID, event_id=event_id, fields=fields)
