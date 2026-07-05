"""
runtime/event_adapter.py
==========================
H4-10 Runtime Adapter — Contract translation only.

Translates the real wire envelope written by phantom_runtime.py's
_emit_event() (`{"version": 1, "type": ..., "timestamp": ..., "payload": {...}}`,
relayed verbatim by runtime.transport_gateway) into the frozen
docs/H4_RUNTIME_EVENT_CONTRACT.md envelope (`schema_version, event_id,
timestamp, session_id, sequence, type, payload`) consumed by
verification.verification_runtime.VerificationRuntime and every downstream
H4 component.

Field mapping decisions are fixed by
docs/H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md ("Resolved Decisions").
This module contains:
  - no business logic
  - no Verification logic
  - no Trust computation
It only renames, carries through, or (for the three identity fields only)
generates structural envelope metadata that the real Runtime does not put
on the wire today. Fields the real Runtime never emits at all (e.g.
reply.provider, reply.model, reply.finish_reason, transcript.confidence,
transcript.is_final, analysis.intent, analysis.metadata,
latency.routing_ms, status.message, error.code, error.recoverable) are
never fabricated here -- they are simply absent from the translated
payload, exactly as they are absent from the source, so
VerificationRuntime's own Gap Detection reports them honestly instead of
this module papering over the gap.

EXPORTED API:
  RuntimeEventAdapter(session_id=None) -- one instance per Runtime
                                          process/connection lifetime
  adapter.translate(raw_event: dict) -> dict  -- Contract-shaped RuntimeEvent
"""

import itertools
import uuid
from typing import Any, Dict, Optional

# Cloud Run Runtime's _emit_event envelope "version" (int) -> Contract's
# "schema_version" (str). Only version 1 exists on the wire today. Per the
# Contract ("Schema Versioning"), schema_version is str-typed and the
# current schema version is "1.0" -- future Contract versions must be
# added explicitly here when the Contract itself is updated; this adapter
# does not infer or guess unreleased Contract versions.
_SCHEMA_VERSION_MAP = {1: "1.0"}

# Per-event-type source-key -> Contract-key rename table. Any source
# payload key not listed here is carried through unchanged (EXTRA,
# informational -- permitted by the Contract's Backward Compatibility
# rule: "Consumers must ignore unknown fields"). Any Contract field not
# listed here has no source equivalent and is left absent (GAP).
_PAYLOAD_FIELD_MAP: Dict[str, Dict[str, str]] = {
    "transcript": {"text": "text", "lang": "language"},
    "reply":      {"text": "text"},
    "analysis":   {"text": "summary"},
    "latency":    {"stt_ms": "stt_ms", "gpt_ms": "provider_ms", "total_ms": "total_ms"},
    "status":     {"state": "state"},
    "error":      {"message": "message"},
}


def _translate_payload(event_type: Optional[str], raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    field_map = _PAYLOAD_FIELD_MAP.get(event_type, {})
    mapped: Dict[str, Any] = {}
    consumed = set()

    for src_key, dst_key in field_map.items():
        if src_key in raw_payload:
            mapped[dst_key] = raw_payload[src_key]
            consumed.add(src_key)

    for key, value in raw_payload.items():
        if key in consumed or key in mapped:
            continue
        mapped[key] = value

    return mapped


class RuntimeEventAdapter:
    """Pure Contract translation. See module docstring.

    One instance represents one Runtime process/connection lifetime: it
    assigns a single session_id and a monotonically increasing sequence
    number to every event it translates, since the real Runtime does not
    emit session_id/sequence/event_id itself (documented gap, see
    verification.verification_runtime module docstring and
    docs/H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md, decision 5).
    """

    def __init__(self, session_id: Optional[str] = None) -> None:
        self._session_id = session_id or str(uuid.uuid4())
        self._sequence = itertools.count(1)

    def translate(self, raw_event: Dict[str, Any]) -> Dict[str, Any]:
        """Translate one raw _emit_event envelope into a Contract-shaped
        RuntimeEvent dict. `raw_event` is read-only and never mutated.
        """
        event_type = raw_event.get("type")
        raw_payload = raw_event.get("payload") or {}
        version = raw_event.get("version")
        schema_version = _SCHEMA_VERSION_MAP.get(version, str(version))

        return {
            "schema_version": schema_version,
            "event_id": str(uuid.uuid4()),
            "timestamp": raw_event.get("timestamp"),
            "session_id": self._session_id,
            "sequence": next(self._sequence),
            "type": event_type,
            "payload": _translate_payload(event_type, raw_payload),
        }
