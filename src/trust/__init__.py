"""
trust package
==============
H4-3 Trust Runtime — read-only, stateless downstream runtime consuming
VerificationResult and producing TrustResult via an explicit Trust
Policy. See trust_runtime.py and trust_result.py for details.

EXPORTED API:
  TrustRuntime — handle(result) -> TrustResult
  TrustResult  — immutable trust outcome
"""

from trust.trust_result import TrustResult
from trust.trust_runtime import TrustRuntime

__all__ = ["TrustRuntime", "TrustResult"]
