# H4-10 Cloud Run Runtime Integration Validation Report

**Document:** H4_10_VALIDATION_REPORT.md
**Status:** Final
**Companion documents:** docs/H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md
(Runtime Event Analysis + Contract Mapping + Resolved Decisions),
docs/H4_10_LIVE_VALIDATION_REPORT.md (OpenAI live-credential validation),
docs/H4_10_GEMINI_LIVE_VALIDATION_REPORT.md (Gemini live-credential validation)

This report covers H4-10 end-to-end validation, live-credential validation
(OpenAI and Gemini), production-like validation (both providers, driven
through a real running Docker container), and the final regression check.
No frozen component was modified: `phantom_runtime.py`,
`runtime/cloud_run_shell.py`, `runtime/transport_gateway.py`,
Providers, Verification Runtime, Trust Runtime, Dashboard Runtime, Event
Aggregator, and FastAPI are all unchanged from the versions already on disk
at the start of this work. The only new code is:

- `src/runtime/event_adapter.py` — the Runtime Adapter (Contract translation only)
- `tests/test_h4_10_runtime_adapter.py` — adapter unit tests
- `tests/test_h4_10_integration_validation.py` — full-pipeline integration tests

---

## 1. Integration Validation Report

Pipeline exercised, using literal real `_emit_event(...)` wire-format
fixtures (not synthetic Contract-shaped events):

```
Cloud Run Runtime (real _emit_event shape)
    -> Runtime Adapter (runtime.event_adapter)
    -> Verification Runtime (H4-2, unmodified)
    -> Trust Runtime (H4-3, unmodified)
    -> Dashboard Runtime (H4-6, unmodified)
    -> Event Aggregator (H4-5, unmodified)
    -> FastAPI POST /aggregate (H4-6, unmodified)
    -> JSON
```

| Requirement | Result |
|---|---|
| Contract compliance | PASS — every translated event has exactly the 7 Contract envelope keys (`schema_version, event_id, timestamp, session_id, sequence, type, payload`); `type` is always one of the 6 Contract-defined values (`ContractComplianceTests`). |
| Identity propagation | PASS — adapter-generated `event_id`/`session_id` propagate unchanged through VerificationResult → TrustResult → DashboardResult → EventAggregate → JSON (`IdentityPropagationTests`). Sequence increments monotonically across a multi-event session from one adapter instance. |
| No field loss | PASS — JSON response field sets match `EventAggregate`/`VerificationResult`/`TrustResult`/`DashboardResult`'s own `__dataclass_fields__` exactly, for real-event-derived pipeline output (`FieldConsistencyTests.test_no_field_lost_across_the_full_json_boundary`). |
| No field rename | PASS — same test; no JSON key differs from its dataclass field name. |
| No field recomputation | PASS — `reliability_score`/`trust_score` are bit-identical between the in-process pipeline result and the FastAPI JSON response (`test_no_field_renamed_or_recomputed_across_the_full_json_boundary`). |
| JSON output | PASS — all 6 real event types reach `/aggregate` as HTTP 200 with a valid JSON body (`JsonOutputTests`). |
| Regression | PASS — full suite (§5) has 0 failures. |

Also confirmed: a real `reply` event (no `provider`/`model`/`finish_reason` on
the wire) correctly produces `gap_detected=True` all the way to JSON,
un-hidden by the adapter — the intended, honest behavior documented in the
Mapping Report.

All 15 tests in `tests/test_h4_10_integration_validation.py` pass, plus the
32 adapter-level tests in `tests/test_h4_10_runtime_adapter.py`.

---

## 2. OpenAI Live Validation

**Result: PASS** — full detail in `docs/H4_10_LIVE_VALIDATION_REPORT.md`.
With `OPENAI_API_KEY` present, the real, unmodified `phantom_runtime.py`
was driven end-to-end against genuine OpenAI traffic (Whisper +
Chat Completions, not mocks/fixtures).

