"""
tests/test_dashboard_runtime.py
==================================
Unit tests for the H4-4 Dashboard (dashboard.dashboard_runtime.DashboardRuntime).

Uses unittest (stdlib), consistent with tests/test_verification_runtime.py
and tests/test_trust_runtime.py: pytest is not a dependency of this
project.
"""

import ast
import datetime
import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from dashboard.dashboard_result import DashboardResult
from dashboard.dashboard_runtime import DashboardRuntime
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


class DashboardResultCreationTests(unittest.TestCase):
    def test_render_produces_dashboard_result(self):
        db = DashboardRuntime()
        result = db.render(_verification_result(), _trust_result())
        self.assertIsInstance(result, DashboardResult)

    def test_render_does_not_return_verification_or_trust_result(self):
        db = DashboardRuntime()
        result = db.render(_verification_result(), _trust_result())
        self.assertNotIsInstance(result, VerificationResult)
        self.assertNotIsInstance(result, TrustResult)

    def test_dashboard_result_is_not_a_subclass_of_inputs(self):
        self.assertFalse(issubclass(DashboardResult, VerificationResult))
        self.assertFalse(issubclass(DashboardResult, TrustResult))


class VisualizationFieldTests(unittest.TestCase):
    def test_verification_fields_are_propagated(self):
        db = DashboardRuntime()
        vr = _verification_result(
            gap_detected=True, gap_reason="missing field",
            fallback_detected=True,
            reliability_score=0.4, reliable=False,
            warnings=["w1", "w2"],
        )
        result = db.render(vr, _trust_result())
        self.assertTrue(result.gap_detected)
        self.assertEqual(result.gap_reason, "missing field")
        self.assertTrue(result.fallback_detected)
        self.assertEqual(result.reliability_score, 0.4)
        self.assertFalse(result.reliable)
        self.assertEqual(result.warnings, ["w1", "w2"])

    def test_trust_fields_are_propagated(self):
        db = DashboardRuntime()
        tr = _trust_result(
            trust_score=0.2,
            trust_level="UNTRUSTED",
            human_review_required=True,
            review_reason="trust_level='UNTRUSTED'",
            contributing_factors=["gap_detected (x)", "1 verification warning(s)"],
        )
        result = db.render(_verification_result(), tr)
        self.assertEqual(result.trust_score, 0.2)
        self.assertEqual(result.trust_level, "UNTRUSTED")
        self.assertTrue(result.human_review_required)
        self.assertEqual(result.review_reason, "trust_level='UNTRUSTED'")
        self.assertEqual(
            result.contributing_factors,
            ["gap_detected (x)", "1 verification warning(s)"],
        )

    def test_no_gap_or_fallback_case(self):
        db = DashboardRuntime()
        result = db.render(_verification_result(), _trust_result())
        self.assertFalse(result.gap_detected)
        self.assertIsNone(result.gap_reason)
        self.assertFalse(result.fallback_detected)
        self.assertTrue(result.reliable)
        self.assertEqual(result.warnings, [])

    def test_no_human_review_case(self):
        db = DashboardRuntime()
        result = db.render(_verification_result(), _trust_result())
        self.assertFalse(result.human_review_required)
        self.assertIsNone(result.review_reason)


