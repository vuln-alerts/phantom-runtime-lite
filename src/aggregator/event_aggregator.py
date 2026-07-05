"""
aggregator/event_aggregator.py
=================================
EventAggregator — H4-5 Event Aggregator.

Per the H4-5 scope, Event Aggregator is a read-only, stateless downstream
orchestration layer. Its only inputs are a VerificationResult (produced by
the Verification Runtime), a TrustResult (produced by the Trust Runtime),
and a DashboardResult (produced by the Dashboard Runtime); it never
inspects the originating RuntimeEvent, never imports VerificationRuntime,
TrustRuntime, or DashboardRuntime, never invokes a Provider or Whisper, and
never runs Cloud Run Runtime logic or accesses Runtime internal state.

Event Aggregator performs no business logic of its own: no verification,
no Trust Policy, no dashboard rendering, no workflow. It simply holds a
reference to each of its three inputs, alongside identity metadata, inside
a new EventAggregate — it never copies, flattens, or renames a field out
of any of them.

EventAggregator is completely stateless: aggregate() is a pure function of
its three arguments. No instance state, session memory, caches, or globals
are used.

EXPORTED API:
  EventAggregator — aggregate(verification_result, trust_result, dashboard_result) -> EventAggregate
"""

from datetime import datetime, timezone

from aggregator.event_aggregate import EventAggregate
from dashboard.dashboard_result import DashboardResult
from trust.trust_result import TrustResult
from verification.verification_result import VerificationResult

_SCHEMA_VERSION = "1.0"


class EventAggregator:
    """Read-only, stateless downstream orchestration layer. See module
    docstring.

    Holds no instance state: every call to aggregate() is independent and
    depends only on the VerificationResult, TrustResult, and
    DashboardResult passed to it.
    """

    def aggregate(
        self,
        verification_result: VerificationResult,
        trust_result: TrustResult,
        dashboard_result: DashboardResult,
    ) -> EventAggregate:
        """Combine one (VerificationResult, TrustResult, DashboardResult)
        triple into a new EventAggregate.

        All three arguments are read-only: none is mutated, and no field
        of any of them is copied verbatim — EventAggregate holds direct
        references to the objects themselves. Identity (source_event_id,
        session_id) is propagated from `dashboard_result`, the latest link
        in the Verification -> Trust -> Dashboard -> Aggregator chain.
        """
        return EventAggregate(
            schema_version=_SCHEMA_VERSION,
            source_event_id=dashboard_result.source_event_id,
            session_id=dashboard_result.session_id,
            timestamp=datetime.now(timezone.utc),
            verification_result=verification_result,
            trust_result=trust_result,
            dashboard_result=dashboard_result,
        )
