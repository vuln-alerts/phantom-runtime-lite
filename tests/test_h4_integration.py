"""
tests/test_h4_integration.py
===============================
H4-7 Integration tests: validate the complete H4 pipeline end-to-end.

    Typed Event
        |
    Verification Runtime
        |
    Trust Runtime
        |
    Dashboard Runtime
        |
    Event Aggregator
        |
    FastAPI (/aggregate)
        |
    JSON

Per docs/H4_IMPLEMENTATION_PLAN.md ("H4-7 Integration") and
docs/H4_RUNTIME_EVENT_CONTRACT.md (v1.0, Frozen), this module adds no new
Runtime, no new DTO, and no alternate contract. It wires the existing,
already-tested H4-2..H4-6 components together exactly as their own public
APIs are already shaped:

    VerificationRuntime().handle(event)                  -> VerificationResult
    TrustRuntime().handle(verification_result)            -> TrustResult
    DashboardRuntime().render(verification_result, trust_result)
                                                            -> DashboardResult
    EventAggregator().aggregate(vr, tr, dr)                -> EventAggregate
    api_server.app  POST /aggregate                        -> JSON

No integration glue module was required: every stage's constructor and
public method already composes directly with the next stage's input type,
so these tests call the existing components directly, in sequence, using
plain dict Typed Events shaped per the frozen Runtime Event Contract
(schema_version, event_id, timestamp, session_id, sequence, type, payload)
-- the same envelope shape documented in H4_RUNTIME_EVENT_CONTRACT.md
("Runtime Event Envelope").

These tests never import phantom_runtime, runtime.cloud_run_shell,
runtime.transport_gateway, any Provider, Whisper, OpenAI, or Gemini module
-- the Cloud Run Runtime is exercised only through the Typed Event
contract it publishes, never invoked directly, per the Single Runtime
Policy.

Uses unittest (stdlib) plus fastapi.testclient.TestClient, consistent with
tests/test_api_server.py, tests/test_event_aggregator.py,
tests/test_dashboard_runtime.py, tests/test_trust_runtime.py, and
tests/test_verification_runtime.py: pytest is not a dependency of this
project.
"""

import ast
import datetime
import json
import os
import sys
import unittest
from dataclasses import asdict

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from fastapi.testclient import TestClient

from aggregator.event_aggregate import EventAggregate
from aggregator.event_aggregator import EventAggregator
from dashboard.dashboard_result import DashboardResult
from dashboard.dashboard_runtime import DashboardRuntime
from trust.trust_result import TrustResult
from trust.trust_runtime import TrustRuntime
from verification.verification_result import VerificationResult
from verification.verification_runtime import VerificationRuntime

from api.api_server import app


# ---------------------------------------------------------------------------
# Typed Event construction — plain dicts shaped per H4_RUNTIME_EVENT_CONTRACT.md
# ("Runtime Event Envelope" + "Event Payloads"). Not a DTO: the Contract
# defines RuntimeEvent as a wire envelope, and VerificationRuntime.handle()
# already accepts a Mapping, so no class is introduced here.
# ---------------------------------------------------------------------------

def _typed_event(event_type, payload=None, **envelope_overrides):
    event = {
        "schema_version": "1.0",
        "event_id": "evt-1",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": "sess-1",
        "sequence": 1,
        "type": event_type,
        "payload": payload if payload is not None else {},
    }
    event.update(envelope_overrides)
    return event


def _transcript_payload(**overrides):
    payload = {"text": "hello", "language": "en", "confidence": 0.9, "is_final": True}
    payload.update(overrides)
    return payload


def _reply_payload(**overrides):
    payload = {"provider": "openai", "model": "gpt-4", "text": "hi", "finish_reason": "stop"}
    payload.update(overrides)
    return payload


def _analysis_payload(**overrides):
    payload = {"intent": "greeting", "summary": "user said hello", "metadata": {"turn": 1}}
    payload.update(overrides)
    return payload


def _latency_payload(**overrides):
    payload = {"stt_ms": 120, "routing_ms": 5, "provider_ms": 300, "total_ms": 425}
    payload.update(overrides)
    return payload


def _status_payload(**overrides):
    payload = {"state": "READY", "message": "runtime ready"}
    payload.update(overrides)
    return payload


def _error_payload(**overrides):
    payload = {"code": "E_TIMEOUT", "message": "provider timed out", "recoverable": True}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Pipeline runner — the H4-7 wiring itself, expressed with no new class.
# ---------------------------------------------------------------------------

