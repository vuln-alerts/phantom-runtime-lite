"""
api/api_server.py
====================
H4-6 FastAPI Presentation Layer — a read-only, stateless HTTP surface over
EventAggregate, the immutable output of the H4-5 Event Aggregator.

    EventAggregate
          |
          v
      FastAPI
          |
        JSON

This module is Presentation Layer only. Its two routes:

  GET  /health     -> {"status": "ok"}
  POST /aggregate  -> the request body's EventAggregate, converted to
                       JSON via dataclasses.asdict() and nothing else

perform no verification, trust scoring, dashboard rendering, or
aggregation of their own, and hold no state across requests: every
request is independent. This module never imports or invokes the Cloud
Run Runtime, any Provider, Whisper, the Verification Runtime, the Trust
Runtime, the Dashboard Runtime, or the Event Aggregator — it imports only
the EventAggregate data contract itself (aggregator.event_aggregate),
which is the single source of truth for that shape, so this layer does
not redefine or copy it into a parallel DTO.

EXPORTED API:
  app — the FastAPI application instance
"""

from dataclasses import asdict

from fastapi import FastAPI

from aggregator.event_aggregate import EventAggregate
from api.api_models import HealthResponse

app = FastAPI(title="Phantom Runtime Lite — Read-Only Event API")


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
    return asdict(event_aggregate)
