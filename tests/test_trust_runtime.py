"""
tests/test_trust_runtime.py
=============================
Unit tests for the H4-3 Trust Runtime (trust.trust_runtime.TrustRuntime).

Uses unittest (stdlib), consistent with tests/test_verification_runtime.py:
pytest is not a dependency of this project.
"""

import datetime
import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from trust.trust_runtime import TrustRuntime
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
        explanation="test explanation",
    )


class TrustPolicyTests(unittest.TestCase):
    def test_high_reliability_yields_high_trust(self):
        rt = TrustRuntime()
        result = rt.handle(_verification_result(reliable=True, reliability_score=1.0))
        self.assertGreaterEqual(result.trust_score, 0.75)
        self.assertEqual(result.trust_level, "TRUSTED")

    def test_gap_detected_lowers_trust(self):
        rt = TrustRuntime()
        baseline = rt.handle(_verification_result(reliability_score=1.0))
        with_gap = rt.handle(_verification_result(
            reliability_score=1.0, gap_detected=True, gap_reason="missing field",
        ))
        self.assertLess(with_gap.trust_score, baseline.trust_score)

    def test_fallback_detected_lowers_trust(self):
        rt = TrustRuntime()
        baseline = rt.handle(_verification_result(reliability_score=1.0))
        with_fallback = rt.handle(_verification_result(
            reliability_score=1.0,
            fallback_detected=True,
            fallback_reason="finish_reason='fallback'",
        ))
        self.assertLess(with_fallback.trust_score, baseline.trust_score)

    def test_gap_and_fallback_combined_lowers_trust_further(self):
        rt = TrustRuntime()
        gap_only = rt.handle(_verification_result(
            reliability_score=1.0, gap_detected=True, gap_reason="x",
        ))
        both = rt.handle(_verification_result(
            reliability_score=1.0,
            gap_detected=True, gap_reason="x",
            fallback_detected=True, fallback_reason="y",
        ))
        self.assertLess(both.trust_score, gap_only.trust_score)
        self.assertTrue(both.human_review_required)

    def test_same_reliability_score_can_produce_different_trust_scores(self):
        # Confirms trust_score is a Trust Policy evaluation of the whole
        # VerificationResult, not a passthrough/rename of reliability_score:
        # both inputs share reliability_score=1.0 yet trust_score differs.
        rt = TrustRuntime()
        without_gap = rt.handle(_verification_result(reliability_score=1.0, gap_detected=False))
        with_gap = rt.handle(_verification_result(
            reliability_score=1.0, gap_detected=True, gap_reason="x",
        ))
        self.assertNotEqual(without_gap.trust_score, with_gap.trust_score)


class TrustScoreBoundsTests(unittest.TestCase):
    def test_trust_score_always_within_bounds(self):
        rt = TrustRuntime()
        cases = [
            _verification_result(
                reliability_score=0.0, gap_detected=True, fallback_detected=True,
                gap_reason="x", fallback_reason="y",
                warnings=["a", "b", "c", "d"],
            ),
            _verification_result(reliability_score=1.0),
            _verification_result(reliability_score=0.5, warnings=["w"]),
        ]
        for vr in cases:
            result = rt.handle(vr)
            self.assertGreaterEqual(result.trust_score, 0.0)
            self.assertLessEqual(result.trust_score, 1.0)


class TrustLevelTests(unittest.TestCase):
    def test_valid_trust_level_values(self):
        rt = TrustRuntime()
        result = rt.handle(_verification_result())
        self.assertIn(result.trust_level, {"TRUSTED", "CAUTION", "UNTRUSTED"})

    def test_low_trust_produces_untrusted_level(self):
        rt = TrustRuntime()
        result = rt.handle(_verification_result(
            reliability_score=0.0, reliable=False,
            gap_detected=True, gap_reason="x",
            fallback_detected=True, fallback_reason="y",
            warnings=["a", "b"],
        ))
        self.assertEqual(result.trust_level, "UNTRUSTED")

    def test_trust_level_is_a_plain_string_for_forward_compatibility(self):
        # trust_level must remain an open string, not a closed enum, so
        # future Trust Policy revisions can introduce new values without
        # breaking the type contract. Consumers must not enumerate values.
        rt = TrustRuntime()
        result = rt.handle(_verification_result())
        self.assertIsInstance(result.trust_level, str)


