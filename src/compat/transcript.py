"""
compat.transcript
=================
Compatibility forwarding — transcript domain.

Re-exports the public API of the transcript modular package.
Contains no runtime logic. Forwarding only.
"""

from transcript.persistence import init_session, persist_entry, get_session_id, close_session

__all__ = ["init_session", "persist_entry", "get_session_id", "close_session"]
