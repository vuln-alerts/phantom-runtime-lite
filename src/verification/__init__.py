"""
verification package
=====================
H4-2 Verification Runtime — read-only downstream Runtime consuming Typed
Events (RuntimeEvent) and producing VerificationResult. See
verification_runtime.py and verification_result.py for details.

EXPORTED API:
  VerificationRuntime — handle(event) -> VerificationResult
  VerificationResult  — immutable verification outcome
"""

from verification.verification_result import VerificationResult
from verification.verification_runtime import VerificationRuntime

__all__ = ["VerificationRuntime", "VerificationResult"]
