"""
tests/test_verification_runtime.py
====================================
Unit tests for the H4-2 Verification Runtime
(verification.verification_runtime.VerificationRuntime).

Uses unittest (stdlib) rather than pytest: pytest is not currently a
dependency of this project's requirements.txt / .venv, and H4-2 does not
introduce new dependencies.
"""

import datetime
import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from verification.verification_runtime import VerificationRuntime
from verification.verification_result import VerificationResult


def _event(event_type, payload=None, **envelope_fields):
    event = {
        "version": 1,
        "type": event_type,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "payload": payload if payload is not None else {},
    }
    event.update(envelope_fields)
    return event


class GapDetectionTests(unittest.TestCase):
    def test_missing_payload_is_a_gap(self):
        rt = VerificationRuntime()
        result = rt.handle(_event("transcript", payload={}))
        self.assertTrue(result.gap_detected)
        self.assertIn("missing payload", result.gap_reason)

    def test_missing_required_field_is_a_gap(self):
        rt = VerificationRuntime()
        payload = {"text": "hello", "language": "en", "confidence": 0.9}  # is_final missing
        result = rt.handle(_event("transcript", payload=payload))
        self.assertTrue(result.gap_detected)
        self.assertIn("is_final", result.gap_reason)

    def test_complete_payload_is_not_a_gap(self):
        rt = VerificationRuntime()
        payload = {"text": "hello", "language": "en", "confidence": 0.9, "is_final": True}
        result = rt.handle(_event("transcript", payload=payload))
        self.assertFalse(result.gap_detected)
        self.assertIsNone(result.gap_reason)

    def test_type_mismatch_is_a_gap(self):
        rt = VerificationRuntime()
        payload = {"text": "hi", "language": "en", "confidence": "high", "is_final": True}
        result = rt.handle(_event("transcript", payload=payload))
        self.assertTrue(result.gap_detected)
        self.assertIn("confidence", result.gap_reason)

    def test_value_out_of_range_is_a_gap(self):
        rt = VerificationRuntime()
        payload = {"text": "hi", "language": "en", "confidence": 1.5, "is_final": True}
        result = rt.handle(_event("transcript", payload=payload))
        self.assertTrue(result.gap_detected)
        self.assertIn("range", result.gap_reason)

    def test_negative_latency_is_a_gap(self):
        rt = VerificationRuntime()
        payload = {"stt_ms": -1, "routing_ms": 1, "provider_ms": 1, "total_ms": 1}
        result = rt.handle(_event("latency", payload=payload))
        self.assertTrue(result.gap_detected)
        self.assertIn("stt_ms", result.gap_reason)

    def test_undefined_state_is_a_gap(self):
        rt = VerificationRuntime()
        payload = {"state": "TELEPORTING", "message": "?"}
        result = rt.handle(_event("status", payload=payload))
        self.assertTrue(result.gap_detected)
        self.assertIn("undefined state", result.gap_reason)

    def test_known_state_is_not_a_gap(self):
        rt = VerificationRuntime()
        payload = {"state": "READY", "message": "ready"}
        result = rt.handle(_event("status", payload=payload))
        self.assertFalse(result.gap_detected)

    def test_undefined_transition_between_known_states_is_not_flagged(self):
        # The Contract defines no transition table; only enum membership
        # is checked. This must not be treated as a gap.
        rt = VerificationRuntime()
        rt.handle(_event("status", payload={"state": "READY", "message": "a"}, session_id="s1"))
        result = rt.handle(_event("status", payload={"state": "STARTING", "message": "b"}, session_id="s1"))
        self.assertFalse(result.gap_detected)

    def test_sequence_regression_is_a_gap(self):
        rt = VerificationRuntime()
        rt.handle(_event("transcript",
                          payload={"text": "a", "language": "en", "confidence": 0.5, "is_final": False},
                          session_id="s1", sequence=5))
        result = rt.handle(_event("transcript",
                                   payload={"text": "b", "language": "en", "confidence": 0.5, "is_final": False},
                                   session_id="s1", sequence=3))
        self.assertTrue(result.gap_detected)
        self.assertIn("sequence regression", result.gap_reason)

    def test_timestamp_regression_is_a_gap(self):
        rt = VerificationRuntime()
        later = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        earlier = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
        rt.handle(_event("status", payload={"state": "READY", "message": "a"},
                          session_id="s1", timestamp=later.isoformat()))
        result = rt.handle(_event("status", payload={"state": "READY", "message": "b"},
                                   session_id="s1", timestamp=earlier.isoformat()))
        self.assertTrue(result.gap_detected)
        self.assertIn("timestamp moved backwards", result.gap_reason)

    def test_ordering_does_not_assume_fixed_type_sequence(self):
        # reply before transcript, in isolation, must not be flagged as a
        # gap purely due to event-type ordering.
        rt = VerificationRuntime()
        result = rt.handle(_event(
            "reply",
            payload={"provider": "openai", "model": "gpt", "text": "hi", "finish_reason": "stop"},
        ))
        self.assertFalse(result.gap_detected)

    def test_missing_sequence_and_session_never_raises(self):
        rt = VerificationRuntime()
        try:
            rt.handle(_event("latency", payload={
                "stt_ms": 1, "routing_ms": 1, "provider_ms": 1, "total_ms": 3,
            }))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"handle() raised unexpectedly: {exc}")


