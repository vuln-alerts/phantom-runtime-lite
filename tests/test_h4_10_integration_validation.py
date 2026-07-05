"""
tests/test_h4_10_integration_validation.py
==============================================
H4-10 End-to-end validation using actual Runtime events.

    Cloud Run Runtime (real _emit_event(...) wire shape)
        |
    Runtime Adapter                 (runtime.event_adapter, H4-10, translation only)
        |
    Verification Runtime            (H4-2, unmodified)
        |
    Trust Runtime                   (H4-3, unmodified)
        |
    Dashboard Runtime                (H4-6, unmodified)
        |
    Event Aggregator                 (H4-5, unmodified)
        |
    FastAPI (/aggregate)             (H4-6, unmodified)
        |
    JSON

Every raw event fixture below is a literal reproduction of a real
_emit_event(...) call site in src/phantom_runtime.py (see
docs/H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md section 1) -- not a
synthetic Contract-shaped event. This module never imports
phantom_runtime, runtime.cloud_run_shell, or runtime.transport_gateway;
the Cloud Run Runtime is exercised only through the literal shape of the
events it is known (by reading its unmodified source) to emit.

This validates, using those real-shaped events pushed through the actual
Adapter and the actual, unmodified H4-2..H4-6 components:
  - Contract compliance   (translated envelope has exactly the Contract's
                            envelope keys, and `type` is Contract-known)
  - Identity propagation   (event_id/session_id survive every hop into JSON)
  - No field loss          (every dataclass field present in the JSON)
  - No field rename        (JSON keys match dataclass field names exactly)
  - No field recomputation (numeric judgments are bit-identical across the
                            JSON boundary, not re-derived)
  - JSON output            (FastAPI /aggregate responds 200 with valid JSON)
  - Regression             (six Contract event types + one unknown type
                            all flow through without raising)

Note on the unknown-event-type case: this module verifies pipeline
interoperability only -- that an event type the real Runtime has never
emitted still flows through every stage without raising and still reaches
FastAPI as valid JSON. It intentionally does not assert any specific
warning string, gap_reason text, or other internal wording chosen by
VerificationRuntime; that classification behavior belongs to
tests/test_verification_runtime.py, not to this integration-boundary
module.

Uses unittest (stdlib) plus fastapi.testclient.TestClient, consistent with
tests/test_h4_integration.py and tests/test_h4_10_runtime_adapter.py.
"""

import datetime
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
from runtime.event_adapter import RuntimeEventAdapter
from trust.trust_result import TrustResult
from trust.trust_runtime import TrustRuntime
from verification.verification_result import VerificationResult
from verification.verification_runtime import VerificationRuntime

from api.api_server import app


# ---------------------------------------------------------------------------
# Raw wire-format fixtures -- literal reproductions of real _emit_event(...)
# call sites (src/phantom_runtime.py). Same fixtures as
# tests/test_h4_10_runtime_adapter.py, redefined here per this project's
# existing convention of self-contained test modules.
# ---------------------------------------------------------------------------

_TS = "2026-07-06T12:00:00+00:00"


def _raw_event(event_type, payload):
    """Exactly the envelope shape _emit_event() constructs (phantom_runtime.py:597-602)."""
    return {"version": 1, "type": event_type, "timestamp": _TS, "payload": payload}


RAW_FIXTURES = {
    # phantom_runtime.py:822 -- _set_state
    "status": _raw_event("status", {"state": "recruiter_speaking", "previous": "idle"}),
    # phantom_runtime.py:892 -- show_err
    "error": _raw_event("error", {"label": "Audio", "message": "fd read failed: EOF"}),
    # phantom_runtime.py:900 -- show_latency
    "latency": _raw_event("latency", {"stt_ms": 120.0, "gpt_ms": 300.0, "total_ms": 420.0}),
    # phantom_runtime.py:3222 -- reply_worker, final agent reply
    "reply": _raw_event(
        "reply", {"text": "Nice to meet you", "lang": "en", "speaker": "agent", "ts": 1720000001.0}
    ),
    # phantom_runtime.py:3097 -- reply_worker, heard speech
    "transcript": _raw_event(
        "transcript", {"text": "hello there", "lang": "en", "ts": 1720000000.0, "speaker": "user"}
    ),
    # phantom_runtime.py:9134 -- generate_meeting_analysis
    "analysis": _raw_event("analysis", {"text": "User introduced themselves; no risks detected."}),
}

# Forward-compatibility case: an event type _emit_event has never produced
# and never will under the current Contract, per Backward Compatibility
# ("Consumers must ignore unknown fields") / forward-compat with future
# event types.
RAW_UNKNOWN_TYPE = _raw_event("future_event", {"anything": 1})


