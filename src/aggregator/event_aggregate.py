"""
aggregator/event_aggregate.py
================================
EventAggregate — the immutable output of the H4-5 Event Aggregator.

Per the H4-5 scope, EventAggregate is a downstream combination artifact: it
does not copy, flatten, or rename any field out of the VerificationResult,
TrustResult, or DashboardResult it aggregates. It carries only a reference
to its source (source_event_id, session_id) plus a direct reference to
each of the three already-computed downstream artifacts. Consumers that
need a specific field (e.g. trust_score) read it off the referenced result
object itself, not off EventAggregate.

EXPORTED API:
  EventAggregate — immutable aggregation of one VerificationResult,
                   TrustResult, and DashboardResult triple
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dashboard.dashboard_result import DashboardResult
from trust.trust_result import TrustResult
from verification.verification_result import VerificationResult


@dataclass(frozen=True)
class EventAggregate:
    schema_version:      str
    source_event_id:     Optional[str]
    session_id:          Optional[str]
    timestamp:           datetime

    verification_result: VerificationResult
    trust_result:        TrustResult
    dashboard_result:    DashboardResult
