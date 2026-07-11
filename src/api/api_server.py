"""
api/api_server.py
====================
H4-6 FastAPI Presentation Layer over EventAggregate, the immutable output
of the H4-5 Event Aggregator, plus a read of the single most recent
DashboardResult it carries.

    EventAggregate
          |
          v
      FastAPI
          |
        JSON

Routes:

  GET  /health     -> {"status": "ok"}. Fully stateless, unchanged.
  POST /aggregate  -> the request body's EventAggregate, converted to
                       JSON via dataclasses.asdict() and nothing else.
                       Request shape, response shape, and status codes are
                       unchanged from before Dashboard support was added.
                       The one addition is a side effect: the
                       DashboardResult already carried by the received
                       EventAggregate is handed to DashboardService, so
                       that GET /dashboard and GET / can read it back.
  GET  /dashboard  -> the most recent DashboardResult (via
                       DashboardService), as JSON. 404 if none has been
                       received yet.
  GET  /           -> the same, rendered as an HTML page (via
                       api.dashboard_view).

This module still never imports or invokes the Cloud Run Runtime, any
Provider, Whisper, the Verification Runtime, the Trust Runtime, the
Dashboard Runtime, or the Event Aggregator directly — the only
Dashboard-related state it touches is api.dashboard_service.DashboardService,
which itself only holds/returns the latest DashboardResult and never runs
the Verification -> Trust -> Dashboard -> Aggregator chain. This module
still imports the EventAggregate data contract itself
(aggregator.event_aggregate), which remains the single source of truth for
that shape, so this layer does not redefine or copy it into a parallel DTO.

EXPORTED API:
  app — the FastAPI application instance
"""

from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from aggregator.event_aggregate import EventAggregate
from api.api_models import HealthResponse
from api.dashboard_service import DashboardService
from api.dashboard_view import render_dashboard_html

app = FastAPI(title="Phantom Runtime Lite — Read-Only Event API")

_dashboard_service = DashboardService()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness check. No Runtime Contract involved."""
    return HealthResponse(status="ok")


@app.post("/aggregate")
def aggregate(event_aggregate: EventAggregate) -> dict:
    """Convert the received EventAggregate to JSON and return it verbatim.

    No field is renamed, dropped, derived, or recomputed: asdict() walks
    the dataclass tree and produces a dict with the same field names and
    values as the EventAggregate (and the VerificationResult, TrustResult,
    DashboardResult it references) received in the request.
    """
    _dashboard_service.set_latest(event_aggregate.dashboard_result)
    return asdict(event_aggregate)


@app.get("/dashboard")
def dashboard() -> dict:
    """The most recent DashboardResult as JSON, or 404 if none yet."""
    latest = _dashboard_service.get_latest()
    if latest is None:
        raise HTTPException(status_code=404, detail="no DashboardResult yet")
    return asdict(latest)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """The most recent DashboardResult, rendered as a simple HTML page."""
    return render_dashboard_html(_dashboard_service.get_latest())
