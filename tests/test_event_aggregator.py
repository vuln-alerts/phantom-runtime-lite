"""
tests/test_event_aggregator.py
=================================
Unit tests for the H4-5 Event Aggregator (aggregator.event_aggregator.EventAggregator).

Uses unittest (stdlib), consistent with tests/test_verification_runtime.py,
tests/test_trust_runtime.py, and tests/test_dashboard_runtime.py: pytest is
not a dependency of this project.
"""

import ast
import datetime
import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from aggregator.event_aggregate import EventAggregate
from aggregator.event_aggregator import EventAggregator
from dashboard.dashboard_result import DashboardResult
from trust.trust_result import TrustResult
from verification.verification_result import VerificationResult


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
    trust_score=1.0,
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
    trust_score=1.0,
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


class EventAggregateCreationTests(unittest.TestCase):
    def test_aggregate_produces_event_aggregate(self):
        agg = EventAggregator()
        result = agg.aggregate(_verification_result(), _trust_result(), _dashboard_result())
        self.assertIsInstance(result, EventAggregate)

    def test_aggregate_does_not_return_an_input_type(self):
        agg = EventAggregator()
        result = agg.aggregate(_verification_result(), _trust_result(), _dashboard_result())
        self.assertNotIsInstance(result, VerificationResult)
        self.assertNotIsInstance(result, TrustResult)
        self.assertNotIsInstance(result, DashboardResult)

    def test_event_aggregate_is_not_a_subclass_of_inputs(self):
        self.assertFalse(issubclass(EventAggregate, VerificationResult))
        self.assertFalse(issubclass(EventAggregate, TrustResult))
        self.assertFalse(issubclass(EventAggregate, DashboardResult))


class ObjectReferencePreservationTests(unittest.TestCase):
    def test_verification_result_reference_preserved(self):
        agg = EventAggregator()
        vr = _verification_result()
        result = agg.aggregate(vr, _trust_result(), _dashboard_result())
        self.assertIs(result.verification_result, vr)

    def test_trust_result_reference_preserved(self):
        agg = EventAggregator()
        tr = _trust_result()
        result = agg.aggregate(_verification_result(), tr, _dashboard_result())
        self.assertIs(result.trust_result, tr)

    def test_dashboard_result_reference_preserved(self):
        agg = EventAggregator()
        dr = _dashboard_result()
        result = agg.aggregate(_verification_result(), _trust_result(), dr)
        self.assertIs(result.dashboard_result, dr)

    def test_dashboard_result_unchanged(self):
        agg = EventAggregator()
        dr = _dashboard_result(trust_score=0.3, trust_level="UNTRUSTED")
        result = agg.aggregate(_verification_result(), _trust_result(), dr)
        self.assertEqual(result.dashboard_result.trust_score, 0.3)
        self.assertEqual(result.dashboard_result.trust_level, "UNTRUSTED")

    def test_verification_result_unchanged(self):
        agg = EventAggregator()
        vr = _verification_result(gap_detected=True, gap_reason="missing field")
        result = agg.aggregate(vr, _trust_result(), _dashboard_result())
        self.assertTrue(result.verification_result.gap_detected)
        self.assertEqual(result.verification_result.gap_reason, "missing field")

    def test_trust_result_unchanged(self):
        agg = EventAggregator()
        tr = _trust_result(human_review_required=True, review_reason="x")
        result = agg.aggregate(_verification_result(), tr, _dashboard_result())
        self.assertTrue(result.trust_result.human_review_required)
        self.assertEqual(result.trust_result.review_reason, "x")


class IdentityPropagationTests(unittest.TestCase):
    def test_source_event_id_and_session_id_propagated(self):
        agg = EventAggregator()
        vr = _verification_result(source_event_id="evt-42", session_id="sess-42")
        tr = _trust_result(source_event_id="evt-42", session_id="sess-42")
        dr = _dashboard_result(source_event_id="evt-42", session_id="sess-42")
        result = agg.aggregate(vr, tr, dr)
        self.assertEqual(result.source_event_id, "evt-42")
        self.assertEqual(result.session_id, "sess-42")

    def test_identity_matches_inputs_when_ids_absent(self):
        agg = EventAggregator()
        vr = _verification_result(source_event_id=None, session_id=None)
        tr = _trust_result(source_event_id=None, session_id=None)
        dr = _dashboard_result(source_event_id=None, session_id=None)
        result = agg.aggregate(vr, tr, dr)
        self.assertIsNone(result.source_event_id)
        self.assertIsNone(result.session_id)

    def test_schema_version_is_present(self):
        agg = EventAggregator()
        result = agg.aggregate(_verification_result(), _trust_result(), _dashboard_result())
        self.assertEqual(result.schema_version, "1.0")

    def test_timestamp_is_freshly_generated(self):
        agg = EventAggregator()
        before = datetime.datetime.now(datetime.timezone.utc)
        result = agg.aggregate(_verification_result(), _trust_result(), _dashboard_result())
        after = datetime.datetime.now(datetime.timezone.utc)
        self.assertLessEqual(before, result.timestamp)
        self.assertLessEqual(result.timestamp, after)


