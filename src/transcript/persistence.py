"""
transcript.persistence
========================
Transcript Persistence engine — Phantom Runtime Lite.

Handles session-scoped transcript persistence to disk.

Public API
----------
init_session(session_dir, info_fn, warn_fn) -> bool
persist_entry(entry, state, latency_ms, warn_fn) -> None
get_session_id() -> str
close_session(info_fn) -> None

This module is independently importable.
It carries no dependency on the main runtime file.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Optional

# ── Module-owned persistence state ────────────────────────────────────────────
# _write_lock is the single unified synchronization boundary for all file writes.
# It protects concurrent append operations from multiple Runtime threads
# (reply_worker, audio capture, agent pipeline).

_write_lock:      threading.Lock = threading.Lock()
_session_id:      str            = ""
_transcript_path: Optional[str]  = None


def init_session(
    session_dir: str,
    info_fn: Optional[Callable[[str], None]] = None,
    warn_fn: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Initialize a new persistence session.

    Creates session_dir, derives a timestamp-based session ID, and sets the
    JSONL transcript path.  Returns True on success, False on failure.

    Filesystem behavior:
      - os.makedirs with exist_ok=True
      - os.path.join(session_dir, f"transcript_{stamp}.jsonl")
      - time.strftime("%Y%m%d_%H%M%S") timestamp
    """
    global _session_id, _transcript_path
    try:
        os.makedirs(session_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        _session_id = f"session_{stamp}"
        _transcript_path = os.path.join(session_dir, f"transcript_{stamp}.jsonl")
        if info_fn is not None:
            info_fn(f"[persist] transcript → {_transcript_path}")
        return True
    except Exception as e:
        if warn_fn is not None:
            warn_fn(f"[persist] Could not create session dir: {e}")
        return False


def persist_entry(
    entry: Any,
    state: str = "unknown",
    latency_ms: float = 0.0,
    warn_fn: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Append an utterance entry to the transcript JSONL file.

    Thread-safe: file I/O is protected by _write_lock (single unified boundary).
    Failure-isolated: exceptions are reported via warn_fn and do not propagate.

    JSONL row format (replay-safe, v15 contract):
      type, session_id, ts, speaker, lang, text, state, latency_ms
    """
    if not _transcript_path:
        return
    try:
        row = {
            "type":       "utterance",
            "session_id": _session_id,
            "ts":         entry.ts,
            "speaker":    entry.speaker,
            "lang":       entry.lang,
            "text":       entry.text,
            "state":      state,
            "latency_ms": round(latency_ms, 1),
        }
        with _write_lock:
            with open(_transcript_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        if warn_fn is not None:
            warn_fn(f"[persist] write error: {e}")


def get_session_id() -> str:
    """Return the current session identifier string."""
    return _session_id


def close_session(
    info_fn: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Close the persistence session.

    Each write uses a fresh file handle in append mode, so no persistent
    resources need to be released.  This call logs session completion to
    allow the Runtime to confirm clean shutdown.
    """
    if info_fn is not None and _session_id:
        info_fn(f"[persist] session closed: {_session_id}")
