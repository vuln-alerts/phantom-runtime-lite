"""
tests/test_h4_gemini_validation.py
=====================================
H4-9 Gemini Validation: validate the complete H4 pipeline using a real
Gemini API response, without modifying the existing Runtime or Provider
implementations.

    GeminiProvider.generate()  (real network call, existing H1-4 Provider)
        |
    ProviderResponse                      (existing provider.models contract)
        |
    Typed Event ("reply")                 (plain dict, per the frozen
        |                                  H4_RUNTIME_EVENT_CONTRACT.md
        |                                  envelope + "reply" payload --
        |                                  no new class introduced)
    Verification Runtime  -> VerificationResult
    Trust Runtime          -> TrustResult
    Dashboard Runtime      -> DashboardResult
    Event Aggregator       -> EventAggregate
    FastAPI (/aggregate)   -> JSON

This module reuses provider.gemini_provider.GeminiProvider and
provider.models.{Message, ProviderRequest, ProviderResponse} completely
unmodified -- it only calls the existing generate() method. The
conversion from ProviderResponse to a Typed Event dict is test-local glue
(a plain dict literal), not a new Runtime Contract or DTO: it reuses the
exact envelope and "reply" payload field names already frozen in
docs/H4_RUNTIME_EVENT_CONTRACT.md and already consumed by
verification.verification_runtime.VerificationRuntime. Structure mirrors
tests/test_h4_openai_validation.py (H4-8) exactly, substituting the
Gemini Provider for the OpenAI Provider -- both are downstream of the
same provider-independent ProviderResponse contract, so no Provider- or
Runtime-specific glue is required beyond that substitution.

Per the Contract's "Provider Rules" (H4_RUNTIME_EVENT_CONTRACT.md,
"reply" payload): provider is informational metadata only, and
finish_reason is treated as an opaque string. Gemini's finish_reason
values (e.g. "STOP", "MAX_TOKENS", via google.genai's FinishReason enum
.value) differ in case/vocabulary from OpenAI's ("stop", "length"), but
Verification Runtime only type-checks finish_reason as str and only
compares it case-insensitively against the literal "fallback" -- so the
same pipeline behavior (no gap, no fallback, TRUSTED) applies to a
well-formed Gemini reply exactly as it does to OpenAI's.

Live network requirement
-------------------------
Making a real Gemini request requires a real GEMINI_API_KEY. Per H4-9
scope, tests that place a live call are gated with
`unittest.skipUnless`: when GEMINI_API_KEY is not set in the
environment, they self-skip with an explicit reason instead of failing
or silently mocking the Provider (mocking would violate "Execute a real
Gemini request" and the project's Single Runtime Policy on mock
artifacts). Set GEMINI_API_KEY (and optionally RUNTIME_GEMINI_MODEL) in
the environment to exercise the live path.

Uses unittest (stdlib) plus fastapi.testclient.TestClient, consistent
with tests/test_h4_openai_validation.py and the rest of this project's
test suite: pytest is not a dependency.
"""

import ast
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
from provider.gemini_provider import GeminiProvider
from provider.models import Message, ProviderRequest, ProviderResponse
from trust.trust_result import TrustResult
from trust.trust_runtime import TrustRuntime
from verification.verification_result import VerificationResult
from verification.verification_runtime import VerificationRuntime

from api.api_server import app

_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
_MODEL = os.environ.get("RUNTIME_GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
_SKIP_REASON = (
    "GEMINI_API_KEY not set in this environment -- skipping live Gemini "
    "validation (H4-9 scope: real requests only, no mocked Provider). "
    "Set GEMINI_API_KEY to exercise this test."
)


# ---------------------------------------------------------------------------
# Typed Event construction — the H4-9 "convert the response into the
# existing Typed Event format" step. Plain dict, shaped per
# H4_RUNTIME_EVENT_CONTRACT.md's Runtime Event Envelope + "reply" payload.
# Not a new class/DTO: identical shape to tests/test_h4_openai_validation.py's
# _reply_event_from_provider_response(), specialized to wrap a real Gemini
# ProviderResponse instead of an OpenAI one -- both flow through the same
# provider-independent ProviderResponse shape.
# ---------------------------------------------------------------------------

def _reply_event_from_provider_response(
    response: ProviderResponse,
    *,
    provider_name: str,
    model: str,
    event_id: str,
    session_id: str,
    sequence: int,
) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": session_id,
        "sequence": sequence,
        "type": "reply",
        "payload": {
            "provider": provider_name,
            "model": model,
            "text": response.text,
            "finish_reason": response.finish_reason,
        },
    }


