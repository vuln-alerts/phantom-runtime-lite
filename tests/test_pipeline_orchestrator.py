"""
tests/test_pipeline_orchestrator.py
======================================
Unit tests for runtime.pipeline_orchestrator.RuntimePipelineOrchestrator.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency of this project. Raw event fixtures are
redefined inline here per this project's existing convention of
self-contained test modules (see tests/test_h4_10_integration_validation.py).
"""

import datetime
import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime.event_adapter import RuntimeEventAdapter
from runtime.pipeline_orchestrator import PipelineOutcome, RuntimePipelineOrchestrator
from verification.verification_runtime import VerificationRuntime

_TS = "2026-07-06T12:00:00+00:00"


def _raw_event(event_type, payload, timestamp=_TS):
    """Same envelope shape _emit_event() constructs (phantom_runtime.py:597-602)."""
    return {"version": 1, "type": event_type, "timestamp": timestamp, "payload": payload}


RAW_FIXTURES = {
    "status": _raw_event("status", {"state": "recruiter_speaking", "previous": "idle"}),
    "error": _raw_event("error", {"label": "Audio", "message": "fd read failed: EOF"}),
    "latency": _raw_event("latency", {"stt_ms": 120.0, "gpt_ms": 300.0, "total_ms": 420.0}),
    "reply": _raw_event(
        "reply", {"text": "Nice to meet you", "lang": "en", "speaker": "agent", "ts": 1720000001.0}
    ),
    "transcript": _raw_event(
        "transcript", {"text": "hello there", "lang": "en", "ts": 1720000000.0, "speaker": "user"}
    ),
    "analysis": _raw_event("analysis", {"text": "User introduced themselves; no risks detected."}),
}
RAW_UNKNOWN_TYPE = _raw_event("future_event", {"anything": 1})


def _run_pipeline(raw_event, adapter=None, verification_runtime=None):
    """Same processing order used to validate RuntimePipelineOrchestrator
    against -- mirrors tests/test_h4_10_integration_validation.py:127-136.
    """
    from aggregator.event_aggregator import EventAggregator
    from dashboard.dashboard_runtime import DashboardRuntime
    from trust.trust_runtime import TrustRuntime

    adapter = adapter if adapter is not None else RuntimeEventAdapter()
    vr_runtime = verification_runtime if verification_runtime is not None else VerificationRuntime()

    event = adapter.translate(raw_event)
    verification_result = vr_runtime.handle(event)
    trust_result = TrustRuntime().handle(verification_result)
    dashboard_result = DashboardRuntime().render(verification_result, trust_result)
    event_aggregate = EventAggregator().aggregate(verification_result, trust_result, dashboard_result)
    return event, verification_result, trust_result, dashboard_result, event_aggregate


class RunReturnsCompleteOutcomeTests(unittest.TestCase):
    def test_run_returns_pipeline_outcome_with_all_fields_populated(self):
        outcome = RuntimePipelineOrchestrator().run(RAW_FIXTURES["transcript"])
        self.assertIsInstance(outcome, PipelineOutcome)
        self.assertIsNotNone(outcome.event)
        self.assertIsNotNone(outcome.verification_result)
        self.assertIsNotNone(outcome.trust_result)
        self.assertIsNotNone(outcome.dashboard_result)
        self.assertIsNotNone(outcome.event_aggregate)

    def test_all_known_fixture_types_and_one_unknown_type_run_without_raising(self):
        orchestrator = RuntimePipelineOrchestrator()
        for name, raw_event in {**RAW_FIXTURES, "future_event": RAW_UNKNOWN_TYPE}.items():
            with self.subTest(fixture=name):
                outcome = orchestrator.run(raw_event)
                self.assertIsInstance(outcome, PipelineOutcome)


class InjectedDependenciesAreUsedTests(unittest.TestCase):
    def test_injected_adapter_is_used(self):
        calls = []

        class SpyAdapter:
            def translate(self, raw_event):
                calls.append(raw_event)
                return RuntimeEventAdapter().translate(raw_event)

        orchestrator = RuntimePipelineOrchestrator(adapter=SpyAdapter())
        orchestrator.run(RAW_FIXTURES["transcript"])
        self.assertEqual(calls, [RAW_FIXTURES["transcript"]])

    def test_injected_verification_runtime_is_used(self):
        calls = []
        real = VerificationRuntime()

        class SpyVerificationRuntime:
            def handle(self, event):
                calls.append(event)
                return real.handle(event)

        orchestrator = RuntimePipelineOrchestrator(verification_runtime=SpyVerificationRuntime())
        orchestrator.run(RAW_FIXTURES["transcript"])
        self.assertEqual(len(calls), 1)


def _complete_transcript_event(timestamp):
    """A transcript payload with every Contract-required field present
    (text, language, confidence, is_final), unlike RAW_FIXTURES["transcript"]
    which -- like the real Runtime's actual wire shape -- always trips Gap
    Detection on missing fields. `confidence`/`is_final` aren't renamed by
    RuntimeEventAdapter's field map, so passing them under their Contract
    names carries them through unchanged.
    """
    return _raw_event(
        "transcript",
        {"text": "hello", "lang": "en", "confidence": 0.95, "is_final": True},
        timestamp=timestamp,
    )