| Check | Result |
|---|---|
| 実OpenAI API通信確認 | PASS — real `OpenAIProvider.generate()` call independently re-verified (`finish_reason='stop'`, real token-usage metadata) |
| Runtime Event生成確認 | PASS — all 5 Contract event types (`transcript`, `status`, `reply`, `latency`, `analysis`) emitted by the real, unmodified `_emit_event()` over a real pipe, from genuine Whisper/Chat Completions output |
| Runtime Adapter確認 | PASS — all 5 real events translated by the real, unmodified `RuntimeEventAdapter`; GAP/EXTRA fields correctly preserved, none fabricated |
| Verification Runtime確認 | PASS — one `VerificationResult` per event, all 5; `gap_detected`/`reliability_score`/`warnings`/`explanation` all correctly derived from real payload content |
| Trust Runtime確認 | PASS — one `TrustResult` per event, all 5; `trust_score`/`trust_level` independently re-derived and matched against the documented Trust Policy formula |
| FastAPI確認 | PASS — all 5 events POSTed to `/aggregate` → HTTP 200; 0 field loss, 0 field rename, 0 field recomputation across all 5×4 dataclass instances |
| Dashboard Runtime確認 | PASS — Verification/Trust portions of `DashboardResult` match source results exactly, for all 5 events (Transcript/Reply/Timeline display are N/A by pre-existing design — Dashboard carries no such fields) |
| Failures | 0 |
| Errors | 0 |

---

## 3. Gemini Live Validation

**Result: PASS** — full detail in `docs/H4_10_GEMINI_LIVE_VALIDATION_REPORT.md`.
With `GEMINI_API_KEY` present, `tests/test_h4_gemini_validation.py`
(H4-9 scope) was driven against genuine Gemini API traffic.