class NoMutationTests(unittest.TestCase):
    def test_aggregate_does_not_mutate_verification_result(self):
        agg = EventAggregator()
        vr = _verification_result(warnings=["w1"])
        before = dict(vr.__dict__)
        agg.aggregate(vr, _trust_result(), _dashboard_result())
        self.assertEqual(vr.__dict__, before)

    def test_aggregate_does_not_mutate_trust_result(self):
        agg = EventAggregator()
        tr = _trust_result(contributing_factors=["f1"])
        before = dict(tr.__dict__)
        agg.aggregate(_verification_result(), tr, _dashboard_result())
        self.assertEqual(tr.__dict__, before)

    def test_aggregate_does_not_mutate_dashboard_result(self):
        agg = EventAggregator()
        dr = _dashboard_result(warnings=["w1"], contributing_factors=["f1"])
        before = dict(dr.__dict__)
        agg.aggregate(_verification_result(), _trust_result(), dr)
        self.assertEqual(dr.__dict__, before)


class ImmutabilityTests(unittest.TestCase):
    def test_event_aggregate_is_frozen(self):
        agg = EventAggregator()
        result = agg.aggregate(_verification_result(), _trust_result(), _dashboard_result())
        with self.assertRaises(Exception):
            result.trust_result = _trust_result()  # type: ignore[misc]

    def test_event_aggregate_is_a_frozen_dataclass(self):
        agg = EventAggregator()
        result = agg.aggregate(_verification_result(), _trust_result(), _dashboard_result())
        self.assertTrue(result.__dataclass_params__.frozen)

    def test_event_aggregate_does_not_flatten_input_fields(self):
        # EventAggregate must not copy every field into itself — only
        # references to the three result objects, plus identity metadata.
        agg = EventAggregator()
        result = agg.aggregate(_verification_result(), _trust_result(), _dashboard_result())
        result_fields = result.__dataclass_fields__
        for forbidden in ("trust_score", "trust_level", "reliability_score", "gap_detected"):
            self.assertNotIn(forbidden, result_fields)


class StatelessnessTests(unittest.TestCase):
    def test_event_aggregator_holds_no_instance_state(self):
        agg = EventAggregator()
        self.assertEqual(vars(agg), {})

    def test_multiple_aggregate_calls_are_independent(self):
        agg = EventAggregator()
        first = agg.aggregate(
            _verification_result(source_event_id="evt-a"),
            _trust_result(source_event_id="evt-a"),
            _dashboard_result(source_event_id="evt-a"),
        )
        second = agg.aggregate(
            _verification_result(source_event_id="evt-b"),
            _trust_result(source_event_id="evt-b"),
            _dashboard_result(source_event_id="evt-b"),
        )
        self.assertEqual(first.source_event_id, "evt-a")
        self.assertEqual(second.source_event_id, "evt-b")

    def test_aggregate_is_deterministic_apart_from_timestamp(self):
        agg = EventAggregator()
        vr = _verification_result()
        tr = _trust_result()
        dr = _dashboard_result()
        first = agg.aggregate(vr, tr, dr)
        second = agg.aggregate(vr, tr, dr)
        self.assertIs(first.verification_result, second.verification_result)
        self.assertIs(first.trust_result, second.trust_result)
        self.assertIs(first.dashboard_result, second.dashboard_result)
        self.assertEqual(first.source_event_id, second.source_event_id)
        self.assertEqual(first.session_id, second.session_id)


