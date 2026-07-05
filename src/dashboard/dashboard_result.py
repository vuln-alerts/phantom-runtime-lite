"""
dashboard/dashboard_result.py
===============================
DashboardResult — the immutable output of the H4-4 Dashboard layer.

Per the H4-4 scope, DashboardResult is a new, presentation-oriented
artifact produced by rendering a VerificationResult and a TrustResult. It
is not a copy of, alias for, or subclass of either input — it carries only
a reference to its source (source_event_id, session_id) plus a flat set of
display-oriented fields chosen from what Verification and Trust already
computed. Dashboard performs no scoring, policy, or workflow logic of its
own; every value below is read directly off the upstream results.

EXPORTED API:
  DashboardResult — immutable, display-oriented view of one Verification
                    + Trust outcome pair
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class DashboardResult:
    schema_version:         str
    source_event_id:        Optional[str]
    session_id:              Optional[str]
    timestamp:                datetime

    # -- Verification display fields --
    gap_detected:             bool
    gap_reason:               Optional[str]
    fallback_detected:        bool
    reliability_score:        float
    reliable:                 bool
    warnings:                 List[str]

    # -- Trust display fields --
    trust_score:              float
    trust_level:              str
    human_review_required:    bool
    review_reason:            Optional[str]
    contributing_factors:     List[str]
