"""
dashboard package
==================
H4-4 Dashboard Integration — read-only, stateless downstream layer
consuming VerificationResult and TrustResult and producing DashboardResult
for visualization. See dashboard_runtime.py and dashboard_result.py for
details.

EXPORTED API:
  DashboardRuntime — render(verification_result, trust_result) -> DashboardResult
  DashboardResult  — immutable, display-oriented outcome
"""

from dashboard.dashboard_result import DashboardResult
from dashboard.dashboard_runtime import DashboardRuntime

__all__ = ["DashboardRuntime", "DashboardResult"]
