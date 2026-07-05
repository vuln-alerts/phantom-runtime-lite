"""
trust/trust_result.py
=======================
TrustResult — the immutable output of the H4-3 Trust Runtime.

Per the approved H4-3 design review, TrustResult is a new artifact
produced by evaluating a VerificationResult through the Trust Policy (see
trust_runtime.py), not a copy of the VerificationResult it was derived
from. It carries only a reference to its source (source_event_id,
session_id) plus the Trust Policy's own findings. No field here is a
verbatim copy of a VerificationResult field — in particular, there is no
`reliability_score` or `reliable` field: trust_score and trust_level are
the Trust Policy's own derived judgments, not renamed copies of
Verification's output.

EXPORTED API:
  TrustResult — immutable trust outcome for one VerificationResult
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class TrustResult:
    schema_version:        str
    source_event_id:       Optional[str]
    session_id:            Optional[str]
    timestamp:             datetime
    trust_score:           float
    trust_level:           str
    human_review_required: bool
    review_reason:         Optional[str]
    contributing_factors:  List[str]
    explanation:           str