class HumanReviewTests(unittest.TestCase):
    def test_human_review_is_recommendation_only(self):
        rt = TrustRuntime()
        result = rt.handle(_verification_result(
            reliability_score=0.0, reliable=False,
            gap_detected=True, gap_reason="x",
            fallback_detected=True, fallback_reason="y",
        ))
        self.assertTrue(result.human_review_required)
        self.assertIsNotNone(result.review_reason)
        # TrustResult carries no review-tracking fields such as a ticket
        # id, queue position, or workflow state — recommendation only.
        result_fields = result.__dataclass_fields__
        self.assertNotIn("ticket_id", result_fields)
        self.assertNotIn("review_status", result_fields)
        self.assertNotIn("queue_position", result_fields)

    def test_human_review_recommendation_is_deterministic(self):
        rt = TrustRuntime()
        vr = _verification_result(reliability_score=0.2, gap_detected=True, gap_reason="x")
        first = rt.handle(vr)
        second = rt.handle(vr)
        self.assertEqual(first.human_review_required, second.human_review_required)
        self.assertEqual(first.trust_score, second.trust_score)
        self.assertEqual(first.trust_level, second.trust_level)

    def test_high_trust_does_not_require_review(self):
        rt = TrustRuntime()
        result = rt.handle(_verification_result(reliability_score=1.0))
        self.assertFalse(result.human_review_required)
        self.assertIsNone(result.review_reason)


class ExplanationTests(unittest.TestCase):
    def test_explanation_always_non_empty(self):
        rt = TrustRuntime()
        cases = [
            _verification_result(),
            _verification_result(gap_detected=True, gap_reason="x"),
            _verification_result(fallback_detected=True, fallback_reason="y"),
            _verification_result(warnings=["a warning"]),
        ]
        for vr in cases:
            result = rt.handle(vr)
            self.assertTrue(result.explanation)
            self.assertIsInstance(result.explanation, str)


class ImmutabilityTests(unittest.TestCase):
    def test_trust_result_is_frozen(self):
        rt = TrustRuntime()
        result = rt.handle(_verification_result())
        with self.assertRaises(Exception):
            result.trust_score = 0.0  # type: ignore[misc]

    def test_trust_result_is_a_frozen_dataclass(self):
        rt = TrustRuntime()
        result = rt.handle(_verification_result())
        self.assertTrue(result.__dataclass_params__.frozen)


class StatelessnessTests(unittest.TestCase):
    def test_multiple_handle_calls_are_independent(self):
        rt = TrustRuntime()
        low = rt.handle(_verification_result(
            reliability_score=0.0,
            gap_detected=True, gap_reason="x",
            fallback_detected=True, fallback_reason="y",
        ))
        high = rt.handle(_verification_result(reliability_score=1.0))
        low_again = rt.handle(_verification_result(
            reliability_score=0.0,
            gap_detected=True, gap_reason="x",
            fallback_detected=True, fallback_reason="y",
        ))
        self.assertEqual(low.trust_score, low_again.trust_score)
        self.assertNotEqual(low.trust_score, high.trust_score)

    def test_trust_runtime_holds_no_instance_state(self):
        rt = TrustRuntime()
        self.assertEqual(vars(rt), {})


class DependencyTests(unittest.TestCase):
    def test_trust_runtime_module_does_not_import_verification_runtime(self):
        # Inspect actual import statements (via ast), not raw file text —
        # the module docstring legitimately mentions "VerificationRuntime"
        # in prose to document that it must never be imported.
        import ast

        import trust.trust_runtime as trust_runtime_module

        with open(trust_runtime_module.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.add(node.module)
                imported_names.update(alias.name for alias in node.names)

        self.assertNotIn("verification_runtime", imported_names)
        self.assertNotIn("VerificationRuntime", imported_names)
        self.assertFalse(
            any("verification_runtime" in name for name in imported_names),
            f"unexpected import of verification_runtime: {imported_names}",
        )
        self.assertFalse(hasattr(trust_runtime_module, "VerificationRuntime"))


if __name__ == "__main__":
    unittest.main()