class SessionStateIsReusedAcrossCallsTests(unittest.TestCase):
    def test_gap_detection_sees_timestamp_regression_across_two_run_calls(self):
        # A single orchestrator instance must reuse its VerificationRuntime
        # across calls, or session-scoped Gap Detection (which relies on
        # VerificationRuntime's own timestamp bookkeeping) would never have
        # any state to compare against. RuntimeEventAdapter self-assigns a
        # monotonically increasing `sequence`, so only `timestamp` (taken
        # verbatim from the raw event) can regress here.
        orchestrator = RuntimePipelineOrchestrator()
        first = _complete_transcript_event("2026-07-06T12:00:00+00:00")
        second = _complete_transcript_event("2026-07-06T11:59:00+00:00")  # earlier than `first`

        outcome_1 = orchestrator.run(first)
        outcome_2 = orchestrator.run(second)

        self.assertFalse(outcome_1.verification_result.gap_detected)
        self.assertTrue(outcome_2.verification_result.gap_detected)


def _raw_event_with_metadata(event_type, payload, metadata, timestamp=_TS):
    """Same envelope shape as _raw_event(), plus the optional Runtime Event
    "metadata" field (docs/H4_RUNTIME_EVENT_CONTRACT.md, "Runtime Event
    Metadata")."""
    return {
        "version": 1, "type": event_type, "timestamp": timestamp,
        "payload": payload, "metadata": metadata,
    }


class ConversationTraceabilityTests(unittest.TestCase):
    def test_metadata_conversation_fields_propagate_to_dashboard_result(self):
        raw_event = _raw_event_with_metadata(
            "transcript",
            {"text": "hello there", "lang": "en", "ts": 1720000000.0, "speaker": "user"},
            {"conversation_line": 31, "speaker": "YOU", "transcript": "現在、利用人数はどのくらいを想定されていますか？"},
        )
        outcome = RuntimePipelineOrchestrator().run(raw_event)
        self.assertEqual(outcome.dashboard_result.conversation_line, 31)
        self.assertEqual(outcome.dashboard_result.speaker, "YOU")
        self.assertEqual(
            outcome.dashboard_result.transcript,
            "現在、利用人数はどのくらいを想定されていますか？",
        )

    def test_missing_metadata_key_yields_none_conversation_fields(self):
        outcome = RuntimePipelineOrchestrator().run(RAW_FIXTURES["transcript"])
        self.assertIsNone(outcome.dashboard_result.conversation_line)
        self.assertIsNone(outcome.dashboard_result.speaker)
        self.assertIsNone(outcome.dashboard_result.transcript)

    def test_empty_metadata_dict_yields_none_conversation_fields(self):
        raw_event = _raw_event_with_metadata(
            "transcript",
            {"text": "hello there", "lang": "en", "ts": 1720000000.0, "speaker": "user"},
            {},
        )
        outcome = RuntimePipelineOrchestrator().run(raw_event)
        self.assertIsNone(outcome.dashboard_result.conversation_line)
        self.assertIsNone(outcome.dashboard_result.speaker)
        self.assertIsNone(outcome.dashboard_result.transcript)

    def test_metadata_never_affects_verification_or_trust_results(self):
        # VerificationRuntime/TrustRuntime logic must be unaffected by the
        # presence of Conversation Traceability metadata.
        without_metadata = RuntimePipelineOrchestrator().run(RAW_FIXTURES["transcript"])
        with_metadata = RuntimePipelineOrchestrator().run(
            _raw_event_with_metadata(
                "transcript",
                RAW_FIXTURES["transcript"]["payload"],
                {"conversation_line": 1, "speaker": "YOU", "transcript": "x"},
            )
        )
        self.assertEqual(
            without_metadata.verification_result.gap_detected,
            with_metadata.verification_result.gap_detected,
        )
        self.assertEqual(
            without_metadata.verification_result.reliability_score,
            with_metadata.verification_result.reliability_score,
        )
        self.assertEqual(
            without_metadata.trust_result.trust_score,
            with_metadata.trust_result.trust_score,
        )


class MatchesReferencePipelineTests(unittest.TestCase):
    # source_event_id/session_id/timestamp are excluded: both _run_pipeline()
    # and RuntimePipelineOrchestrator().run() build their own fresh
    # RuntimeEventAdapter() by default, and translate() assigns a new
    # random event_id (and this test's adapters a new random session_id)
    # on every call -- by design, not something either side computes from
    # Runtime logic. Everything Verification/Trust/Dashboard actually
    # judge is compared, which is what demonstrates the two call paths run
    # identical, unmodified Runtime logic.
    _IDENTITY_FIELDS = {"source_event_id", "session_id", "timestamp"}

    def test_matches_run_pipeline_dashboard_result(self):
        raw_event = RAW_FIXTURES["reply"]

        _, _, _, expected_dashboard_result, _ = _run_pipeline(raw_event)
        actual = RuntimePipelineOrchestrator().run(raw_event).dashboard_result

        expected_fields = {
            k: v for k, v in expected_dashboard_result.__dict__.items() if k not in self._IDENTITY_FIELDS
        }
        actual_fields = {
            k: v for k, v in actual.__dict__.items() if k not in self._IDENTITY_FIELDS
        }
        self.assertEqual(expected_fields, actual_fields)
        self.assertIsInstance(actual.timestamp, datetime.datetime)


if __name__ == "__main__":
    unittest.main()
