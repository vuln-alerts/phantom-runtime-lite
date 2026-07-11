"""
runtime/pipeline_orchestrator.py
===================================
RuntimePipelineOrchestrator — makes the H4-2..H4-5 Runtime chain callable
from production code.

Every existing call site of this chain (RuntimeEventAdapter.translate ->
VerificationRuntime.handle -> TrustRuntime.handle -> DashboardRuntime.render
-> EventAggregator.aggregate) lives only in test helpers, e.g. _run_pipeline()
in tests/test_h4_10_integration_validation.py:127-136. This module wraps the
exact same processing order in a reusable, importable class -- it adds no
new behavior of its own and does not modify VerificationRuntime, TrustRuntime,
DashboardRuntime, or EventAggregator.

adapter and verification_runtime are accepted in the constructor and held
for the object's lifetime (rather than being created fresh per call, as
_run_pipeline()'s defaults do) because VerificationRuntime carries per-session
state (sequence/timestamp bookkeeping used for Gap Detection); reusing one
instance across multiple run() calls is required for that state to have any
effect across a session's events.

Conversation Traceability: run() also reads conversation_line/speaker/
transcript from raw_event["metadata"] (docs/H4_RUNTIME_EVENT_CONTRACT.md,
"Runtime Event Metadata") and passes them straight through to
DashboardRuntime.render(). This adds no new business logic -- it is a
verbatim read-and-forward, same as everything else in this module -- and
raw_event's metadata is never guessed when absent.

EXPORTED API:
  PipelineOutcome            -- the five artifacts one run() call produces
  RuntimePipelineOrchestrator(adapter=None, verification_runtime=None)
      .run(raw_event: dict) -> PipelineOutcome
"""

from dataclasses import dataclass
from typing import Any, Dict

from aggregator.event_aggregate import EventAggregate
from aggregator.event_aggregator import EventAggregator
from dashboard.dashboard_result import DashboardResult
from dashboard.dashboard_runtime import DashboardRuntime
from runtime.event_adapter import RuntimeEventAdapter
from trust.trust_result import TrustResult
from trust.trust_runtime import TrustRuntime
from verification.verification_result import VerificationResult
from verification.verification_runtime import VerificationRuntime


@dataclass(frozen=True)
class PipelineOutcome:
    event:                Dict[str, Any]
    verification_result:  VerificationResult
    trust_result:          TrustResult
    dashboard_result:      DashboardResult
    event_aggregate:       EventAggregate


class RuntimePipelineOrchestrator:
    """Runs one raw wire-format event through the full H4-2..H4-5 chain,
    in the same order as tests/test_h4_10_integration_validation.py's
    _run_pipeline(). See module docstring for why adapter and
    verification_runtime are held rather than recreated per call.
    """

    def __init__(self, adapter=None, verification_runtime=None):
        self._adapter = adapter if adapter is not None else RuntimeEventAdapter()
        self._verification_runtime = (
            verification_runtime if verification_runtime is not None else VerificationRuntime()
        )

    def run(self, raw_event: Dict[str, Any]) -> PipelineOutcome:
        event = self._adapter.translate(raw_event)
        verification_result = self._verification_runtime.handle(event)
        trust_result = TrustRuntime().handle(verification_result)

        # Conversation Traceability (docs/H4_RUNTIME_EVENT_CONTRACT.md,
        # "Runtime Event Metadata"): read verbatim from raw_event["metadata"]
        # and passed straight through to Dashboard -- never inferred, and
        # absent when raw_event carries no metadata.
        metadata = raw_event.get("metadata") or {}
        conversation_line = metadata.get("conversation_line")
        speaker = metadata.get("speaker")
        transcript = metadata.get("transcript")

        dashboard_result = DashboardRuntime().render(
            verification_result,
            trust_result,
            conversation_line=conversation_line,
            speaker=speaker,
            transcript=transcript,
        )
        event_aggregate = EventAggregator().aggregate(
            verification_result, trust_result, dashboard_result
        )
        return PipelineOutcome(
            event=event,
            verification_result=verification_result,
            trust_result=trust_result,
            dashboard_result=dashboard_result,
            event_aggregate=event_aggregate,
        )