# ---------------------------------------------------------------------------
# Pipeline runner — identical wiring to tests/test_h4_integration.py's and
# tests/test_h4_openai_validation.py's _run_pipeline(): no integration glue
# module exists or is needed, since each stage's constructor/method already
# composes with the next.
# ---------------------------------------------------------------------------

def _run_pipeline(event, verification_runtime=None):
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


def _post_aggregate(client, event_aggregate):
    import json

    payload = json.loads(json.dumps(asdict(event_aggregate), default=_json_default))
    response = client.post("/aggregate", json=payload)
    return response, payload


def _assert_exact_field_set(testcase, dataclass_type, json_obj):
    expected = set(dataclass_type.__dataclass_fields__.keys())
    actual = set(json_obj.keys())
    testcase.assertEqual(
        expected, actual,
        f"{dataclass_type.__name__} JSON field set does not match its dataclass contract",
    )


@unittest.skipUnless(bool(_GEMINI_API_KEY), _SKIP_REASON)
class GeminiLiveRequestValidationTests(unittest.TestCase):
    """Places one real Gemini request through GeminiProvider.generate()
    (existing H1-4 Provider, unmodified) and drives the full H4 pipeline
    with it. Only runs when GEMINI_API_KEY is present.
    """

    @classmethod
    def setUpClass(cls):
        cls.provider = GeminiProvider(api_key=_GEMINI_API_KEY, model=_MODEL, timeout=20.0)
        cls.request = ProviderRequest(
            messages=[Message(role="user", content="Reply with exactly the single word: PONG")],
            temperature=0,
            max_tokens=5,
        )
        # Exactly one real network call for the whole class -- every test
        # below validates a different facet of the same live response,
        # rather than re-issuing redundant paid requests.
        cls.response = cls.provider.generate(cls.request)

    def test_provider_returns_a_real_provider_response(self):
        self.assertIsInstance(self.response, ProviderResponse)
        self.assertIsInstance(self.response.text, str)
        self.assertTrue(self.response.text.strip(), "Gemini returned empty text")

    def test_response_converts_into_contract_shaped_reply_event(self):
        event = _reply_event_from_provider_response(
            self.response, provider_name="gemini", model=_MODEL,
            event_id="evt-gemini-live", session_id="sess-gemini-live", sequence=1,
        )
        self.assertIsInstance(event, dict)
        self.assertEqual(event["type"], "reply")
        # Exactly the Contract's documented "reply" payload fields --
        # no extra, missing, or renamed keys.
        self.assertEqual(
            set(event["payload"].keys()), {"provider", "model", "text", "finish_reason"},
        )
        self.assertEqual(event["payload"]["text"], self.response.text)
        self.assertEqual(event["payload"]["finish_reason"], self.response.finish_reason)

    def test_real_reply_event_flows_through_full_pipeline(self):
        event = _reply_event_from_provider_response(
            self.response, provider_name="gemini", model=_MODEL,
            event_id="evt-gemini-pipeline", session_id="sess-gemini-pipeline", sequence=1,
        )
        vr, tr, dr, ea = _run_pipeline(event)

        self.assertIsInstance(vr, VerificationResult)
        self.assertIsInstance(tr, TrustResult)
        self.assertIsInstance(dr, DashboardResult)
        self.assertIsInstance(ea, EventAggregate)
        self.assertIs(ea.verification_result, vr)
        self.assertIs(ea.trust_result, tr)
        self.assertIs(ea.dashboard_result, dr)

        self.assertEqual(vr.source_event_id, "evt-gemini-pipeline")
        self.assertEqual(ea.source_event_id, "evt-gemini-pipeline")

        # A well-formed reply payload (all four Contract fields present,
        # correctly typed, finish_reason not case-insensitively equal to
        # "fallback") must not be flagged as a gap or a fallback -- this
        # holds regardless of which specific finish_reason Gemini
        # returned ("STOP", "MAX_TOKENS", etc.), so it is safe to assert
        # unconditionally.
        self.assertFalse(vr.gap_detected, vr.gap_reason)
        self.assertFalse(vr.fallback_detected)
        self.assertTrue(vr.reliable)
        self.assertEqual(vr.reliability_score, 1.0)

        # Trust Policy is deterministic given a clean VerificationResult:
        # reliability_score=1.0, no gap/fallback/warnings -> trust_score=1.0.
        self.assertEqual(tr.trust_score, 1.0)
        self.assertEqual(tr.trust_level, "TRUSTED")
        self.assertFalse(tr.human_review_required)

        # Dashboard/Aggregate must not recompute or rename any of the above.
        self.assertEqual(dr.trust_score, tr.trust_score)
        self.assertEqual(dr.trust_level, tr.trust_level)
        self.assertEqual(dr.reliability_score, vr.reliability_score)
        self.assertEqual(dr.gap_detected, vr.gap_detected)

    def test_real_reply_event_reaches_fastapi_as_lossless_json(self):
        client = TestClient(app)
        event = _reply_event_from_provider_response(
            self.response, provider_name="gemini", model=_MODEL,
            event_id="evt-gemini-json", session_id="sess-gemini-json", sequence=1,
        )
        vr, tr, dr, ea = _run_pipeline(event)
        response, _ = _post_aggregate(client, ea)

        self.assertEqual(response.status_code, 200)
        body = response.json()

        _assert_exact_field_set(self, EventAggregate, body)
        _assert_exact_field_set(self, VerificationResult, body["verification_result"])
        _assert_exact_field_set(self, TrustResult, body["trust_result"])
        _assert_exact_field_set(self, DashboardResult, body["dashboard_result"])

        self.assertEqual(body["source_event_id"], "evt-gemini-json")
        self.assertEqual(body["trust_result"]["trust_score"], tr.trust_score)
        self.assertEqual(body["trust_result"]["trust_level"], tr.trust_level)
        self.assertEqual(body["verification_result"]["reliability_score"], vr.reliability_score)
        self.assertEqual(body["dashboard_result"]["trust_score"], tr.trust_score)

    def test_sequential_live_session_preserves_ordering_across_stages(self):
        # A second real request in the same "session", one sequence number
        # later, must be seen as ordered (no regression) by a shared
        # VerificationRuntime instance -- confirming P4 (Event Ordering)
        # holds for a genuine multi-turn Gemini session, not just
        # synthetic events.
        shared_runtime = VerificationRuntime()
        first_event = _reply_event_from_provider_response(
            self.response, provider_name="gemini", model=_MODEL,
            event_id="evt-gemini-seq-1", session_id="sess-gemini-seq", sequence=1,
        )
        second_response = self.provider.generate(self.request)
        second_event = _reply_event_from_provider_response(
            second_response, provider_name="gemini", model=_MODEL,
            event_id="evt-gemini-seq-2", session_id="sess-gemini-seq", sequence=2,
        )

        _run_pipeline(first_event, verification_runtime=shared_runtime)
        vr2, tr2, dr2, ea2 = _run_pipeline(second_event, verification_runtime=shared_runtime)

        self.assertFalse(vr2.gap_detected, vr2.gap_reason)
        self.assertEqual(tr2.trust_level, "TRUSTED")