class FallbackDetectionTests(unittest.TestCase):
    def test_fallback_finish_reason_detected(self):
        rt = VerificationRuntime()
        payload = {"provider": "openai", "model": "gpt-4", "text": "hi", "finish_reason": "fallback"}
        result = rt.handle(_event("reply", payload=payload))
        self.assertTrue(result.fallback_detected)
        self.assertIn("fallback", result.fallback_reason)

    def test_normal_finish_reason_not_a_fallback(self):
        rt = VerificationRuntime()
        payload = {"provider": "openai", "model": "gpt-4", "text": "hi", "finish_reason": "stop"}
        result = rt.handle(_event("reply", payload=payload))
        self.assertFalse(result.fallback_detected)

    def test_fallback_only_applies_to_reply_events(self):
        rt = VerificationRuntime()
        result = rt.handle(_event("error", payload={
            "code": "E1", "message": "boom", "recoverable": True,
        }))
        self.assertFalse(result.fallback_detected)


class ReliabilityEvaluationTests(unittest.TestCase):
    def test_score_within_bounds_when_clean(self):
        rt = VerificationRuntime()
        payload = {"text": "hi", "language": "en", "confidence": 0.9, "is_final": True}
        result = rt.handle(_event("transcript", payload=payload))
        self.assertTrue(0.0 <= result.reliability_score <= 1.0)
        self.assertTrue(result.reliable)

    def test_score_within_bounds_when_gap_and_fallback(self):
        rt = VerificationRuntime()
        payload = {"provider": "openai", "finish_reason": "fallback"}  # missing model/text
        result = rt.handle(_event("reply", payload=payload))
        self.assertTrue(0.0 <= result.reliability_score <= 1.0)
        self.assertTrue(result.gap_detected)
        self.assertTrue(result.fallback_detected)


class WarningGenerationTests(unittest.TestCase):
    def test_warning_on_gap(self):
        rt = VerificationRuntime()
        result = rt.handle(_event("transcript", payload={}))
        self.assertTrue(any("gap" in w for w in result.warnings))

    def test_warning_on_fallback(self):
        rt = VerificationRuntime()
        payload = {"provider": "openai", "model": "gpt-4", "text": "hi", "finish_reason": "fallback"}
        result = rt.handle(_event("reply", payload=payload))
        self.assertTrue(any("fallback" in w for w in result.warnings))

    def test_warning_on_unknown_event_type(self):
        rt = VerificationRuntime()
        result = rt.handle(_event("future_event", payload={"anything": 1}))
        self.assertTrue(any("unknown event type" in w for w in result.warnings))