class DependencyTests(unittest.TestCase):
    """AST-based dependency inspection: aggregator modules must import only
    their own aggregate data and the three upstream result data contracts
    (VerificationResult, TrustResult, DashboardResult) — never the runtimes
    that produce them, never RuntimeEvent, and never anything from the
    Cloud Run Runtime, Provider, or Whisper layers.
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

    def test_event_aggregator_does_not_import_verification_runtime(self):
        import aggregator.event_aggregator as event_aggregator_module

        imported = self._imported_names(event_aggregator_module.__file__)
        self.assertNotIn("verification_runtime", imported)
        self.assertNotIn("VerificationRuntime", imported)
        self.assertFalse(any("verification_runtime" in name for name in imported))
        self.assertFalse(hasattr(event_aggregator_module, "VerificationRuntime"))

    def test_event_aggregator_does_not_import_trust_runtime(self):
        import aggregator.event_aggregator as event_aggregator_module

        imported = self._imported_names(event_aggregator_module.__file__)
        self.assertNotIn("trust_runtime", imported)
        self.assertNotIn("TrustRuntime", imported)
        self.assertFalse(any("trust_runtime" in name for name in imported))
        self.assertFalse(hasattr(event_aggregator_module, "TrustRuntime"))

    def test_event_aggregator_does_not_import_dashboard_runtime(self):
        import aggregator.event_aggregator as event_aggregator_module

        imported = self._imported_names(event_aggregator_module.__file__)
        self.assertNotIn("dashboard_runtime", imported)
        self.assertNotIn("DashboardRuntime", imported)
        self.assertFalse(any("dashboard_runtime" in name for name in imported))
        self.assertFalse(hasattr(event_aggregator_module, "DashboardRuntime"))

    def test_event_aggregator_does_not_import_runtime_event_or_cloud_run_runtime(self):
        import aggregator.event_aggregator as event_aggregator_module

        imported = self._imported_names(event_aggregator_module.__file__)
        for forbidden in (
            "phantom_runtime", "cloud_run_shell", "runtime.cloud_run_shell", "RuntimeEvent",
        ):
            self.assertNotIn(forbidden, imported)
            self.assertFalse(any(forbidden in name for name in imported))

    def test_event_aggregator_does_not_import_provider(self):
        import aggregator.event_aggregator as event_aggregator_module

        imported = self._imported_names(event_aggregator_module.__file__)
        self.assertFalse(any("provider" in name.lower() for name in imported))

    def test_event_aggregator_does_not_import_whisper(self):
        import aggregator.event_aggregator as event_aggregator_module

        imported = self._imported_names(event_aggregator_module.__file__)
        self.assertFalse(any("whisper" in name.lower() for name in imported))

    def test_event_aggregator_does_not_import_openai_or_gemini(self):
        import aggregator.event_aggregator as event_aggregator_module

        imported = self._imported_names(event_aggregator_module.__file__)
        for forbidden in ("openai", "google.generativeai", "fastapi"):
            self.assertFalse(any(forbidden in name.lower() for name in imported))

    def test_event_aggregate_module_has_no_forbidden_dependencies(self):
        import aggregator.event_aggregate as event_aggregate_module

        imported = self._imported_names(event_aggregate_module.__file__)
        forbidden_substrings = (
            "phantom_runtime", "cloud_run_shell", "provider", "whisper",
            "verification_runtime", "trust_runtime", "dashboard_runtime",
            "openai", "generativeai", "fastapi",
        )
        for name in imported:
            lowered = name.lower()
            for forbidden in forbidden_substrings:
                self.assertNotIn(forbidden, lowered)

    def test_event_aggregator_only_imports_expected_modules(self):
        import aggregator.event_aggregator as event_aggregator_module

        imported = self._imported_names(event_aggregator_module.__file__)
        allowed_prefixes = (
            "datetime",
            "timezone",
            "aggregator.event_aggregate",
            "event_aggregate",
            "EventAggregate",
            "dashboard.dashboard_result",
            "dashboard_result",
            "DashboardResult",
            "trust.trust_result",
            "trust_result",
            "TrustResult",
            "verification.verification_result",
            "verification_result",
            "VerificationResult",
        )
        for name in imported:
            self.assertTrue(
                any(name == prefix or name.startswith(prefix + ".") for prefix in allowed_prefixes),
                f"unexpected import in event_aggregator.py: {name}",
            )

    def test_event_aggregate_only_imports_expected_modules(self):
        import aggregator.event_aggregate as event_aggregate_module

        imported = self._imported_names(event_aggregate_module.__file__)
        allowed_prefixes = (
            "dataclasses",
            "dataclass",
            "datetime",
            "typing",
            "Optional",
            "dashboard.dashboard_result",
            "dashboard_result",
            "DashboardResult",
            "trust.trust_result",
            "trust_result",
            "TrustResult",
            "verification.verification_result",
            "verification_result",
            "VerificationResult",
        )
        for name in imported:
            self.assertTrue(
                any(name == prefix or name.startswith(prefix + ".") for prefix in allowed_prefixes),
                f"unexpected import in event_aggregate.py: {name}",
            )


if __name__ == "__main__":
    unittest.main()