class GatingBehaviorTests(unittest.TestCase):
    """Always runs (never network-gated). Confirms the skip-if-absent
    contract itself behaves as approved, for the environment this test
    run actually has.
    """

    def test_live_tests_self_skip_without_a_real_api_key(self):
        if _GEMINI_API_KEY:
            self.skipTest(
                "GEMINI_API_KEY is set in this environment; the "
                "skip-if-absent path is not exercised by this run."
            )
        self.assertTrue(
            getattr(GeminiLiveRequestValidationTests, "__unittest_skip__", False),
            "live Gemini tests should be marked skipped when no API key is present",
        )
        self.assertIn(
            "GEMINI_API_KEY",
            getattr(GeminiLiveRequestValidationTests, "__unittest_skip_why__", ""),
        )

    def test_skip_reason_names_the_missing_credential(self):
        self.assertIn("GEMINI_API_KEY", _SKIP_REASON)


class NoNewContractOrProviderModificationTests(unittest.TestCase):
    """AST-based checks: H4-9 must not introduce a new Runtime Contract,
    a duplicate DTO, or any Provider/Runtime modification of its own.
    """

    def test_module_defines_no_new_dataclass(self):
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
                    "H4-9 validation must not introduce a new dataclass/DTO",
                )

    def test_module_never_imports_cloud_run_runtime_or_openai(self):
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
            "openai_provider", "openai",
        )
        for name in names:
            lowered = name.lower()
            for forbidden in forbidden_substrings:
                self.assertNotIn(
                    forbidden, lowered,
                    f"H4-9 validation test imported forbidden module: {name}",
                )

    def test_uses_the_real_unmodified_gemini_provider_class(self):
        import provider.gemini_provider as gemini_provider_module

        self.assertIs(GeminiProvider, gemini_provider_module.GeminiProvider)
        # generate() must remain the Provider's own method -- this test
        # module defines no subclass, wrapper, or monkeypatch of it.
        self.assertEqual(GeminiProvider.generate.__module__, "provider.gemini_provider")

    def test_event_aggregate_remains_the_single_contract_the_api_accepts(self):
        import aggregator.event_aggregate as event_aggregate_module
        import api.api_server as api_server_module

        self.assertIs(api_server_module.EventAggregate, event_aggregate_module.EventAggregate)


if __name__ == "__main__":
    unittest.main()