# ---------------------------------------------------------------------------
# Pipeline runner -- Adapter output fed into the real, unmodified H4-2..H4-6
# chain, exactly as tests/test_h4_integration.py's _run_pipeline wires it.
# ---------------------------------------------------------------------------

def _run_pipeline(raw_event, adapter=None, verification_runtime=None):
    adapter = adapter if adapter is not None else RuntimeEventAdapter()
    vr_runtime = verification_runtime if verification_runtime is not None else VerificationRuntime()

    event = adapter.translate(raw_event)
    verification_result = vr_runtime.handle(event)
    trust_result = TrustRuntime().handle(verification_result)
    dashboard_result = DashboardRuntime().render(verification_result, trust_result)
    event_aggregate = EventAggregator().aggregate(verification_result, trust_result, dashboard_result)
    return event, verification_result, trust_result, dashboard_result, event_aggregate


def _json_default(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


def _client_payload(event_aggregate):
    import json
    return json.loads(json.dumps(asdict(event_aggregate), default=_json_default))


def _post_aggregate(client, event_aggregate):
    payload = _client_payload(event_aggregate)
    response = client.post("/aggregate", json=payload)
    return response, payload


def _assert_exact_field_set(testcase, dataclass_type, json_obj):
    expected = set(dataclass_type.__dataclass_fields__.keys())
    actual = set(json_obj.keys())
    testcase.assertEqual(
        expected, actual,
        f"{dataclass_type.__name__} JSON field set does not match its dataclass contract",
    )


_CONTRACT_ENVELOPE_KEYS = {
    "schema_version", "event_id", "timestamp", "session_id", "sequence", "type", "payload",
}
_CONTRACT_EVENT_TYPES = {"transcript", "reply", "analysis", "latency", "status", "error"}


# ---------------------------------------------------------------------------
# Contract compliance of the Adapter's translated envelope
# ---------------------------------------------------------------------------

class ContractComplianceTests(unittest.TestCase):
    def test_translated_envelope_has_exactly_the_contract_keys(self):
        for label, raw in RAW_FIXTURES.items():
            with self.subTest(event_type=label):
                event = RuntimeEventAdapter().translate(raw)
                self.assertEqual(set(event.keys()), _CONTRACT_ENVELOPE_KEYS)

    def test_translated_type_is_one_of_the_contract_event_types(self):
        for label, raw in RAW_FIXTURES.items():
            with self.subTest(event_type=label):
                event = RuntimeEventAdapter().translate(raw)
                self.assertIn(event["type"], _CONTRACT_EVENT_TYPES)


# ---------------------------------------------------------------------------
# Full pipeline wiring for every real event type
# ---------------------------------------------------------------------------

class RealEventPipelineWiringTests(unittest.TestCase):
    def _assert_wired(self, raw):
        event, vr, tr, dr, ea = _run_pipeline(raw)
        self.assertIsInstance(vr, VerificationResult)
        self.assertIsInstance(tr, TrustResult)
        self.assertIsInstance(dr, DashboardResult)
        self.assertIsInstance(ea, EventAggregate)
        self.assertIs(ea.verification_result, vr)
        self.assertIs(ea.trust_result, tr)
        self.assertIs(ea.dashboard_result, dr)
        return event, vr, tr, dr, ea

    def test_status_event_flows_through_full_pipeline(self):
        self._assert_wired(RAW_FIXTURES["status"])

    def test_error_event_flows_through_full_pipeline(self):
        self._assert_wired(RAW_FIXTURES["error"])

    def test_latency_event_flows_through_full_pipeline(self):
        self._assert_wired(RAW_FIXTURES["latency"])

    def test_reply_event_flows_through_full_pipeline(self):
        self._assert_wired(RAW_FIXTURES["reply"])

    def test_transcript_event_flows_through_full_pipeline(self):
        self._assert_wired(RAW_FIXTURES["transcript"])

    def test_analysis_event_flows_through_full_pipeline(self):
        self._assert_wired(RAW_FIXTURES["analysis"])

    def test_unknown_event_type_flows_through_the_complete_pipeline(self):
        # Pipeline interoperability only: an event type the real Runtime
        # has never emitted must still produce every downstream result
        # type, without raising, and still reach FastAPI as valid JSON.
        # The specific classification VerificationRuntime assigns it
        # (warning text, gap/fallback wording, etc.) is that component's
        # own implementation detail -- covered by
        # tests/test_verification_runtime.py, not asserted here.
        try:
            event, vr, tr, dr, ea = self._assert_wired(RAW_UNKNOWN_TYPE)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"pipeline raised on unknown event type: {exc}")

        client = TestClient(app)
        response, _ = _post_aggregate(client, ea)
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), dict)


