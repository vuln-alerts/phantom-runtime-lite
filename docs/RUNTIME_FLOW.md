# Runtime Flow

**Source:** docs/H4_10_VALIDATION_REPORT.md §1 (Integration Validation Report)

Pipeline exercised in H4-10 validation, using real `_emit_event(...)` wire-format
events (not synthetic Contract-shaped events):

```text
Cloud Run Runtime (real _emit_event shape)
    -> Runtime Adapter (runtime.event_adapter)
    -> Verification Runtime (H4-2, unmodified)
    -> Trust Runtime (H4-3, unmodified)
    -> Dashboard Runtime (H4-6, unmodified)
    -> Event Aggregator (H4-5, unmodified)
    -> FastAPI POST /aggregate (H4-6, unmodified)
    -> JSON
```

For the full validation results against this flow (Contract compliance,
identity propagation, field consistency, JSON output), see
docs/H4_10_VALIDATION_REPORT.md §1.
