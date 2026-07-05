"""
verification/verification_runtime.py
=======================================
VerificationRuntime — H4-2 Verification Runtime.

Per docs/H4_RUNTIME_EVENT_CONTRACT.md (v1.0, Frozen) and
docs/H4_IMPLEMENTATION_PLAN.md (v1.0, Frozen), this is a read-only,
event-driven downstream Runtime. It consumes Typed Events (RuntimeEvent)
emitted by the Cloud Run Runtime and produces one VerificationResult per
event. It never executes Runtime logic, never invokes a Provider or
Whisper, never runs a Pipeline, and never accesses Runtime internal state
directly — its only input is the RuntimeEvent envelope itself, passed by
value.

Per the approved H4-2 design review:
  - Gap Detection is evaluated per RuntimeEvent / per Payload. It does not
    assume any fixed cross-type event ordering (e.g. transcript before
    reply before analysis). The only ordering rule enforced is the
    Contract's own sequence/timestamp monotonicity rule (Event Ordering,
    P4), evaluated pairwise within the same session.
  - "Invalid state transition" detection is scoped to what the Contract
    explicitly defines: a `status` event's `state` must be one of the
    five documented values. The Contract does not define a state
    transition table, so no specific state-to-state transition is
    rejected beyond that enum-membership check.
  - Fallback Detection uses only the Contract-defined `finish_reason`
    field of `reply` events — no other signal is consulted.
  - Unknown event types (any `type` outside the six Contract-defined
    types) never raise and never stop processing; they may produce a
    Warning, per the Contract's Backward Compatibility rule ("Consumers
    must ignore unknown fields") and forward compatibility with future
    event types.

Known Contract/Runtime gap (tracked, not fixed here): the Contract's
RuntimeEvent envelope defines `event_id`, `session_id`, and `sequence`,
but the current Cloud Run Runtime (src/phantom_runtime.py `_emit_event`)
does not emit them yet — only `version`/`schema_version`, `type`,
`timestamp`, and `payload` are actually on the wire today. All three are
therefore treated as Optional throughout this module; their absence never
raises, and checks that depend on them simply no-op. This gap is expected
to close by H4-10 Final Validation, per the Implementation Plan.

EXPORTED API:
  VerificationRuntime — handle(event) -> VerificationResult
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from verification.verification_result import VerificationResult

_SCHEMA_VERSION = "1.0"

# Event types defined by H4_RUNTIME_EVENT_CONTRACT.md ("Event Types").
_KNOWN_EVENT_TYPES = frozenset({
    "transcript", "reply", "analysis", "latency", "status", "error",
})

# Required payload fields per event type, per the Contract's
# "Event Payloads" section — used for missing-field Gap Detection.
_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "transcript": ("text", "language", "confidence", "is_final"),
    "reply":      ("provider", "model", "text", "finish_reason"),
    "analysis":   ("intent", "summary", "metadata"),
    "latency":    ("stt_ms", "routing_ms", "provider_ms", "total_ms"),
    "status":     ("state", "message"),
    "error":      ("code", "message", "recoverable"),
}

# Declared field types per the Contract's "Event Payloads" section — used
# for type-mismatch Gap Detection. A tuple means "any of these types".
_FIELD_TYPES: Dict[str, Dict[str, Any]] = {
    "transcript": {
        "text": str, "language": str,
        "confidence": (int, float), "is_final": bool,
    },
    "reply": {
        "provider": str, "model": str, "text": str, "finish_reason": str,
    },
    "analysis": {
        "intent": str, "summary": str, "metadata": dict,
    },
    "latency": {
        "stt_ms": (int, float), "routing_ms": (int, float),
        "provider_ms": (int, float), "total_ms": (int, float),
    },
    "status": {
        "state": str, "message": str,
    },
    "error": {
        "code": str, "message": str, "recoverable": bool,
    },
}

# Contract "status" event — supported RuntimeState values.
_SUPPORTED_STATES = frozenset({
    "STARTING", "READY", "PROCESSING", "IDLE", "STOPPED",
})

_FALLBACK_FINISH_REASON = "fallback"


class VerificationRuntime:
    """Read-only, event-driven downstream Runtime. See module docstring.

    One instance may be reused across an event stream: it keeps a small
    amount of internal bookkeeping (last-seen sequence/timestamp per
    session) purely to evaluate the Contract's ordering rule. This does
    not read or touch Cloud Run Runtime internal state — it only inspects
    RuntimeEvent envelopes handed to it.
    """

    def __init__(self) -> None:
        self._last_seen: Dict[Optional[str], Tuple[Optional[int], Optional[datetime]]] = {}

    def handle(self, event: Mapping[str, Any]) -> VerificationResult:
        """Verify one RuntimeEvent and return a new VerificationResult.

        `event` is read-only: it is never mutated, and no field of it is
        copied verbatim into the returned VerificationResult beyond a
        reference to its identity (source_event_id, session_id).
        """
        event_type = event.get("type")
        payload: Mapping[str, Any] = event.get("payload") or {}
        source_event_id = event.get("event_id")
        session_id = event.get("session_id")

        warnings: List[str] = []
        gap_reasons: List[str] = []

        if event_type not in _KNOWN_EVENT_TYPES:
            warnings.append(f"unknown event type: {event_type!r}")
        else:
            gap_reasons.extend(self._check_payload(event_type, payload))

        gap_reasons.extend(self._check_ordering(session_id, event))

        fallback_detected, fallback_reason = self._check_fallback(event_type, payload)

        gap_detected = bool(gap_reasons)
        gap_reason = "; ".join(gap_reasons) if gap_reasons else None

        if gap_detected:
            warnings.append(f"gap detected: {gap_reason}")
        if fallback_detected:
            warnings.append(f"fallback detected: {fallback_reason}")

        reliability_score = 1.0
        if gap_detected:
            reliability_score -= 0.5
        if fallback_detected:
            reliability_score -= 0.3
        reliability_score = max(0.0, min(1.0, reliability_score))
        reliable = reliability_score >= 0.5

        explanation = self._build_explanation(
            event_type, gap_detected, gap_reason, fallback_detected, fallback_reason,
        )

        return VerificationResult(
            schema_version=_SCHEMA_VERSION,
            source_event_id=source_event_id,
            session_id=session_id,
            timestamp=datetime.now(timezone.utc),
            gap_detected=gap_detected,
            gap_reason=gap_reason,
            fallback_detected=fallback_detected,
            fallback_reason=fallback_reason,
            reliable=reliable,
            reliability_score=reliability_score,
            warnings=warnings,
            explanation=explanation,
        )

    # -- Gap Detection --------------------------------------------------

    def _check_payload(self, event_type: str, payload: Mapping[str, Any]) -> List[str]:
        """Missing-payload / missing-field / type-mismatch / value-range checks.

        Scoped strictly to fields the Contract declares for `event_type`;
        no field or rule outside the Contract is introduced.
        """
        if not payload:
            return [f"missing payload for event type {event_type!r}"]

        reasons: List[str] = []

        missing = [f for f in _REQUIRED_FIELDS.get(event_type, ()) if f not in payload]
        if missing:
            reasons.append(f"missing required field(s) {missing} for event type {event_type!r}")

        reasons.extend(self._check_field_values(event_type, payload))
        return reasons

    def _check_field_values(self, event_type: str, payload: Mapping[str, Any]) -> List[str]:
        reasons: List[str] = []

        for field_name, expected_type in _FIELD_TYPES.get(event_type, {}).items():
            if field_name not in payload:
                continue
            value = payload[field_name]
            if not isinstance(value, expected_type):
                label = (
                    expected_type.__name__
                    if isinstance(expected_type, type)
                    else " or ".join(t.__name__ for t in expected_type)
                )
                reasons.append(f"{field_name!r} has unexpected type (expected {label})")

        if event_type == "transcript":
            confidence = payload.get("confidence")
            if isinstance(confidence, (int, float)) and not (0.0 <= confidence <= 1.0):
                reasons.append("'confidence' out of range [0.0, 1.0]")

        elif event_type == "latency":
            for field_name in ("stt_ms", "routing_ms", "provider_ms", "total_ms"):
                value = payload.get(field_name)
                if isinstance(value, (int, float)) and value < 0:
                    reasons.append(f"{field_name!r} is negative")

        elif event_type == "status":
            state = payload.get("state")
            if isinstance(state, str) and state not in _SUPPORTED_STATES:
                reasons.append(f"undefined state {state!r}")

        return reasons

    def _check_ordering(self, session_id: Optional[str], event: Mapping[str, Any]) -> List[str]:
        """Contract Event Ordering (P4): sequence/timestamp must not regress.

        Evaluated only pairwise within the same session_id — no cross-type
        ordering is assumed. When sequence/timestamp/session_id are absent
        from the current wire format, this simply has nothing to compare
        against and never raises.
        """
        reasons: List[str] = []
        sequence = event.get("sequence")
        timestamp = self._parse_timestamp(event.get("timestamp"))

        last_sequence, last_timestamp = self._last_seen.get(session_id, (None, None))

        if sequence is not None and last_sequence is not None and sequence < last_sequence:
            reasons.append(f"sequence regression ({sequence} < {last_sequence})")
        if timestamp is not None and last_timestamp is not None and timestamp < last_timestamp:
            reasons.append("timestamp moved backwards")

        self._last_seen[session_id] = (
            sequence if sequence is not None else last_sequence,
            timestamp if timestamp is not None else last_timestamp,
        )
        return reasons

    @staticmethod
    def _parse_timestamp(raw: Any) -> Optional[datetime]:
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    # -- Fallback Detection -----------------------------------------------

    @staticmethod
    def _check_fallback(
        event_type: Optional[str], payload: Mapping[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Contract-defined `finish_reason` only — no other signal is used."""
        if event_type != "reply":
            return False, None
        finish_reason = payload.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason.strip().lower() == _FALLBACK_FINISH_REASON:
            return True, f"finish_reason={finish_reason!r}"
        return False, None

    # -- Explanation Generation -------------------------------------------

    @staticmethod
    def _build_explanation(
        event_type: Optional[str],
        gap_detected: bool,
        gap_reason: Optional[str],
        fallback_detected: bool,
        fallback_reason: Optional[str],
    ) -> str:
        if not gap_detected and not fallback_detected:
            return f"No gap or fallback detected for event type {event_type!r}."

        parts = []
        if gap_detected:
            parts.append(f"gap ({gap_reason})")
        if fallback_detected:
            parts.append(f"fallback ({fallback_reason})")
        return f"Verification for event type {event_type!r} found: " + "; ".join(parts) + "."
