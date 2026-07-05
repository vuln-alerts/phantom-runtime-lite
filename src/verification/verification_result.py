"""
verification/verification_result.py
=====================================
VerificationResult — the immutable output of the H4-2 Verification Runtime.

Per the approved H4-2 design review, VerificationResult is a new artifact
produced by the Verification Runtime, not a copy of the RuntimeEvent it was
derived from. It carries only a reference to its source event
(source_event_id, session_id) plus the Verification Runtime's own findings.
No field here is a verbatim copy of a RuntimeEvent field (in particular,
there is no `event_type` — the source event's `type` is not duplicated).

EXPORTED API:
  VerificationResult — immutable verification outcome for one RuntimeEvent
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class VerificationResult:
    schema_version:    str
    source_event_id:   Optional[str]
    session_id:        Optional[str]
    timestamp:         datetime
    gap_detected:      bool
    gap_reason:        Optional[str]
    fallback_detected: bool
    fallback_reason:   Optional[str]
    reliable:          bool
    reliability_score: float
    warnings:          List[str]
    explanation:       str