def _run_pipeline(event, verification_runtime=None):
    """Drive one Typed Event through Verification -> Trust -> Dashboard ->
    Aggregator, exactly as the H4-7 architecture diagram specifies.
    """
    vr_runtime = verification_runtime if verification_runtime is not None else VerificationRuntime()
    verification_result = vr_runtime.handle(event)
    trust_result = TrustRuntime().handle(verification_result)
    dashboard_result = DashboardRuntime().render(verification_result, trust_result)
    event_aggregate = EventAggregator().aggregate(verification_result, trust_result, dashboard_result)
    return verification_result, trust_result, dashboard_result, event_aggregate


def _json_default(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


def _client_payload(event_aggregate):
    """Serialize an EventAggregate the way a real HTTP client would."""
    return json.loads(json.dumps(asdict(event_aggregate), default=_json_default))


def _post_aggregate(client, event_aggregate):
    payload = _client_payload(event_aggregate)
    response = client.post("/aggregate", json=payload)
    return response, payload


def _assert_exact_field_set(testcase, dataclass_type, json_obj):
    """No field renamed, dropped, or added between the dataclass contract
    and its JSON representation.
    """
    expected = set(dataclass_type.__dataclass_fields__.keys())
    actual = set(json_obj.keys())
    testcase.assertEqual(
        expected, actual,
        f"{dataclass_type.__name__} JSON field set does not match its dataclass contract",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class PipelineTypeWiringTests(unittest.TestCase):
    """Every stage's output must be the exact declared type of the next
    stage's input, for every Contract-defined event type.
    """

    def _assert_wired(self, event):
        vr, tr, dr, ea = _run_pipeline(event)
        self.assertIsInstance(vr, VerificationResult)
        self.assertIsInstance(tr, TrustResult)
        self.assertIsInstance(dr, DashboardResult)
        self.assertIsInstance(ea, EventAggregate)
        # References, not copies -- Aggregator holds the exact objects
        # produced by the upstream stages (per event_aggregator.py's own
        # "no field is copied verbatim" contract).
        self.assertIs(ea.verification_result, vr)
        self.assertIs(ea.trust_result, tr)
        self.assertIs(ea.dashboard_result, dr)
        return vr, tr, dr, ea

    def test_transcript_event_flows_through_full_pipeline(self):
        self._assert_wired(_typed_event("transcript", _transcript_payload()))

    def test_reply_event_flows_through_full_pipeline(self):
        self._assert_wired(_typed_event("reply", _reply_payload()))

    def test_analysis_event_flows_through_full_pipeline(self):
        self._assert_wired(_typed_event("analysis", _analysis_payload()))

    def test_latency_event_flows_through_full_pipeline(self):
        self._assert_wired(_typed_event("latency", _latency_payload()))

    def test_status_event_flows_through_full_pipeline(self):
        self._assert_wired(_typed_event("status", _status_payload()))

    def test_error_event_flows_through_full_pipeline(self):
        self._assert_wired(_typed_event("error", _error_payload()))

    def test_unknown_event_type_flows_through_full_pipeline_without_raising(self):
        # Forward-compatibility per the Contract's Backward Compatibility
        # rule: an unrecognized type must not break the pipeline.
        try:
            vr, tr, dr, ea = self._assert_wired(_typed_event("future_event", {"anything": 1}))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"pipeline raised on unknown event type: {exc}")
        self.assertTrue(any("unknown event type" in w for w in vr.warnings))


class IdentityPropagationEndToEndTests(unittest.TestCase):
    def test_event_id_and_session_id_propagate_through_all_stages(self):
        event = _typed_event("transcript", _transcript_payload(), event_id="evt-42", session_id="sess-42")
        vr, tr, dr, ea = _run_pipeline(event)
        for stage in (vr, tr, dr, ea):
            self.assertEqual(stage.source_event_id, "evt-42")
            self.assertEqual(stage.session_id, "sess-42")

    def test_absent_identity_propagates_as_none_through_all_stages(self):
        # Mirrors the documented Contract/Runtime gap (verification_runtime.py):
        # event_id/session_id may be absent on the wire today; every stage
        # must still complete and propagate None consistently.
        event = _typed_event("transcript", _transcript_payload())
        del event["event_id"]
        del event["session_id"]
        vr, tr, dr, ea = _run_pipeline(event)
        for stage in (vr, tr, dr, ea):
            self.assertIsNone(stage.source_event_id)
            self.assertIsNone(stage.session_id)


class FieldConsistencyEndToEndTests(unittest.TestCase):
    """No field is renamed, recomputed, or lost as it crosses each hop,
    from the raw Typed Event all the way to the FastAPI JSON response.
    """

    def setUp(self):
        self.client = TestClient(app)

    def test_verification_findings_preserved_into_dashboard_and_json(self):
        # Missing 'is_final' -> gap. Confidence in range, so only the
        # missing-field gap fires -- a single, unambiguous signal to trace.
        payload = _transcript_payload()
        del payload["is_final"]
        event = _typed_event("transcript", payload)
        vr, tr, dr, ea = _run_pipeline(event)

        self.assertTrue(vr.gap_detected)
        self.assertEqual(dr.gap_detected, vr.gap_detected)
        self.assertEqual(dr.gap_reason, vr.gap_reason)
        self.assertEqual(dr.reliability_score, vr.reliability_score)
        self.assertEqual(dr.reliable, vr.reliable)
        self.assertEqual(dr.warnings, vr.warnings)

        response, _ = _post_aggregate(self.client, ea)
        body = response.json()
        self.assertEqual(body["verification_result"]["gap_detected"], vr.gap_detected)
        self.assertEqual(body["verification_result"]["gap_reason"], vr.gap_reason)
        self.assertEqual(body["dashboard_result"]["gap_detected"], vr.gap_detected)
        self.assertEqual(body["dashboard_result"]["gap_reason"], vr.gap_reason)

    def test_trust_findings_preserved_into_dashboard_and_json(self):
        event = _typed_event("reply", _reply_payload(finish_reason="fallback"))
        vr, tr, dr, ea = _run_pipeline(event)

        self.assertTrue(vr.fallback_detected)
        self.assertEqual(dr.trust_score, tr.trust_score)
        self.assertEqual(dr.trust_level, tr.trust_level)
        self.assertEqual(dr.human_review_required, tr.human_review_required)
        self.assertEqual(dr.review_reason, tr.review_reason)
        self.assertEqual(dr.contributing_factors, tr.contributing_factors)

        response, _ = _post_aggregate(self.client, ea)
        body = response.json()
        self.assertEqual(body["trust_result"]["trust_score"], tr.trust_score)
        self.assertEqual(body["trust_result"]["trust_level"], tr.trust_level)
        self.assertEqual(body["dashboard_result"]["trust_score"], tr.trust_score)
        self.assertEqual(body["dashboard_result"]["trust_level"], tr.trust_level)

    def test_combined_gap_and_fallback_triggers_review_end_to_end(self):
        # 'model' missing -> gap; finish_reason='fallback' -> fallback.
        # Per trust_runtime.py's Trust Policy, gap AND fallback together
        # force human_review_required=True regardless of trust_level.
        payload = _reply_payload(finish_reason="fallback")
        del payload["model"]
        event = _typed_event("reply", payload, event_id="evt-combined", session_id="sess-combined")
        vr, tr, dr, ea = _run_pipeline(event)

        self.assertTrue(vr.gap_detected)
        self.assertTrue(vr.fallback_detected)
        self.assertTrue(tr.human_review_required)

        response, _ = _post_aggregate(self.client, ea)
        body = response.json()
        self.assertEqual(body["source_event_id"], "evt-combined")
        self.assertEqual(body["session_id"], "sess-combined")
        self.assertTrue(body["verification_result"]["gap_detected"])
        self.assertTrue(body["verification_result"]["fallback_detected"])
        self.assertTrue(body["trust_result"]["human_review_required"])
        self.assertTrue(body["dashboard_result"]["human_review_required"])
        self.assertEqual(body["trust_result"]["review_reason"], tr.review_reason)

    def test_full_json_response_is_value_identical_to_pipeline_output(self):
        event = _typed_event(
            "transcript", _transcript_payload(confidence=0.42),
            event_id="evt-json", session_id="sess-json", sequence=7,
        )
        _, _, _, ea = _run_pipeline(event)
        response, request_payload = _post_aggregate(self.client, ea)
        self.assertEqual(response.status_code, 200)
        body = response.json()

        def _normalize(value):
            if isinstance(value, dict):
                return {
                    key: (
                        datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
                        if key == "timestamp" and isinstance(val, str)
                        else _normalize(val)
                    )
                    for key, val in value.items()
                }
            if isinstance(value, list):
                return [_normalize(item) for item in value]
            return value

        self.assertEqual(_normalize(body), _normalize(request_payload))

    def test_no_field_lost_across_the_full_json_boundary(self):
        event = _typed_event("status", _status_payload(state="PROCESSING"))
        _, _, _, ea = _run_pipeline(event)
        response, _ = _post_aggregate(self.client, ea)
        body = response.json()

        _assert_exact_field_set(self, EventAggregate, body)
        _assert_exact_field_set(self, VerificationResult, body["verification_result"])
        _assert_exact_field_set(self, TrustResult, body["trust_result"])
        _assert_exact_field_set(self, DashboardResult, body["dashboard_result"])

    def test_no_field_renamed_or_recomputed_across_the_full_json_boundary(self):
        event = _typed_event("latency", _latency_payload(stt_ms=999))
        vr, tr, dr, ea = _run_pipeline(event)
        response, _ = _post_aggregate(self.client, ea)
        body = response.json()

        # Bit-exact identity of the numeric judgments made upstream --
        # the API layer must not re-derive trust_score or reliability_score.
        self.assertEqual(body["verification_result"]["reliability_score"], vr.reliability_score)
        self.assertEqual(body["trust_result"]["trust_score"], tr.trust_score)
        self.assertEqual(body["dashboard_result"]["trust_score"], tr.trust_score)
        self.assertEqual(body["dashboard_result"]["reliability_score"], vr.reliability_score)


class SessionOrderingEndToEndTests(unittest.TestCase):
    """The Verification Runtime's per-session ordering state (P4, Event
    Ordering) must be observable all the way through the JSON response.
    """

    def test_sequence_regression_reduces_trust_end_to_end(self):
        client = TestClient(app)
        shared_verification_runtime = VerificationRuntime()

        first_event = _typed_event(
            "transcript", _transcript_payload(text="a"),
            session_id="sess-order", sequence=5,
        )
        second_event = _typed_event(
            "transcript", _transcript_payload(text="b"),
            session_id="sess-order", sequence=3,
        )

        _run_pipeline(first_event, verification_runtime=shared_verification_runtime)
        vr2, tr2, dr2, ea2 = _run_pipeline(second_event, verification_runtime=shared_verification_runtime)

        self.assertTrue(vr2.gap_detected)
        self.assertIn("sequence regression", vr2.gap_reason)

        response, _ = _post_aggregate(client, ea2)
        body = response.json()
        self.assertTrue(body["verification_result"]["gap_detected"])
        self.assertIn("sequence regression", body["verification_result"]["gap_reason"])
        self.assertEqual(body["trust_result"]["trust_score"], tr2.trust_score)
        self.assertLess(tr2.trust_score, 1.0)

    def test_ordered_session_stream_stays_reliable_end_to_end(self):
        client = TestClient(app)
        shared_verification_runtime = VerificationRuntime()
        last_body = None

        for seq in range(1, 4):
            event = _typed_event(
                "transcript", _transcript_payload(text=f"turn-{seq}"),
                session_id="sess-stream", sequence=seq,
            )
            _, _, _, ea = _run_pipeline(event, verification_runtime=shared_verification_runtime)
            response, _ = _post_aggregate(client, ea)
            last_body = response.json()

        self.assertFalse(last_body["verification_result"]["gap_detected"])
        self.assertTrue(last_body["verification_result"]["reliable"])
        self.assertEqual(last_body["trust_result"]["trust_level"], "TRUSTED")


class SingleRuntimePolicyIntegrationTests(unittest.TestCase):
    """The integration itself must be achieved purely through the Typed
    Event contract and the existing downstream components -- never by
    reaching into the Cloud Run Runtime, a Provider, or Whisper, and never
    by introducing a new DTO/contract of its own.
    """

    def test_integration_test_module_defines_no_new_dataclass(self):
        with open(__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                decorator_names = {
                    (d.id if isinstance(d, ast.Name) else getattr(d, "attr", ""))
                    for d in node.decorator_list
                }
                self.assertNotIn(
                    "dataclass", decorator_names,
                    "H4-7 integration must not introduce a new dataclass/DTO",
                )

    def test_integration_test_module_never_imports_cloud_run_runtime_or_providers(self):
        with open(__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
                names.update(alias.name for alias in node.names)

        forbidden_substrings = (
            "phantom_runtime", "cloud_run_shell", "transport_gateway",
            "provider", "whisper", "openai", "generativeai",
        )
        for name in names:
            lowered = name.lower()
            for forbidden in forbidden_substrings:
                self.assertNotIn(
                    forbidden, lowered,
                    f"H4-7 integration test imported forbidden module: {name}",
                )

    def test_event_aggregate_remains_the_single_contract_the_api_accepts(self):
        # The FastAPI layer's only Runtime Contract dependency is
        # EventAggregate itself (per api_server.py's own docstring) --
        # confirm the object handed to /aggregate really is that same,
        # unmodified class produced by the Aggregator.
        import aggregator.event_aggregate as event_aggregate_module
        import api.api_server as api_server_module

        self.assertIs(api_server_module.EventAggregate, event_aggregate_module.EventAggregate)


if __name__ == "__main__":
    unittest.main()
