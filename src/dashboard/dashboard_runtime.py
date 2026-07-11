"""
dashboard/dashboard_runtime.py
=================================
DashboardRuntime — H4-4 Dashboard Integration.

Per the H4-4 scope, Dashboard is a read-only, stateless, presentation-only
downstream layer. Its only inputs are a VerificationResult (produced by
the Verification Runtime) and a TrustResult (produced by the Trust
Runtime); it never inspects the originating RuntimeEvent, never imports
VerificationRuntime or TrustRuntime, never invokes a Provider or Whisper,
and never runs Cloud Run Runtime logic or accesses Runtime internal state.

Dashboard performs no business logic: no scoring, no Trust Policy, no
Verification logic, no workflow, and no review-state tracking. It simply
reads already-computed fields off its two inputs and republishes them as a
new, dedicated DashboardResult intended for visualization.

DashboardRuntime is completely stateless: render() is a pure function of
its two arguments. No instance state, session memory, caches, or globals
are used.

EXPORTED API:
  DashboardRuntime — render(verification_result, trust_result,
                             conversation_line=None, speaker=None, transcript=None)
                     -> DashboardResult
"""

from datetime import datetime, timezone

from dashboard.dashboard_result import DashboardResult
from trust.trust_result import TrustResult
from verification.verification_result import VerificationResult

_SCHEMA_VERSION = "1.0"


class DashboardRuntime:
    """Read-only, stateless downstream layer. See module docstring.

    Holds no instance state: every call to render() is independent and
    depends only on the VerificationResult and TrustResult passed to it.
    """

    def render(
        self,
        verification_result: VerificationResult,
        trust_result: TrustResult,
        conversation_line=None,
        speaker=None,
        transcript=None,
    ) -> DashboardResult:
        """Render one (VerificationResult, TrustResult) pair into a new
        DashboardResult.

        Both arguments are read-only: neither is mutated, and no field of
        either is written back. Identity (source_event_id, session_id) is
        propagated from `verification_result`, which is the earlier link
        in the Verification -> Trust -> Dashboard chain.

        conversation_line/speaker/transcript are Conversation Traceability
        metadata (docs/H4_RUNTIME_EVENT_CONTRACT.md, "Runtime Event
        Metadata") read off the source Runtime Event by the caller. They
        are copied verbatim onto DashboardResult -- Dashboard computes,
        infers, or validates nothing about them.
        """
        return DashboardResult(
            schema_version=_SCHEMA_VERSION,
            source_event_id=verification_result.source_event_id,
            session_id=verification_result.session_id,
            timestamp=datetime.now(timezone.utc),
            gap_detected=verification_result.gap_detected,
            gap_reason=verification_result.gap_reason,
            fallback_detected=verification_result.fallback_detected,
            reliability_score=verification_result.reliability_score,
            reliable=verification_result.reliable,
            warnings=list(verification_result.warnings),
            trust_score=trust_result.trust_score,
            trust_level=trust_result.trust_level,
            human_review_required=trust_result.human_review_required,
            review_reason=trust_result.review_reason,
            contributing_factors=list(trust_result.contributing_factors),
            conversation_line=conversation_line,
            speaker=speaker,
            transcript=transcript,
        )
