# Architecture

**Source:** README.md (System Overview / Components), docs/H4_STATUS.md (Architecture Summary), docs/H4_10_VALIDATION_REPORT.md §1

This document describes the full system as two independent processes. See
`README.md` "Architecture" for the equivalent mermaid diagram and
"Components" for the module-level breakdown.

## System Overview (two independent processes)

```text
Runtime process (deployed on Cloud Run)
  Runtime Client (runtime_client, mic / BlackHole)
      -> WS /ws?provider=openai|gemini (audio in)
      -> TransportGateway (runtime.transport_gateway, GET /healthz)
      -> spawns per session
      -> phantom_runtime.py (Whisper STT, OpenAI/Gemini LLM)
      -> _emit_event() Typed Events
      -> back through TransportGateway -> WS events out -> Runtime Client

Dashboard API process (not deployed to Cloud Run, local uvicorn only)
  FastAPI (api.api_server, POST /aggregate)
      -> RuntimePipelineOrchestrator (runtime.pipeline_orchestrator)
      -> Runtime Adapter (runtime.event_adapter)
      -> Verification Runtime
      -> Trust Runtime
      -> DashboardRuntime.render()
      -> Event Aggregator
      -> DashboardService (holds latest 1 result only)
      -> GET /dashboard (JSON) / GET / (HTML)
```

The Runtime process and the Dashboard API process do not import each other
and are not automatically wired together. The only bridge between them is
manual or script-driven: forward a raw `_emit_event()` line to
`scripts/post_dashboard_event.py`, which drives the pipeline above and POSTs
the result to `/aggregate`. There is no standing consumer that subscribes to
the Runtime's `/ws` stream automatically. See README.md "Dashboard API" for
full detail.

## Preserved Architecture (H4 Runtime Extension)

The H4 Runtime Extension (Verification Runtime / Trust Runtime / Dashboard
Runtime / Event Aggregator / FastAPI, i.e. everything inside the Dashboard
API process above) preserved the approved architecture of the pre-existing
Runtime process:

* Cloud Run Runtime unchanged
* Provider unchanged
* Runtime Routing unchanged
* Whisper unchanged
* Runtime Event Contract is the single source of truth
* Runtime Adapter performs Contract Translation only
* Single Runtime Policy maintained

## H4 Event Pipeline (Dashboard API process detail)

```text
Cloud Run Runtime (real _emit_event shape)
    -> Runtime Adapter (runtime.event_adapter)
    -> Verification Runtime
    -> Trust Runtime
    -> Dashboard Runtime
    -> Event Aggregator
    -> FastAPI POST /aggregate
    -> JSON
```

This pipeline is only reachable via `RuntimePipelineOrchestrator`, invoked
manually or via `scripts/post_dashboard_event.py` — it is not wired
automatically to the Runtime process's live `/ws` stream (see "System
Overview" above).

See docs/H4_10_VALIDATION_REPORT.md §1 for the full integration validation
against this pipeline, and docs/H4_STATUS.md for current completion status.