# ---------------------------------------------------------------------------
# Identity propagation, end to end
# ---------------------------------------------------------------------------

class IdentityPropagationTests(unittest.TestCase):
    def test_adapter_generated_identity_propagates_through_all_stages_and_json(self):
        client = TestClient(app)
        adapter = RuntimeEventAdapter(session_id="sess-h4-10-e2e")
        event, vr, tr, dr, ea = _run_pipeline(RAW_FIXTURES["transcript"], adapter=adapter)

        for stage in (vr, tr, dr, ea):
            self.assertEqual(stage.source_event_id, event["event_id"])
            self.assertEqual(stage.session_id, "sess-h4-10-e2e")

        response, _ = _post_aggregate(client, ea)
        body = response.json()
        self.assertEqual(body["source_event_id"], event["event_id"])
        self.assertEqual(body["session_id"], "sess-h4-10-e2e")

    def test_sequence_increments_across_a_multi_event_session(self):
        adapter = RuntimeEventAdapter(session_id="sess-h4-10-seq")
        shared_vr_runtime = VerificationRuntime()
        sequences = []
        for label in ("transcript", "reply", "latency"):
            event, vr, _, _, _ = _run_pipeline(
                RAW_FIXTURES[label], adapter=adapter, verification_runtime=shared_vr_runtime
            )
            sequences.append(event["sequence"])
            self.assertNotIn("sequence regression", vr.gap_reason or "")
        self.assertEqual(sequences, [1, 2, 3])


# ---------------------------------------------------------------------------
# No field loss / no field rename / no field recomputation across the full
# JSON boundary, for real-event-derived pipeline output.
# ---------------------------------------------------------------------------

class FieldConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_no_field_lost_across_the_full_json_boundary(self):
        _, vr, tr, dr, ea = _run_pipeline(RAW_FIXTURES["status"])
        response, _ = _post_aggregate(self.client, ea)
        body = response.json()

        _assert_exact_field_set(self, EventAggregate, body)
        _assert_exact_field_set(self, VerificationResult, body["verification_result"])
        _assert_exact_field_set(self, TrustResult, body["trust_result"])
        _assert_exact_field_set(self, DashboardResult, body["dashboard_result"])

    def test_no_field_renamed_or_recomputed_across_the_full_json_boundary(self):
        _, vr, tr, dr, ea = _run_pipeline(RAW_FIXTURES["latency"])
        response, _ = _post_aggregate(self.client, ea)
        body = response.json()

        # Bit-exact identity of the numeric judgments made upstream -- the
        # API layer must not re-derive trust_score or reliability_score
        # from the real-event-derived pipeline output.
        self.assertEqual(body["verification_result"]["reliability_score"], vr.reliability_score)
        self.assertEqual(body["trust_result"]["trust_score"], tr.trust_score)
        self.assertEqual(body["dashboard_result"]["trust_score"], tr.trust_score)
        self.assertEqual(body["dashboard_result"]["reliability_score"], vr.reliability_score)

    def test_gap_and_trust_findings_from_a_real_reply_event_preserved_into_json(self):
        # The real reply event (RAW_FIXTURES["reply"]) has no
        # provider/model/finish_reason on the wire at all -- documented gap
        # (docs/H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md). Confirms that
        # gap survives, unrenamed and unrecomputed, all the way to JSON.
        _, vr, tr, dr, ea = _run_pipeline(RAW_FIXTURES["reply"])
        self.assertTrue(vr.gap_detected)

        response, _ = _post_aggregate(self.client, ea)
        body = response.json()
        self.assertEqual(body["verification_result"]["gap_detected"], vr.gap_detected)
        self.assertEqual(body["verification_result"]["gap_reason"], vr.gap_reason)
        self.assertEqual(body["dashboard_result"]["gap_detected"], vr.gap_detected)
        self.assertEqual(body["dashboard_result"]["gap_reason"], vr.gap_reason)
        self.assertEqual(body["trust_result"]["trust_score"], tr.trust_score)
        self.assertEqual(body["dashboard_result"]["trust_score"], tr.trust_score)


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

class JsonOutputTests(unittest.TestCase):
    def test_every_real_event_type_reaches_fastapi_as_valid_200_json(self):
        client = TestClient(app)
        for label, raw in RAW_FIXTURES.items():
            with self.subTest(event_type=label):
                _, _, _, _, ea = _run_pipeline(raw)
                response, _ = _post_aggregate(client, ea)
                self.assertEqual(response.status_code, 200)
                self.assertIsInstance(response.json(), dict)


if __name__ == "__main__":
    unittest.main()