class IdentityPropagationTests(unittest.TestCase):
    def test_source_event_id_and_session_id_propagated(self):
        db = DashboardRuntime()
        vr = _verification_result(source_event_id="evt-42", session_id="sess-42")
        tr = _trust_result(source_event_id="evt-42", session_id="sess-42")
        result = db.render(vr, tr)
        self.assertEqual(result.source_event_id, "evt-42")
        self.assertEqual(result.session_id, "sess-42")

    def test_identity_matches_verification_result_when_ids_absent(self):
        db = DashboardRuntime()
        vr = _verification_result(source_event_id=None, session_id=None)
        tr = _trust_result(source_event_id=None, session_id=None)
        result = db.render(vr, tr)
        self.assertIsNone(result.source_event_id)
        self.assertIsNone(result.session_id)

    def test_schema_version_is_present(self):
        db = DashboardRuntime()
        result = db.render(_verification_result(), _trust_result())
        self.assertEqual(result.schema_version, "1.0")

    def test_timestamp_is_freshly_generated(self):
        db = DashboardRuntime()
        before = datetime.datetime.now(datetime.timezone.utc)
        result = db.render(_verification_result(), _trust_result())
        after = datetime.datetime.now(datetime.timezone.utc)
        self.assertLessEqual(before, result.timestamp)
        self.assertLessEqual(result.timestamp, after)


class ReadOnlyBehaviorTests(unittest.TestCase):
    def test_dashboard_result_is_frozen(self):
        db = DashboardRuntime()
        result = db.render(_verification_result(), _trust_result())
        with self.assertRaises(Exception):
            result.trust_score = 0.0  # type: ignore[misc]

    def test_dashboard_result_is_a_frozen_dataclass(self):
        db = DashboardRuntime()
        result = db.render(_verification_result(), _trust_result())
        self.assertTrue(result.__dataclass_params__.frozen)

    def test_render_does_not_mutate_verification_result(self):
        db = DashboardRuntime()
        vr = _verification_result(warnings=["w1"])
        before = dict(vr.__dict__)
        db.render(vr, _trust_result())
        self.assertEqual(vr.__dict__, before)

    def test_render_does_not_mutate_trust_result(self):
        db = DashboardRuntime()
        tr = _trust_result(contributing_factors=["f1"])
        before = dict(tr.__dict__)
        db.render(_verification_result(), tr)
        self.assertEqual(tr.__dict__, before)

    def test_render_mutating_dashboard_warnings_does_not_affect_source(self):
        # DashboardResult.warnings must be its own list, not an alias of
        # VerificationResult.warnings.
        db = DashboardRuntime()
        vr = _verification_result(warnings=["w1"])
        result = db.render(vr, _trust_result())
        result.warnings.append("mutated")
        self.assertEqual(vr.warnings, ["w1"])


class StatelessnessTests(unittest.TestCase):
    def test_dashboard_runtime_holds_no_instance_state(self):
        db = DashboardRuntime()
        self.assertEqual(vars(db), {})

    def test_multiple_render_calls_are_independent(self):
        db = DashboardRuntime()
        low = db.render(
            _verification_result(reliability_score=0.0, gap_detected=True, gap_reason="x"),
            _trust_result(trust_score=0.1, trust_level="UNTRUSTED"),
        )
        high = db.render(_verification_result(), _trust_result())
        low_again = db.render(
            _verification_result(reliability_score=0.0, gap_detected=True, gap_reason="x"),
            _trust_result(trust_score=0.1, trust_level="UNTRUSTED"),
        )
        self.assertEqual(low.trust_score, low_again.trust_score)
        self.assertNotEqual(low.trust_score, high.trust_score)

    def test_render_is_deterministic_apart_from_timestamp(self):
        db = DashboardRuntime()
        vr = _verification_result(gap_detected=True, gap_reason="x")
        tr = _trust_result(trust_score=0.5, trust_level="CAUTION")
        first = db.render(vr, tr)
        second = db.render(vr, tr)
        self.assertEqual(first.gap_detected, second.gap_detected)
        self.assertEqual(first.trust_score, second.trust_score)
        self.assertEqual(first.trust_level, second.trust_level)


class DashboardResultShapeTests(unittest.TestCase):
    def test_dashboard_result_does_not_reuse_verification_or_trust_fields_directly(self):
        # DashboardResult must be its own dataclass, not a re-export or
        # alias of VerificationResult/TrustResult.
        self.assertIsNot(DashboardResult, VerificationResult)
        self.assertIsNot(DashboardResult, TrustResult)

    def test_dashboard_result_has_no_workflow_or_persistence_fields(self):
        db = DashboardRuntime()
        result = db.render(_verification_result(), _trust_result())
        result_fields = result.__dataclass_fields__
        for forbidden in ("ticket_id", "review_status", "queue_position", "persisted_at"):
            self.assertNotIn(forbidden, result_fields)


