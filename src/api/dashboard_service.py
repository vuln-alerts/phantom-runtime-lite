"""
api/dashboard_service.py
===========================
DashboardService — the only application-layer module api_server.py imports
to reach Dashboard state.

Its responsibility is limited to holding and returning the single most
recent DashboardResult (no history, no persistence, no session keying).
It never runs the Verification -> Trust -> Dashboard -> Aggregator chain
itself and never imports VerificationRuntime, TrustRuntime, DashboardRuntime,
EventAggregator, or runtime.pipeline_orchestrator -- pipeline execution is
runtime.pipeline_orchestrator.RuntimePipelineOrchestrator's responsibility,
not this module's. api_server.py's POST /aggregate handler is what supplies
the DashboardResult here, as a side effect of receiving an EventAggregate
that was already produced by the Pipeline.

EXPORTED API:
  DashboardService()
    .set_latest(dashboard_result: DashboardResult) -> None
    .get_latest() -> Optional[DashboardResult]
"""

from typing import Optional

from dashboard.dashboard_result import DashboardResult


class DashboardService:
    def __init__(self) -> None:
        self._latest: Optional[DashboardResult] = None

    def set_latest(self, dashboard_result: DashboardResult) -> None:
        self._latest = dashboard_result

    def get_latest(self) -> Optional[DashboardResult]:
        return self._latest
