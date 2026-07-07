# Phantom Runtime Lite

## What is Phantom Runtime Lite

Phantom Runtime Lite is a lightweight **Conversational AI Agent Runtime** for
real-time operational assistance. Instead of treating every interaction as an
isolated prompt, it continuously observes a live conversation — transcribing
speech, inferring speakers, generating agent replies, and verifying the
reliability of every runtime event — to support a human operator in a
**Human-in-the-loop** workflow.

For the full product vision and problem statement, see
[docs/SUBMISSION_STORY.md](docs/SUBMISSION_STORY.md).

## Hackathon Submission Purpose

This repository is the **DevOps × AI Agent Hackathon 2026** public submission
of Phantom Runtime Lite. It contains the public hackathon edition of the
runtime, extended (H4) with a verification/trust/dashboard pipeline that
independently assesses the reliability of every runtime event, plus the
end-to-end and live-credential validation that backs this submission.

## Current Implementation

The Cloud Run deployment for this submission runs the **Production WebSocket
Runtime**: `runtime.cloud_run_shell` driving the unmodified
`phantom_runtime.py` conversational runtime, with `runtime.transport_gateway`
exposing `GET /healthz` (liveness) and `WS /ws` (live audio in / runtime
events out). This is what a Cloud Run URL visitor talks to today.

The following components are implemented and validated (see
[Validation Summary](#validation-summary) below) as the **H4 Extension** — an
event-driven, translation-only pipeline that consumes the Production
WebSocket Runtime's event stream:

- Runtime Event Contract
- Runtime Adapter
- Verification Runtime
- Trust Runtime
- Dashboard Runtime
- Event Aggregator
- FastAPI
  - `GET /health`
  - `POST /aggregate`

The H4 Extension is validated end-to-end (unit, integration, live
OpenAI/Gemini credentials, and a production-like Docker container — see
[Validation Summary](#validation-summary)) but is **not yet wired as a live
consumer of the deployed Cloud Run service** — see [Future Work](#future-work)
(Live consumer wiring). Validation results for these components (OpenAI Live
Validation, Gemini Live Validation, Production-like Validation, Regression)
are summarized in [Validation Summary](#validation-summary) below.

## Architecture

```text
                  Live Audio
                       │
                       ▼
        Speech Recognition
       (OpenAI Whisper)
                       │
                       ▼
      Phantom Runtime Lite
 Conversational AI Agent Runtime
                       │
                       ▼
                 Gemini API
             Reasoning & Assistance
                       │
                       ▼
         Human Decision Support
                       │
                       ▼
        Cloud Run Deployment
```

The H4 Extension adds an event-driven, translation-only pipeline that has
been implemented and validated (see
[Validation Summary](#validation-summary)) against the Cloud Run Runtime's
real event shape:

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

This pipeline is validated (unit, integration, and production-like Docker
tests — see [Validation Summary](#validation-summary)) but does not yet run
as a live consumer of the deployed Cloud Run service; the publicly deployed
Cloud Run URL serves the **Production WebSocket Runtime** only (`GET
/healthz`, `WS /ws`). Wiring this pipeline to that live `/ws` event stream is
tracked as **Future Work: Live consumer wiring** (see
[Future Work](#future-work)).

Cloud Run Runtime, Provider, Runtime Routing, and Whisper are unchanged by
this extension; the Runtime Event Contract is the single source of truth and
the Runtime Adapter performs Contract translation only. See
[docs/H4_STATUS.md](docs/H4_STATUS.md) for the full architecture summary.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (default OpenAI provider, microphone audio)
export OPENAI_API_KEY=sk-...
python -m src.phantom_runtime --profile default --mode light

# Or build and run the container (Cloud Run-style, audio over stdin/fd)
docker build -t phantom-runtime-lite .
docker run -e OPENAI_API_KEY=sk-... -p 8080:8080 phantom-runtime-lite
```

To use Gemini for reply generation instead of the default OpenAI provider,
connect to the Cloud Run WebSocket runtime at `/ws?provider=gemini` together
with `GEMINI_API_KEY` set. Provider selection is request-based and
session-scoped (H5-1): each `/ws` connection specifies `provider=openai` or
`provider=gemini` as a mandatory query parameter, validated by the Provider
Router (`runtime.provider_router`) before the Runtime child is spawned.
Note that speech-to-text (Whisper) always uses `OPENAI_API_KEY` regardless
of the session's selected provider.

## Required Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes | Whisper speech-to-text; default (OpenAI) reply provider |
| `GEMINI_API_KEY` | Only if a session selects `provider=gemini` | Gemini reply provider |

## Validation Summary

Full detail: [docs/H4_10_VALIDATION_REPORT.md](docs/H4_10_VALIDATION_REPORT.md)

* Tests Collected: 217
* Passed: 215
* Skipped: 2
* Failures: 0
* Errors: 0

| Validation | Result |
|---|---|
| OpenAI Live Validation | PASS — [docs/H4_10_LIVE_VALIDATION_REPORT.md](docs/H4_10_LIVE_VALIDATION_REPORT.md) |
| Gemini Live Validation | PASS — [docs/H4_10_GEMINI_LIVE_VALIDATION_REPORT.md](docs/H4_10_GEMINI_LIVE_VALIDATION_REPORT.md) |
| Production-like Validation | PASS — [docs/H4_10_VALIDATION_REPORT.md](docs/H4_10_VALIDATION_REPORT.md) §4 |
| Regression | PASS — [docs/H4_10_VALIDATION_REPORT.md](docs/H4_10_VALIDATION_REPORT.md) §5 |

Note: Production-like Validation (§4 of the linked report) was performed
against a dedicated validation image (`phantom-runtime-lite:h4-10-prodlike`),
not the publicly deployed Cloud Run image — the deployed Cloud Run service
currently runs the Production WebSocket Runtime only (see
[Architecture](#architecture) and [Future Work](#future-work)).

Current status overview: [docs/H4_STATUS.md](docs/H4_STATUS.md).

## Running Tests

```bash
pytest tests/
```

When both `OPENAI_API_KEY` and `GEMINI_API_KEY` are configured, the suite
reports 217 collected / 215 passed / 2 skipped, as in
[Validation Summary](#validation-summary) above. Without live API keys, the
skip count differs. See
[docs/H4_10_VALIDATION_REPORT.md](docs/H4_10_VALIDATION_REPORT.md) §5 for
full detail.

## Future Work

The remaining implementation work is tracked in:
[docs/H4_10_VALIDATION_REPORT.md](docs/H4_10_VALIDATION_REPORT.md) §7 Remaining Risks

- Live consumer wiring
- FastAPI endpoint expansion
- Status vocabulary alignment
- Durable identity/session persistence

See the linked section for full detail — it is not duplicated here.