class DependencyTests(unittest.TestCase):
    """AST-based dependency inspection: dashboard_runtime.py must import
    only its own display data and the two upstream result data contracts
    (VerificationResult, TrustResult) — never the runtimes that produce
    them, and never anything from the Cloud Run Runtime, Provider, or
    Whisper layers.
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

    def test_dashboard_runtime_does_not_import_verification_runtime(self):
        import dashboard.dashboard_runtime as dashboard_runtime_module

        imported = self._imported_names(dashboard_runtime_module.__file__)
        self.assertNotIn("verification_runtime", imported)
        self.assertNotIn("VerificationRuntime", imported)
        self.assertFalse(any("verification_runtime" in name for name in imported))
        self.assertFalse(hasattr(dashboard_runtime_module, "VerificationRuntime"))

    def test_dashboard_runtime_does_not_import_trust_runtime(self):
        import dashboard.dashboard_runtime as dashboard_runtime_module

        imported = self._imported_names(dashboard_runtime_module.__file__)
        self.assertNotIn("trust_runtime", imported)
        self.assertNotIn("TrustRuntime", imported)
        self.assertFalse(any("trust_runtime" in name for name in imported))
        self.assertFalse(hasattr(dashboard_runtime_module, "TrustRuntime"))

    def test_dashboard_runtime_does_not_import_cloud_run_runtime(self):
        import dashboard.dashboard_runtime as dashboard_runtime_module

        imported = self._imported_names(dashboard_runtime_module.__file__)
        for forbidden in ("phantom_runtime", "cloud_run_shell", "runtime.cloud_run_shell"):
            self.assertNotIn(forbidden, imported)
            self.assertFalse(any(forbidden in name for name in imported))

    def test_dashboard_runtime_does_not_import_provider(self):
        import dashboard.dashboard_runtime as dashboard_runtime_module

        imported = self._imported_names(dashboard_runtime_module.__file__)
        self.assertFalse(any("provider" in name.lower() for name in imported))

    def test_dashboard_runtime_does_not_import_whisper(self):
        import dashboard.dashboard_runtime as dashboard_runtime_module

        imported = self._imported_names(dashboard_runtime_module.__file__)
        self.assertFalse(any("whisper" in name.lower() for name in imported))

    def test_dashboard_result_module_has_no_forbidden_dependencies(self):
        import dashboard.dashboard_result as dashboard_result_module

        imported = self._imported_names(dashboard_result_module.__file__)
        forbidden_substrings = (
            "phantom_runtime", "cloud_run_shell", "provider", "whisper",
            "verification_runtime", "trust_runtime",
        )
        for name in imported:
            lowered = name.lower()
            for forbidden in forbidden_substrings:
                self.assertNotIn(forbidden, lowered)

    def test_dashboard_runtime_only_imports_expected_modules(self):
        import dashboard.dashboard_runtime as dashboard_runtime_module

        imported = self._imported_names(dashboard_runtime_module.__file__)
        allowed_prefixes = (
            "datetime",
            "dashboard.dashboard_result",
            "dashboard_result",
            "trust.trust_result",
            "trust_result",
            "verification.verification_result",
            "verification_result",
            "DashboardResult",
            "TrustResult",
            "VerificationResult",
            "datetime",
            "timezone",
        )
        for name in imported:
            self.assertTrue(
                any(name == prefix or name.startswith(prefix + ".") for prefix in allowed_prefixes),
                f"unexpected import in dashboard_runtime.py: {name}",
            )


if __name__ == "__main__":
    unittest.main()
