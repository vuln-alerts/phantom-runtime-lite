"""
tests/test_api_server.py
===========================
Unit tests for the H4-6 FastAPI Presentation Layer (api.api_server.app).

Uses unittest (stdlib) plus fastapi.testclient.TestClient, consistent
with tests/test_verification_runtime.py, tests/test_trust_runtime.py,
tests/test_dashboard_runtime.py, and tests/test_event_aggregator.py:
pytest is not a dependency of this project.

Per the H4-6 design revision, api_server.py holds no mirror DTO of
EventAggregate — it converts the EventAggregate it receives to JSON via
dataclasses.asdict() and nothing else. These tests verify that
conversion is value-preserving and that the API layer is stateless and
free of any import-time coupling to the Cloud Run Runtime, Provider,
Whisper, or any of the Verification/Trust/Dashboard Runtimes or the
Event Aggregator.
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
from trust.trust_result import TrustResult
from verification.verification_result import VerificationResult

from api.api_server import app


def _verification_result(
    reliable=True,
    reliability_score=1.0,
    gap_detected=False,
    gap_reason=None,
    fallback_detected=False,
    fallback_reason=None,
    warnings=None,
    source_event_id="evt-1",
    session_id="sess-1",
):
    return VerificationResult(
        schema_version="1.0",
        source_event_id=source_event_id,
        session_id=session_id,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        gap_detected=gap_detected,
        gap_reason=gap_reason,
        fallback_detected=fallback_detected,
        fallback_reason=fallback_reason,
        reliable=reliable,
        reliability_score=reliability_score,
        warnings=warnings if warnings is not None else [],
        explanation="test verification explanation",
    )


def _trust_result(
    trust_score=0.87,
    trust_level="TRUSTED",
    human_review_required=False,
    review_reason=None,
    contributing_factors=None,
    source_event_id="evt-1",
    session_id="sess-1",
):
    return TrustResult(
        schema_version="1.0",
        source_event_id=source_event_id,
        session_id=session_id,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        trust_score=trust_score,
        trust_level=trust_level,
        human_review_required=human_review_required,
        review_reason=review_reason,
        contributing_factors=contributing_factors if contributing_factors is not None else [],
        explanation="test trust explanation",
    )


def _dashboard_result(
    gap_detected=False,
    gap_reason=None,
    fallback_detected=False,
    reliability_score=1.0,
    reliable=True,
    warnings=None,
    trust_score=0.87,
    trust_level="TRUSTED",
    human_review_required=False,
    review_reason=None,
    contributing_factors=None,
    source_event_id="evt-1",
    session_id="sess-1",
):
    return DashboardResult(
        schema_version="1.0",
        source_event_id=source_event_id,
        session_id=session_id,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        gap_detected=gap_detected,
        gap_reason=gap_reason,
        fallback_detected=fallback_detected,
        reliability_score=reliability_score,
        reliable=reliable,
        warnings=warnings if warnings is not None else [],
        trust_score=trust_score,
        trust_level=trust_level,
        human_review_required=human_review_required,
        review_reason=review_reason,
        contributing_factors=contributing_factors if contributing_factors is not None else [],
    )


def _event_aggregate(**overrides):
    vr = _verification_result(
        source_event_id=overrides.get("source_event_id", "evt-1"),
        session_id=overrides.get("session_id", "sess-1"),
        gap_detected=overrides.get("gap_detected", False),
        gap_reason=overrides.get("gap_reason", None),
        warnings=overrides.get("warnings", None),
    )
    tr = _trust_result(
        source_event_id=overrides.get("source_event_id", "evt-1"),
        session_id=overrides.get("session_id", "sess-1"),
        trust_score=overrides.get("trust_score", 0.87),
        trust_level=overrides.get("trust_level", "TRUSTED"),
        contributing_factors=overrides.get("contributing_factors", None),
    )
    dr = _dashboard_result(
        source_event_id=overrides.get("source_event_id", "evt-1"),
        session_id=overrides.get("session_id", "sess-1"),
        trust_score=overrides.get("trust_score", 0.87),
        trust_level=overrides.get("trust_level", "TRUSTED"),
    )
    return EventAggregator().aggregate(vr, tr, dr)


def _json_default(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


def _payload(event_aggregate):
    """Serialize an EventAggregate the way a real HTTP client would:
    dataclasses.asdict() followed by a JSON round trip (datetimes become
    ISO-8601 strings, exactly as a client's JSON encoder would produce).
    """
    return json.loads(json.dumps(asdict(event_aggregate), default=_json_default))


def _normalize_timestamps(value):
    """Recursively parse ISO-8601 "timestamp" strings into datetimes so
    two equivalent instants (e.g. "...+00:00" vs "...Z", both valid
    ISO-8601 renderings of the same instant) compare equal regardless of
    which shorthand a given JSON encoder chose.
    """
    if isinstance(value, dict):
        return {
            key: (
                datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
                if key == "timestamp" and isinstance(val, str)
                else _normalize_timestamps(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_normalize_timestamps(item) for item in value]
    return value


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_returns_ok(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class AggregateEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_aggregate_returns_200(self):
        ea = _event_aggregate()
        response = self.client.post("/aggregate", json=_payload(ea))
        self.assertEqual(response.status_code, 200)

    def test_aggregate_response_is_json_conversion_of_input(self):
        ea = _event_aggregate(trust_score=0.42, trust_level="UNTRUSTED")
        payload = _payload(ea)
        response = self.client.post("/aggregate", json=payload)
        body = response.json()
        self.assertEqual(body["source_event_id"], ea.source_event_id)
        self.assertEqual(body["session_id"], ea.session_id)
        self.assertEqual(body["verification_result"]["reliability_score"], ea.verification_result.reliability_score)
        self.assertEqual(body["trust_result"]["trust_score"], 0.42)
        self.assertEqual(body["trust_result"]["trust_level"], "UNTRUSTED")
        self.assertEqual(body["dashboard_result"]["trust_score"], 0.42)

    def test_aggregate_does_not_alter_values(self):
        # Distinct, easily-distinguished values on every field group so
        # any accidental renaming/coercion/recomputation would surface.
        ea = _event_aggregate(
            source_event_id="evt-distinct",
            session_id="sess-distinct",
            gap_detected=True,
            gap_reason="missing field",
            warnings=["w1", "w2"],
            trust_score=0.13,
            trust_level="LOW",
            contributing_factors=["f1", "f2"],
        )
        payload = _payload(ea)
        response = self.client.post("/aggregate", json=payload)
        body = response.json()

        self.assertEqual(_normalize_timestamps(body), _normalize_timestamps(payload))

        self.assertTrue(body["verification_result"]["gap_detected"])
        self.assertEqual(body["verification_result"]["gap_reason"], "missing field")
        self.assertEqual(body["verification_result"]["warnings"], ["w1", "w2"])
        self.assertEqual(body["trust_result"]["trust_score"], 0.13)
        self.assertEqual(body["trust_result"]["trust_level"], "LOW")
        self.assertEqual(body["trust_result"]["contributing_factors"], ["f1", "f2"])

    def test_aggregate_does_not_mutate_the_original_object(self):
        ea = _event_aggregate(trust_score=0.55)
        before = asdict(ea)
        self.client.post("/aggregate", json=_payload(ea))
        after = asdict(ea)
        self.assertEqual(before, after)

    def test_aggregate_performs_no_computation_or_correction(self):
        # An internally "inconsistent" pair (gap detected but reliability
        # high) must pass through unchanged -- H4-6 must not re-derive,
        # clamp, or validate against Trust Policy / Verification logic.
        ea = _event_aggregate(gap_detected=True, gap_reason="inconsistent on purpose")
        response = self.client.post("/aggregate", json=_payload(ea))
        body = response.json()
        self.assertTrue(body["verification_result"]["gap_detected"])
        self.assertEqual(body["verification_result"]["reliability_score"], ea.verification_result.reliability_score)


class StatelessnessTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_independent_requests_do_not_leak_state(self):
        first = _event_aggregate(source_event_id="evt-a", trust_score=0.9)
        second = _event_aggregate(source_event_id="evt-b", trust_score=0.1)

        r1 = self.client.post("/aggregate", json=_payload(first))
        r2 = self.client.post("/aggregate", json=_payload(second))

        self.assertEqual(r1.json()["source_event_id"], "evt-a")
        self.assertEqual(r1.json()["trust_result"]["trust_score"], 0.9)
        self.assertEqual(r2.json()["source_event_id"], "evt-b")
        self.assertEqual(r2.json()["trust_result"]["trust_score"], 0.1)

    def test_repeated_identical_requests_are_deterministic(self):
        ea = _event_aggregate(source_event_id="evt-repeat")
        payload = _payload(ea)
        r1 = self.client.post("/aggregate", json=payload)
        r2 = self.client.post("/aggregate", json=payload)
        self.assertEqual(r1.json(), r2.json())

    def test_health_holds_no_state_across_calls(self):
        r1 = self.client.get("/health")
        r2 = self.client.get("/health")
        self.assertEqual(r1.json(), r2.json())

    def test_aggregate_updates_dashboard_as_a_side_effect(self):
        # /aggregate's own request/response contract stays independent per
        # request (see the other tests in this class); this only verifies
        # the one intentional exception: DashboardService's single latest
        # slot, which GET /dashboard reads back. See api.dashboard_service.
        ea = _event_aggregate(trust_score=0.65, trust_level="TRUSTED")
        self.client.post("/aggregate", json=_payload(ea))
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trust_score"], 0.65)
        self.assertEqual(response.json()["trust_level"], "TRUSTED")


class DependencyTests(unittest.TestCase):
    """AST-based dependency inspection: the H4-6 Presentation Layer must
    import only the EventAggregate data contract itself, FastAPI/pydantic,
    and the stdlib -- never a Runtime, Provider, or Whisper.
    """

    @staticmethod
    def _imported_names(module_file):
        with open(module_file, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
                names.update(alias.name for alias in node.names)
        return names

    def _assert_no_forbidden_imports(self, module_file):
        imported = self._imported_names(module_file)
        forbidden_substrings = (
            "phantom_runtime",
            "cloud_run_shell",
            "transport_gateway",
            "runtimeevent",
            "provider",
            "whisper",
            "openai",
            "generativeai",
            "verification_runtime",
            "verificationruntime",
            "trust_runtime",
            "trustruntime",
            "dashboard_runtime",
            "dashboardruntime",
            "event_aggregator",
            "eventaggregator",
        )
        for name in imported:
            lowered = name.lower()
            for forbidden in forbidden_substrings:
                self.assertNotIn(
                    forbidden, lowered,
                    f"forbidden import '{name}' found in {module_file}",
                )

    def test_api_server_has_no_forbidden_dependencies(self):
        import api.api_server as api_server_module

        self._assert_no_forbidden_imports(api_server_module.__file__)

    def test_api_models_has_no_forbidden_dependencies(self):
        import api.api_models as api_models_module

        self._assert_no_forbidden_imports(api_models_module.__file__)

    def test_api_models_does_not_redefine_event_aggregate_shape(self):
        # Per the H4-6 design revision, api_models.py must not carry a
        # mirror DTO of EventAggregate/VerificationResult/TrustResult/
        # DashboardResult -- the only Runtime Contract source is
        # aggregator.event_aggregate.EventAggregate itself.
        import api.api_models as api_models_module

        forbidden_names = (
            "EventAggregateModel",
            "VerificationResultModel",
            "TrustResultModel",
            "DashboardResultModel",
        )
        for name in forbidden_names:
            self.assertFalse(
                hasattr(api_models_module, name),
                f"api_models.py must not redefine {name}",
            )

    def test_api_server_imports_event_aggregate_directly(self):
        import api.api_server as api_server_module

        imported = self._imported_names(api_server_module.__file__)
        self.assertIn("aggregator.event_aggregate", imported)

    def test_api_server_only_imports_expected_modules(self):
        import api.api_server as api_server_module

        imported = self._imported_names(api_server_module.__file__)
        allowed_prefixes = (
            "dataclasses",
            "asdict",
            "fastapi",
            "FastAPI",
            "HTTPException",
            "fastapi.responses",
            "HTMLResponse",
            "aggregator.event_aggregate",
            "EventAggregate",
            "api.api_models",
            "HealthResponse",
            "api.dashboard_service",
            "DashboardService",
            "api.dashboard_view",
            "render_dashboard_html",
        )
        for name in imported:
            self.assertTrue(
                any(name == prefix or name.startswith(prefix + ".") for prefix in allowed_prefixes),
                f"unexpected import in api_server.py: {name}",
            )

    def test_api_models_only_imports_expected_modules(self):
        import api.api_models as api_models_module

        imported = self._imported_names(api_models_module.__file__)
        allowed_prefixes = ("pydantic", "BaseModel")
        for name in imported:
            self.assertTrue(
                any(name == prefix or name.startswith(prefix + ".") for prefix in allowed_prefixes),
                f"unexpected import in api_models.py: {name}",
            )


if __name__ == "__main__":
    unittest.main()
