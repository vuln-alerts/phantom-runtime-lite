"""
api/api_models.py
====================
Presentation-only response shapes for the H4-6 FastAPI Presentation Layer.

Per the H4-6 design revision, this module holds no mirror of the
EventAggregate / VerificationResult / TrustResult / DashboardResult
Runtime Contracts — those are defined exactly once in aggregator,
verification, trust, and dashboard, and api_server.py serializes
EventAggregate directly (via dataclasses.asdict) rather than duplicating
its shape here. The only response shape owned by this layer is the one
with no Runtime Contract of its own: the health check.

EXPORTED API:
  HealthResponse — {"status": "ok"} response shape for GET /health
"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
