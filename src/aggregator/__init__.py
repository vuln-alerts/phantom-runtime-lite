"""
aggregator package
====================
H4-5 Event Aggregator — a downstream orchestration layer that combines the
already-computed VerificationResult, TrustResult, and DashboardResult for a
single RuntimeEvent into one immutable EventAggregate. See
event_aggregator.py and event_aggregate.py for details.

EXPORTED API:
  EventAggregator — aggregate(verification_result, trust_result, dashboard_result) -> EventAggregate
  EventAggregate  — immutable, reference-only aggregation of the three inputs
"""

from aggregator.event_aggregate import EventAggregate
from aggregator.event_aggregator import EventAggregator

__all__ = ["EventAggregator", "EventAggregate"]
