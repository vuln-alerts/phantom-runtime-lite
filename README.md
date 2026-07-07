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

The following components are implemented and validated (see
[Validation Summary](#validation-summary) below):

- Runtime Event Contract
- Runtime Adapter
- Verification Runtime
- Trust Runtime
- Dashboard Runtime
- Event Aggregator
- FastAPI
  - `GET /health`
  - `POST /aggregate`

Validation results for these components (OpenAI Live Validation, Gemini Live
Validation, Production-like Validation, Regression) are summarized in
[Validation Summary](#validation-summary) below.

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

The H4 extension adds an event-driven, translation-only pipeline downstream of
the Cloud Run Runtime:

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

To use Gemini for reply generation instead of the default OpenAI provider, set
`PROVIDER=gemini` together with `GEMINI_API_KEY`. Note that speech-to-text
(Whisper) always uses `OPENAI_API_KEY` regardless of `PROVIDER`.

## Required Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes | Whisper speech-to-text; default (OpenAI) reply provider |
| `GEMINI_API_KEY` | Only if `PROVIDER=gemini` | Gemini reply provider |

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

Current status overview: [docs/H4_STATUS.md](docs/H4_STATUS.md).

## Future Work

The remaining implementation work is tracked in:
[docs/H4_10_VALIDATION_REPORT.md](docs/H4_10_VALIDATION_REPORT.md) §7 Remaining Risks

- Live consumer wiring
- FastAPI endpoint expansion
- Status vocabulary alignment
- Durable identity/session persistence

See the linked section for full detail — it is not duplicated here.
