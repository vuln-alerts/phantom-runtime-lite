"""
trust/trust_runtime.py
=========================
TrustRuntime — H4-3 Trust Runtime.

Per the approved H4-3 design, Trust Runtime is a read-only, independent
downstream runtime. Its only input is VerificationResult (produced by the
Verification Runtime); it never inspects the originating RuntimeEvent,
never imports VerificationRuntime, never invokes a Provider or Whisper,
and never runs any Runtime logic.

Trust Runtime is deliberately NOT a passthrough of
VerificationResult.reliability_score. It evaluates the entire
VerificationResult (reliable, reliability_score, gap_detected, gap_reason,
fallback_detected, fallback_reason, warnings) through an explicit Trust
Policy — a weighted, documented ruleset owned by this module — to produce
trust_score, trust_level, and human_review_required. reliability_score
contributes only part of the weighting; gap/fallback/warning signals are
penalized independently on top of it, so two VerificationResults sharing
the same reliability_score can still yield different trust_score values.

human_review_required is a policy RECOMMENDATION only. Trust Runtime does
not perform review, track review state, create tickets, or maintain any
workflow — that responsibility belongs entirely to downstream consumers
(Dashboard, FastAPI, or a future review-workflow service).

Trust Level values (TRUSTED / CAUTION / UNTRUSTED) are illustrative
outputs of the current Trust Policy, not a closed enumeration. Consumers
must not enumerate these values and must handle unknown values
gracefully, per the same forward-compatibility principle the Runtime
Event Contract applies to `provider`.

TrustRuntime is completely stateless: handle() is a pure function of its
single VerificationResult argument. No instance state, session memory,
caches, or globals are used.

EXPORTED API:
  TrustRuntime — handle(result) -> TrustResult
"""

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from trust.trust_result import TrustResult
from verification.verification_result import VerificationResult

_SCHEMA_VERSION = "1.0"

# Trust Policy weighting. Owned exclusively by Trust Runtime — independent
# of, and never imported from, Verification Runtime's own constants.
_RELIABILITY_WEIGHT = 0.5
_BASELINE_WEIGHT = 1.0 - _RELIABILITY_WEIGHT
_GAP_PENALTY = 0.2
_FALLBACK_PENALTY = 0.2
_WARNING_PENALTY_PER_ITEM = 0.05
_MAX_WARNING_PENALTY = 0.15

# Trust Level thresholds. Documented examples only — see module docstring
# on forward compatibility. Consumers must not enumerate these values.
_TRUST_LEVEL_TRUSTED = "TRUSTED"
_TRUST_LEVEL_CAUTION = "CAUTION"
_TRUST_LEVEL_UNTRUSTED = "UNTRUSTED"

_TRUSTED_THRESHOLD = 0.75
_CAUTION_THRESHOLD = 0.4


class TrustRuntime:
    """Read-only, stateless downstream Runtime. See module docstring.

    Holds no instance state: every call to handle() is independent and
    depends only on the VerificationResult passed to it.
    """

    def handle(self, result: VerificationResult) -> TrustResult:
        """Evaluate one VerificationResult through the Trust Policy and
        return a new TrustResult.

        `result` is read-only: it is never mutated, and no field of it is
        copied verbatim into the returned TrustResult beyond a reference
        to its identity (source_event_id, session_id).
        """
        trust_score, contributing_factors = self._apply_trust_policy(result)
        trust_level = self._classify_trust_level(trust_score)
        human_review_required, review_reason = self._recommend_review(
            result, trust_score, trust_level,
        )
        explanation = self._build_explanation(trust_level, trust_score, contributing_factors)

        return TrustResult(
            schema_version=_SCHEMA_VERSION,
            source_event_id=result.source_event_id,
            session_id=result.session_id,
            timestamp=datetime.now(timezone.utc),
            trust_score=trust_score,
            trust_level=trust_level,
            human_review_required=human_review_required,
            review_reason=review_reason,
            contributing_factors=contributing_factors,
            explanation=explanation,
        )

    # -- Trust Policy -------------------------------------------------------

    @staticmethod
    def _apply_trust_policy(result: VerificationResult) -> Tuple[float, List[str]]:
        """The Trust Policy: a weighted evaluation of the full
        VerificationResult, not a passthrough of any single field.

        reliability_score contributes only _RELIABILITY_WEIGHT of the
        score; gap/fallback/warning signals are penalized independently on
        top of the remaining baseline weight, so two VerificationResults
        sharing the same reliability_score can still diverge here.
        """
        factors: List[str] = []

        score = (result.reliability_score * _RELIABILITY_WEIGHT) + _BASELINE_WEIGHT

        if result.gap_detected:
            score -= _GAP_PENALTY
            factors.append("gap_detected (" + str(result.gap_reason) + ")")
        if result.fallback_detected:
            score -= _FALLBACK_PENALTY
            factors.append("fallback_detected (" + str(result.fallback_reason) + ")")
        if not result.reliable:
            factors.append("verification marked this event as unreliable")
        if result.warnings:
            penalty = min(
                _WARNING_PENALTY_PER_ITEM * len(result.warnings),
                _MAX_WARNING_PENALTY,
            )
            score -= penalty
            factors.append(str(len(result.warnings)) + " verification warning(s)")

        score = max(0.0, min(1.0, score))

        if not factors:
            factors.append("no gap, fallback, or warnings reported by verification")

        return score, factors

    @staticmethod
    def _classify_trust_level(trust_score: float) -> str:
        if trust_score >= _TRUSTED_THRESHOLD:
            return _TRUST_LEVEL_TRUSTED
        if trust_score >= _CAUTION_THRESHOLD:
            return _TRUST_LEVEL_CAUTION
        return _TRUST_LEVEL_UNTRUSTED

    # -- Human Review (policy recommendation only) ---------------------------

    @staticmethod
    def _recommend_review(
        result: VerificationResult, trust_score: float, trust_level: str,
    ) -> Tuple[bool, Optional[str]]:
        """Produce a policy RECOMMENDATION only. Trust Runtime does not
        perform, track, or queue any review — see module docstring.
        """
        if trust_level == _TRUST_LEVEL_UNTRUSTED:
            reason = "trust_level=" + repr(trust_level) + " (trust_score=" + format(trust_score, ".2f") + ")"
            return True, reason
        if result.gap_detected and result.fallback_detected:
            return True, "verification reported both a gap and a fallback for this event"
        return False, None

    # -- Explanation Generation -----------------------------------------------

    @staticmethod
    def _build_explanation(
        trust_level: str, trust_score: float, contributing_factors: List[str],
    ) -> str:
        factors_text = "; ".join(contributing_factors)
        header = "Trust Policy classified this event as " + repr(trust_level) + " "
        body = "(trust_score=" + format(trust_score, ".2f") + "): " + factors_text + "."
        return header + body