class ExplanationGenerationTests(unittest.TestCase):
    def test_explanation_always_present(self):
        rt = VerificationRuntime()
        clean = rt.handle(_event("transcript", payload={
            "text": "hi", "language": "en", "confidence": 0.9, "is_final": True,
        }))
        broken = rt.handle(_event("transcript", payload={}))
        self.assertTrue(clean.explanation)
        self.assertTrue(broken.explanation)


class UnknownEventTypeTests(unittest.TestCase):
    def test_unknown_type_does_not_raise_and_continues(self):
        rt = VerificationRuntime()
        try:
            result = rt.handle(_event("future_event", payload={"foo": "bar"}))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"handle() raised on unknown type: {exc}")
        self.assertIsInstance(result, VerificationResult)
        # A subsequent, known event on the same runtime instance must still
        # be processed normally.
        follow_up = rt.handle(_event("transcript", payload={
            "text": "hi", "language": "en", "confidence": 0.9, "is_final": True,
        }))
        self.assertFalse(follow_up.gap_detected)

    def test_unknown_type_payload_fields_do_not_raise(self):
        # Forward compatibility: an unknown type may carry an arbitrary
        # payload shape. Verification Runtime has no schema for it and
        # must not attempt to validate its fields.
        rt = VerificationRuntime()
        try:
            result = rt.handle(_event("future_event", payload={
                "anything": "goes", "nested": {"a": 1}, "list": [1, 2, 3],
            }))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"handle() raised on unknown type's payload fields: {exc}")
        self.assertFalse(result.gap_detected)


class PayloadBoundaryTests(unittest.TestCase):
    def test_payload_none_and_payload_empty_dict_are_equivalent(self):
        rt_a = VerificationRuntime()
        rt_b = VerificationRuntime()
        event_none = _event("transcript")
        event_none["payload"] = None
        result_none = rt_a.handle(event_none)
        result_empty = rt_b.handle(_event("transcript", payload={}))
        self.assertEqual(result_none.gap_detected, result_empty.gap_detected)
        self.assertEqual(result_none.gap_reason, result_empty.gap_reason)

    def test_missing_payload_key_entirely_does_not_raise(self):
        rt = VerificationRuntime()
        event = _event("transcript")
        del event["payload"]
        try:
            result = rt.handle(event)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"handle() raised when payload key was absent: {exc}")
        self.assertTrue(result.gap_detected)


class ImmutabilityAndIdentityTests(unittest.TestCase):
    def test_result_has_no_event_type_field(self):
        self.assertNotIn("event_type", VerificationResult.__dataclass_fields__)

    def test_result_is_frozen(self):
        rt = VerificationRuntime()
        result = rt.handle(_event("transcript", payload={
            "text": "hi", "language": "en", "confidence": 0.9, "is_final": True,
        }))
        with self.assertRaises(Exception):
            result.gap_detected = True  # frozen dataclass must reject mutation

    def test_source_event_id_and_session_id_are_references_not_copies(self):
        rt = VerificationRuntime()
        result = rt.handle(_event(
            "transcript",
            payload={"text": "hi", "language": "en", "confidence": 0.9, "is_final": True},
            event_id="evt-123", session_id="sess-abc",
        ))
        self.assertEqual(result.source_event_id, "evt-123")
        self.assertEqual(result.session_id, "sess-abc")

    def test_missing_event_id_and_session_id_never_raise(self):
        rt = VerificationRuntime()
        try:
            result = rt.handle(_event("transcript", payload={
                "text": "hi", "language": "en", "confidence": 0.9, "is_final": True,
            }))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"handle() raised on missing event_id/session_id: {exc}")
        self.assertIsNone(result.source_event_id)
        self.assertIsNone(result.session_id)


if __name__ == "__main__":
    unittest.main()
