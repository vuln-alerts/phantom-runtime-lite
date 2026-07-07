# Architecture

**Source:** docs/H4_STATUS.md (Architecture Summary), docs/H4_10_VALIDATION_REPORT.md §1

## Preserved Architecture

The H4 Runtime Extension preserved the approved architecture:

* Cloud Run Runtime unchanged
* Provider unchanged
* Runtime Routing unchanged
* Whisper unchanged
* Runtime Event Contract is the single source of truth
* Runtime Adapter performs Contract Translation only
* Single Runtime Policy maintained

## H4 Event Pipeline

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

See docs/H4_10_VALIDATION_REPORT.md §1 for the full integration validation
against this pipeline, and docs/H4_STATUS.md for current completion status.
