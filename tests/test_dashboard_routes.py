"""
tests/test_dashboard_routes.py
=================================
Integration tests for GET /dashboard and GET / (api.api_server.app), using
fastapi.testclient.TestClient -- consistent with tests/test_api_server.py.

api_server._dashboard_service is a module-level singleton shared by every
test in the process (there is no per-request reset in production, by
design -- see api.dashboard_service.DashboardService). setUp/tearDown here
save and restore its internal state so this file's "nothing posted yet"
assertions are not order-dependent on what other test modules in the same
run already posted to /aggregate.
"""

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
from trust.trust_result import TrustResult
from verification.verification_result import VerificationResult

from api import api_server
from api.api_server import app


def _event_aggregate(
    trust_score=0.87, trust_level="TRUSTED", source_event_id="evt-1",
    conversation_line=None, speaker=None, transcript=None,
):
    vr = VerificationResult(
        schema_version="1.0", source_event_id=source_event_id, session_id="sess-1",
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        gap_detected=False, gap_reason=None, fallback_detected=False, fallback_reason=None,
        reliable=True, reliability_score=1.0, warnings=[], explanation="test",
    )
    tr = TrustResult(
        schema_version="1.0", source_event_id=source_event_id, session_id="sess-1",
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        trust_score=trust_score, trust_level=trust_level, human_review_required=False,
        review_reason=None, contributing_factors=[], explanation="test",
    )
    dr = DashboardResult(
        schema_version="1.0", source_event_id=source_event_id, session_id="sess-1",
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        gap_detected=False, gap_reason=None, fallback_detected=False,
        reliability_score=1.0, reliable=True, warnings=[],
        trust_score=trust_score, trust_level=trust_level,
        human_review_required=False, review_reason=None, contributing_factors=[],
        conversation_line=conversation_line, speaker=speaker, transcript=transcript,
    )
    return EventAggregator().aggregate(vr, tr, dr)


def _json_default(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


def _payload(event_aggregate):
    return json.loads(json.dumps(asdict(event_aggregate), default=_json_default))


class DashboardRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._saved_latest = api_server._dashboard_service.get_latest()
        api_server._dashboard_service._latest = None

    def tearDown(self):
        api_server._dashboard_service._latest = self._saved_latest

    def test_get_dashboard_is_404_before_anything_is_posted(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 404)

    def test_get_dashboard_returns_latest_dashboard_result_after_aggregate(self):
        ea = _event_aggregate(trust_score=0.55, trust_level="TRUSTED", source_event_id="evt-x")
        self.client.post("/aggregate", json=_payload(ea))

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["trust_score"], 0.55)
        self.assertEqual(body["source_event_id"], "evt-x")

    def test_get_dashboard_reflects_only_the_most_recent_aggregate_call(self):
        self.client.post("/aggregate", json=_payload(_event_aggregate(source_event_id="evt-1")))
        self.client.post("/aggregate", json=_payload(_event_aggregate(source_event_id="evt-2")))

        response = self.client.get("/dashboard")
        self.assertEqual(response.json()["source_event_id"], "evt-2")

    def test_index_is_html_before_anything_is_posted(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("No DashboardResult yet", response.text)

    def test_index_reflects_latest_dashboard_result_after_aggregate(self):
        ea = _event_aggregate(trust_score=0.73, trust_level="TRUSTED")
        self.client.post("/aggregate", json=_payload(ea))

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("0.730", response.text)

    def test_get_dashboard_json_includes_conversation_traceability_fields(self):
        # Conversation Traceability (docs/H4_RUNTIME_EVENT_CONTRACT.md,
        # "Runtime Event Metadata") must be available via GET /dashboard's
        # JSON, not only via the HTML view (GET /).
        ea = _event_aggregate(
            source_event_id="evt-conv",
            conversation_line=31,
            speaker="YOU",
            transcript="現在、利用人数はどのくらいを想定されていますか？",
        )
        self.client.post("/aggregate", json=_payload(ea))

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["conversation_line"], 31)
        self.assertEqual(body["speaker"], "YOU")
        self.assertEqual(body["transcript"], "現在、利用人数はどのくらいを想定されていますか？")

    def test_get_dashboard_json_conversation_fields_are_null_when_absent(self):
        ea = _event_aggregate(source_event_id="evt-no-conv")
        self.client.post("/aggregate", json=_payload(ea))

        response = self.client.get("/dashboard")
        body = response.json()
        self.assertIsNone(body["conversation_line"])
        self.assertIsNone(body["speaker"])
        self.assertIsNone(body["transcript"])

    def test_aggregate_response_shape_is_unaffected_by_dashboard_support(self):
        ea = _event_aggregate()
        response = self.client.post("/aggregate", json=_payload(ea))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body.keys()), {
            "schema_version", "source_event_id", "session_id", "timestamp",
            "verification_result", "trust_result", "dashboard_result",
        })


if __name__ == "__main__":
    unittest.main()