| Check | Result |
|---|---|
| 実Gemini API通信確認 | PASS — real `GeminiProvider.generate()` call independently re-verified (`finish_reason='MAX_TOKENS'`, real server-computed token-usage metadata) |
| Runtime Event生成確認 | PASS — Contract-shaped `reply` Typed Event built directly from the real `ProviderResponse` (H4-9's own scope: Provider → Typed Event, not a Cloud Run Runtime wire-format event) |
| Runtime Adapter確認 | PASS — see note below; confirmed separately in §4 of this report, not within the H4-9 test's own scope |
| Cloud Run Runtime起動確認 | PASS — see note below; confirmed separately in §4 of this report, not within the H4-9 test's own scope |
| Verification Runtime確認 | PASS — `gap_detected=False`, `reliability_score=1.0`, `warnings=[]` (all 4 Contract "reply" fields present and correctly typed) |
| Trust Runtime確認 | PASS — `trust_score=1.0`, `trust_level="TRUSTED"`, `human_review_required=False` |
| FastAPI確認 | PASS — `POST /aggregate` → HTTP 200; 0 field loss, 0 field rename, 0 field recomputation |
| Dashboard Runtime確認 | PASS — Verification/Trust portions of `DashboardResult` match source results exactly (Reply text/Timeline display are N/A by pre-existing design) |
| Failures | 0 |
| Errors | 0 |

**Note (scope + closure):** `tests/test_h4_gemini_validation.py` is
designed, per its own AST-enforced import guard
(`test_module_never_imports_cloud_run_runtime_or_openai`), to validate
Gemini strictly at the Provider → `ProviderResponse` boundary — it does
not import or drive `phantom_runtime.py`, `runtime.cloud_run_shell`, or
`runtime.event_adapter`. This is an intentional H4-9 scope boundary, not a
gap introduced here. The Cloud Run Runtime → Runtime Adapter hop for
Gemini is validated separately, with real data, in **§4 Production-like
Validation** below (`PROVIDER=gemini` driven through the actual container),
which closes this boundary rather than leaving it unexercised.

---

## 4. Production-like Validation Report

Performed against a freshly built image
(`phantom-runtime-lite:h4-10-prodlike`) from the current, unmodified
`Dockerfile`, run as two separate real containers — one with the default
OpenAI provider, one with `PROVIDER=gemini` — each driven end-to-end over
its actual network boundary (`/healthz`, `/ws`), not by importing
`phantom_runtime.py` as a library.

| Step | Result |
|---|---|
| Docker Build | PASS — `docker build` succeeded using the unmodified `Dockerfile` (base `python:3.14-slim`, PortAudio system deps, unmodified `CMD ... --audio-source fd`) |
| Container Startup | PASS — both containers (OpenAI-provider, `PROVIDER=gemini`) started cleanly; logs show real startup sequence (profile load, `[audio] fd-source active — awaiting client audio stream`) |
| Health Check | PASS — `GET /healthz` → HTTP 200 on both containers; Docker's own `HEALTHCHECK` reports `healthy` |
| WebSocket | PASS — connected to `/ws` on both containers and streamed real synthesized speech audio (raw PCM16LE mono 16kHz, matching `phantom_runtime.py`'s `--audio-source fd` format); relayed real `_emit_event` JSON lines received back on both |
| Runtime Adapter | PASS — real events captured from both containers (`transcript`, `reply`, `latency`) translated by the real, unmodified `RuntimeEventAdapter`; Contract envelope correct on all |
| Verification Runtime | PASS — `VerificationResult` generated for all captured events on both containers; `gap_detected=True` correctly reported (real events are a subset of the frozen Contract, per the Mapping Report's documented finding) |
| Trust Runtime | PASS — `TrustResult` generated for all events on both containers; `trust_score=0.5`, `trust_level="CAUTION"` per the documented Trust Policy formula |
| FastAPI | PASS — all events POSTed to `/aggregate` → HTTP 200 on both; 0 field loss, 0 field rename, 0 field recomputation |
| Dashboard Runtime | PASS — Verification/Trust display matched source results exactly for all events on both |
| End-to-End | PASS — real audio in → real STT → real reply generation → real event emission → WebSocket relay → Runtime Adapter → Verification → Trust → Dashboard → FastAPI, confirmed for both the OpenAI provider and `PROVIDER=gemini` |
| Container Shutdown | PASS — `docker stop` (SIGTERM) on both containers: `[cloud_run_shell] SIGTERM received — forwarding SIGINT to runtime child` → clean shutdown → `runtime child exited (code=0)` → container `ExitCode=0` |
| Failures | 0 |
| Errors | 0 |

Also confirmed: `requirements.txt` installs cleanly both in this session's
venv and inside the Docker build layer. FastAPI's own implemented surface
remains only `/health` and `/aggregate` — `/events`, `/verification`,
`/trust`, `/timeline` are documented in `docs/H4_IMPLEMENTATION_PLAN.md`
but not implemented in `src/api/api_server.py`; this is a pre-existing gap
unrelated to and not fixed by H4-10 (FastAPI is frozen for this task).

No file in the repository was created, modified, or read/write-affected by
this container testing — all containers ran in isolation with no volume
mount into the working tree; `git status` was confirmed unchanged before
and after.

---

## 5. Regression Summary

```
217 tests collected
215 passed
2 skipped
0 failures
0 errors
```

- 0 failures, 0 errors, across the entire `tests/` directory (H4-2 through
  H4-9's existing suites, plus the two new H4-10 modules).
- **2 skips are expected in this environment.** Both `OPENAI_API_KEY` and
  `GEMINI_API_KEY` are configured, so the live OpenAI/Gemini test classes
  (`OpenAILiveRequestValidationTests`, `GeminiLiveRequestValidationTests`)
  actually ran against real APIs and passed — they are not among the
  skips. The 2 skips are exclusively
  `GatingBehaviorTests.test_live_tests_self_skip_without_a_real_api_key`
  (one in each of `tests/test_h4_openai_validation.py` and
  `tests/test_h4_gemini_validation.py`), which is designed to self-skip
  precisely when a real key **is** present — the correct, inverse-gating
  behavior, not a regression. (In a no-key environment this count would
  instead show as 10 skips, per the original H4-8/H4-9 baseline; the
  lower count here reflects a more fully-exercised run, not missing
  coverage.)
- No existing test file was modified.
- No regression from OpenAI Live Validation, Gemini Live Validation, or
  Production-like Validation: all three completed with 0 failures/errors,
  and the full suite remains green.

---

## 6. Runtime Impact Analysis

- **Zero behavioral change** to Cloud Run Runtime: `phantom_runtime.py`,
  `runtime/cloud_run_shell.py`, and `runtime/transport_gateway.py` are
  byte-identical to their state at the start of this task (all three were
  already-uncommitted H3 work; H4-10 read them but wrote nothing to them).
- **New, additive-only code**: `runtime/event_adapter.py` is a standalone
  module with no side effects — it is not imported by, and does not alter,
  any existing entrypoint (`cloud_run_shell.main()`'s pipeline is unchanged;
  the adapter is invoked only by the H4-10 test modules and this
  validation's own ad hoc scripts, run outside the repository). Wiring it
  into a permanent, always-on consumer of `transport_gateway`'s `/ws`
  stream remains a deployment decision outside this validation task's
  scope (see Remaining Risks §7).
- **Central finding** (from the Mapping Report, confirmed here against the
  real, running system, for both providers): the live Cloud Run Runtime's
  event payloads are a strict subset of what the frozen Contract requires.
  Every real `reply`, `transcript`, `analysis`, `latency`, and `status`
  event will report `gap_detected=True` under Verification Runtime once
  translated — correctly and by design, not as an adapter defect. Only
  `error` events come closest to gap-free (missing only
  `code`/`recoverable`).
- No new Runtime, no duplicate Runtime Contract, no duplicate DTO, and no
  Provider/Runtime refactoring were introduced, per the Implementation
  Plan's constraints.

---

## 7. Remaining Risks

1. **No live consumer wired to the adapter yet.** `runtime/transport_gateway.py`
   currently relays raw `_emit_event` lines verbatim to a WebSocket client;
   nothing in production today reads that stream, runs it through
   `RuntimeEventAdapter`, and feeds the H4-2..H4-6 chain automatically.
   This validation (§4) proves the adapter + pipeline combination is
   correct for real events from both providers, driven by an ad hoc
   script outside the repository — but deploying a standing process that
   actually connects to `/ws` and drives this chain live in production is
   a follow-up integration step, not part of H4-10's "translation only, no
   Runtime modification" scope.
2. **FastAPI endpoint gap vs. the Implementation Plan.** `/events`,
   `/verification`, `/trust`, `/timeline` are documented in
   `docs/H4_IMPLEMENTATION_PLAN.md` but not implemented in
   `src/api/api_server.py` (only `/health` and `/aggregate` exist). This
   predates H4-10 and is out of scope to fix here (FastAPI is frozen for
   this task), but it means a real dashboard/API consumer cannot query
   historical events/verification/trust/timeline yet — only push one
   `EventAggregate` at a time via `POST /aggregate`.
3. **Status vocabulary mismatch is permanent under the current design.**
   Per the approved Resolved Decision, `status.state` passes through
   untranslated. Every real status event — including `idle` — will show
   as `undefined state` to Verification Runtime, since the Contract enum
   is uppercase and the real enum's other 5 values have no Contract
   equivalent at all. If a future revision wants clean `status` gap
   scores, that requires either a Contract change (new state enum) or a
   Runtime change (emit Contract-vocabulary states) — both out of scope
   for a translation-only adapter.
4. **Identity fields are adapter-local, not Runtime-durable.** `event_id`/
   `session_id`/`sequence` only exist from the point the adapter is
   instantiated; they are not persisted or recoverable if the adapter
   process restarts mid-session. This is inherent to "generate, don't
   fabricate business meaning" and was an explicit, approved trade-off,
   but is worth flagging for anyone relying on `session_id` continuity
   across adapter restarts.
