# H4-10 Gemini End-to-End Live Validation Report

**Document:** H4_10_GEMINI_LIVE_VALIDATION_REPORT.md
**Status:** Final
**Companion documents:** docs/H4_10_LIVE_VALIDATION_REPORT.md (OpenAI live
validation), docs/H4_10_VALIDATION_REPORT.md, docs/H4_RUNTIME_EVENT_CONTRACT.md

This report records the results of the live-credential Gemini validation
of `tests/test_h4_gemini_validation.py` (H4-9 scope). With
`GEMINI_API_KEY` present and Cloud Prepay credits replenished, the
`GeminiProvider.generate()` → Typed Event ("reply") → Verification
Runtime → Trust Runtime → Dashboard Runtime → Event Aggregator → FastAPI
chain was exercised end-to-end against real Gemini API traffic (not
fixtures, not mocks). No frozen component was modified: `GeminiProvider`,
Verification Runtime, Trust Runtime, Dashboard Runtime, Event Aggregator,
and FastAPI are all byte-identical to their state at the start of this
validation. The only file created by this validation is this report.

**Scope note (architecture, not a limitation introduced here):**
`tests/test_h4_gemini_validation.py` is designed, per its own module
docstring and its AST-enforced import guard
(`test_module_never_imports_cloud_run_runtime_or_openai`), to validate
Gemini strictly at the Provider → `ProviderResponse` boundary. It builds
the Typed Event directly from the real `ProviderResponse` (test-local
dict literal, shaped exactly per `H4_RUNTIME_EVENT_CONTRACT.md`'s "reply"
payload) and never imports or drives `phantom_runtime.py`,
`runtime.cloud_run_shell`, or `runtime.event_adapter.RuntimeEventAdapter`.
This is the same, already-approved H4-9 scope boundary — not a change or
reduction made during this validation. Sections 2 and 3 below report
against that actual scope.

**Update:** the Cloud Run Runtime → Runtime Adapter hop left N/A by §3's
scope boundary has since been exercised separately, with real data, in
`docs/H4_10_VALIDATION_REPORT.md` §4 (Production-like Validation). See
§3.1 below for that addendum.

---

## 1. Gemini Live Validation

| Check | Result |
|---|---|
| `GEMINI_API_KEY` configured | PASS — present in the validation environment, Cloud Prepay credits replenished since the prior 429 RESOURCE_EXHAUSTED run |
| Live Test executed (not skipped) | PASS — `GeminiLiveRequestValidationTests` (5 tests) ran under `unittest`; only `GatingBehaviorTests.test_live_tests_self_skip_without_a_real_api_key` self-skipped, the correct designed behavior when a real key **is** present |
| `tests/test_h4_gemini_validation.py` run result | `Ran 11 tests in 2.217s` — `OK (skipped=1)` |
| Failures | 0 |
| Errors | 0 |
| Real Gemini API communication succeeded | PASS — independently re-verified outside the test suite via a direct `GeminiProvider.generate()` call: `text='P'`, `finish_reason='MAX_TOKENS'`, real token-usage metadata `{'prompt_tokens': 10, 'completion_tokens': 1, 'total_tokens': 11}` (server-computed token accounting, not reproducible by a mock; `finish_reason='MAX_TOKENS'` — with `max_tokens=5` on the same prompt the test suite itself uses — reflects genuine server-side truncation behavior, not a canned value) |

---

## 2. Runtime Event

Per the H4-9 scope (see note above), "Runtime Event" for this Gemini
validation is the Contract-shaped Typed Event dict built directly from
the real `ProviderResponse`, via the test's own
`_reply_event_from_provider_response()` glue (a plain dict literal, not a
new class/DTO — confirmed by
`test_module_defines_no_new_dataclass`).

Real event captured from a live call:

```json
{
  "schema_version": "1.0",
  "event_id": "evt-gemini-report",
  "timestamp": "2026-07-05T23:47:16.784039+00:00",
  "session_id": "sess-gemini-report",
  "sequence": 1,
  "type": "reply",
  "payload": {
    "provider": "gemini",
    "model": "gemini-2.5-flash",
    "text": "P",
    "finish_reason": "MAX_TOKENS"
  }
}
```

| Check | Result |
|---|---|
| Envelope fields | PASS — all 6 Contract envelope keys present (`schema_version`, `event_id`, `timestamp`, `session_id`, `sequence`, `type`) |
| `type` | PASS — `"reply"` |
| `payload` field set | PASS — exactly the Contract's 4 documented "reply" fields (`provider`, `model`, `text`, `finish_reason`); no extra, missing, or renamed keys |
| `payload.text` / `payload.finish_reason` | PASS — copied verbatim from the real `ProviderResponse`, not fabricated |

Result: **PASS** — Runtime Event generated correctly from real
Gemini-derived data.

---

## 3. Runtime Adapter

**N/A（設計対象外 / out of scope by design）.** This Gemini validation
does not exercise `runtime.event_adapter.RuntimeEventAdapter`. Unlike the
OpenAI validation (docs/H4_10_LIVE_VALIDATION_REPORT.md §3), which drove
the real Cloud Run Runtime wire format and its Adapter translation, the
Gemini test's own architecture (module docstring, §"Live network
requirement") builds the Typed Event directly from `ProviderResponse`
without a Cloud Run Runtime hop, and its AST guard
(`test_module_never_imports_cloud_run_runtime_or_openai`) actively
forbids importing `runtime.event_adapter`/`phantom_runtime`/
`cloud_run_shell` from this test module. No Runtime Adapter behavior was
claimed, exercised, or (per the prohibition on design changes in this
task) introduced.

---

## 3.1 Cloud Run Runtime × Gemini Dynamic Validation (Production-like Addendum)

This addendum closes the §3 boundary: it validates the Cloud Run Runtime
→ Runtime Adapter hop for Gemini specifically, using a real running
container rather than the test module above (whose own scope
deliberately excludes it). Full detail and methodology in
`docs/H4_10_VALIDATION_REPORT.md` §4; summarized here for completeness.

**Setup:** built the current, unmodified `Dockerfile` into
`phantom-runtime-lite:h4-10-prodlike`, ran it as a real container with
`PROVIDER=gemini` and both `GEMINI_API_KEY`/`OPENAI_API_KEY` set.
Confirmed via `docker exec` that `RuntimeConfig.from_env().provider ==
"gemini"` inside the running container (`src/config.py`'s `_selected_provider`
branch at `phantom_runtime.py`'s import time selects `GeminiProvider` for
reply generation). Note: speech-to-text (`transcribe()`) is hardcoded to
OpenAI Whisper regardless of `PROVIDER` (`phantom_runtime.py`'s own
comment: Whisper transcription is "out of scope for the Provider"
abstraction) — only the reply-generation stage is Gemini here. This is a
pre-existing architecture property, reported transparently, not a gap
introduced by this addendum.

Real synthesized speech audio (raw PCM16LE mono 16kHz, matching
`--audio-source fd`'s expected format) was streamed to the container's
`/ws` over an actual WebSocket connection (`runtime.transport_gateway`,
unmodified). Real `_emit_event` lines were relayed back and captured:

```json
{"version": 1, "type": "transcript", "payload": {"text": "Hello, when can you start the new position?", "lang": "japanese", "ts": "07:09:31", "speaker": "user"}}
{"version": 1, "type": "reply", "payload": {"lang": "ja", "text": "（いつから勤務可能か尋ねられています）", "speaker": "agent"}}
{"version": 1, "type": "latency", "payload": {"stt_ms": 1883.24, "gpt_ms": 1456.02, "total_ms": 3339.25}}
```

The `reply` text is a real Gemini-generated paraphrase (distinct in
style from the OpenAI-provider container's direct-answer reply captured
in the same session — independent evidence this run was not silently
falling back to OpenAI).

| Check | Result |
|---|---|
| Container startup with `PROVIDER=gemini` | PASS |
| `GET /healthz` | PASS — HTTP 200 |
| `/ws` real audio in → real event out | PASS — all 3 real events (`transcript`, `reply`, `latency`) received over the WebSocket |
| Runtime Adapter (`RuntimeEventAdapter.translate()`) | PASS — all 3 real events translated to the 7-key Contract envelope; GAP fields (e.g. `reply.provider`/`model`/`finish_reason`, `transcript.confidence`/`is_final`) correctly left absent, not fabricated |
| Verification Runtime | PASS — `VerificationResult` generated for all 3; `gap_detected=True` for all 3 (real events are a strict subset of the Contract, per the Mapping Report's documented, pre-existing finding — same as every other real-event run in this validation series), `reliability_score=0.5` |
| Trust Runtime | PASS — `TrustResult` generated for all 3; `trust_score=0.5`, `trust_level="CAUTION"`, `human_review_required=False`, matching the documented Trust Policy formula |
| Dashboard Runtime | PASS — Verification/Trust display matched source results exactly for all 3 |
| FastAPI (`POST /aggregate`) | PASS — HTTP 200 for all 3; 0 field loss, 0 field rename, 0 field recomputation across all 3×4 dataclass instances |
| Container Shutdown | PASS — `docker stop` (SIGTERM) → `[cloud_run_shell] SIGTERM received — forwarding SIGINT to runtime child` → clean shutdown → `runtime child exited (code=0)` → container `ExitCode=0` |
| Failures | 0 |
| Errors | 0 |

**Result: PASS.** The Cloud Run Runtime → `_emit_event()` → Runtime
Adapter → Verification Runtime → Trust Runtime → Dashboard Runtime →
FastAPI chain is now confirmed end-to-end for Gemini with real data, not
just for OpenAI. §3's N/A remains an accurate description of
`tests/test_h4_gemini_validation.py`'s own scope (which still does not
exercise this path) — it is not a gap in H4-10's overall Gemini coverage,
which this addendum closes via a separate, real-container run.

---

## 4. Verification Runtime

The real reply event above was handed to the real, unmodified
`verification.verification_runtime.VerificationRuntime`.

| Check | Result |
|---|---|
| `VerificationResult` generated | PASS |
| `gap_detected` | `False` — all 4 Contract "reply" fields present and correctly typed |
| `fallback_detected` | `False` — `finish_reason="MAX_TOKENS"` is not case-insensitively equal to `"fallback"` |
| `reliable` | `True` |
| `reliability_score` | `1.0` |
| `warnings` | `[]` |
| `explanation` | `"No gap or fallback detected for event type 'reply'."` |

Consistent with the test suite's own
`test_real_reply_event_flows_through_full_pipeline` assertions (all 4
tests in `GeminiLiveRequestValidationTests` covering this path passed).

---

## 5. Trust Runtime

The VerificationResult above was handed to the real, unmodified
`trust.trust_runtime.TrustRuntime`.

| Check | Result |
|---|---|
| `TrustResult` generated | PASS |
| `trust_score` | `1.0` |
| `trust_level` | `"TRUSTED"` |
| `human_review_required` | `False` |
| `contributing_factors` | `["no gap, fallback, or warnings reported by verification"]` |
| `explanation` | `"Trust Policy classified this event as 'TRUSTED' (trust_score=1.00): no gap, fallback, or warnings reported by verification."` |

Matches the deterministic Trust Policy formula for a clean
VerificationResult (`reliability_score=1.0`, no gap/fallback/warnings →
`trust_score=1.0`), and matches
`test_real_reply_event_flows_through_full_pipeline`'s assertions.

---

## 6. FastAPI

The (VerificationResult, TrustResult) pair was rendered into a
DashboardResult and combined into an EventAggregate (both real,
unmodified), then POSTed to the real, unmodified FastAPI app's
`POST /aggregate` via `TestClient`.

| Check | Result |
|---|---|
| `EventAggregate` generated | PASS |
| `POST /aggregate` | **HTTP 200** |
| JSON output | PASS — valid JSON, full nested content present |
| Field loss | **None** — JSON key sets for `EventAggregate`/`VerificationResult`/`TrustResult`/`DashboardResult` matched each dataclass's own `fields()` exactly (independently re-verified: all 4 `match: True`) |
| Field rename | **None** |
| Field recomputation | **None** — every value (`trust_score`, `trust_level`, `reliability_score`, `gap_detected`, etc.) identical between in-process objects and the FastAPI JSON response; only the timestamp string spelling differed (`Z` suffix vs. `+00:00`, same UTC instant — serialization format, not recomputation) |

Matches `test_real_reply_event_reaches_fastapi_as_lossless_json`'s
assertions (passed).

---

## 7. Dashboard

The EventAggregate's embedded `dashboard_result` (real, unmodified
`DashboardRuntime.render()` output) was inspected directly.

| Check | Result |
|---|---|
| Verification表示 | PASS — `gap_detected=False`, `reliability_score=1.0`, `reliable=True`, `warnings=[]` match the source VerificationResult exactly |
| Trust表示 | PASS — `trust_score=1.0`, `trust_level="TRUSTED"`, `human_review_required=False`, `contributing_factors` match the source TrustResult exactly |
| Reply text表示 | **N/A（設計対象外）** — `DashboardResult` (src/dashboard/dashboard_result.py) declares no field carrying reply text; same pre-existing architecture boundary noted in docs/H4_10_LIVE_VALIDATION_REPORT.md §7, not introduced by this validation |
| Timeline表示 | **N/A（設計対象外）** — `DashboardRuntime.render(verification_result, trust_result) -> DashboardResult` remains a strict 1-in/1-out per-event transform; no session/multi-event aggregation exists in the Dashboard layer |

---

## 8. 総合結果 (Overall Result)

**PASS**

All in-scope components of the H4-9 Gemini validation (Runtime Event
generation from a real `ProviderResponse`, Verification Runtime, Trust
Runtime, Event Aggregator, FastAPI, and the Verification/Trust portions
of Dashboard) were validated end-to-end against real Gemini API traffic
with **0 Failures and 0 Errors** (`Ran 11 tests in 2.217s`,
`OK (skipped=1)`). The prior session's `429 RESOURCE_EXHAUSTED` (Cloud
Prepay credits depleted) no longer reproduces after credit replenishment.
Runtime Adapter and the Cloud Run Runtime wire-format hop are correctly
reported as out of scope for `tests/test_h4_gemini_validation.py`'s own
test design (see §3), consistent with this validation's transparency
standard — not concealed as a pass. That hop has since been validated
separately, with real data, in **§3.1** (also 0 Failures, 0 Errors) —
Gemini's Cloud Run Runtime × Runtime Adapter coverage is complete overall,
even though this specific test module does not exercise it.

---

## 9. Runtime Impact Analysis

- **Zero behavioral change** to any frozen component: `GeminiProvider`,
  Verification Runtime, Trust Runtime, Dashboard Runtime, Event
  Aggregator, and FastAPI are unchanged from their state at the start of
  this validation.
- **No code, design, Runtime, Provider, Runtime Contract, or Runtime
  Adapter changes** were made at any point in this validation.
- **No test code was modified.** `tests/test_h4_gemini_validation.py` was
  executed as-is; the additional single-call re-verification used to
  capture real field values for this report (§1–§7) was written to a
  session-local scratchpad directory outside the repository and deleted
  immediately after use.
- **The only file created by this entire live-validation exercise is
  this report** (`docs/H4_10_GEMINI_LIVE_VALIDATION_REPORT.md`).
  `git status` was confirmed unchanged (relative to the session's
  starting snapshot) before and after this validation.

---

## 10. 変更ファイル一覧 (Files Changed)

- `docs/H4_10_GEMINI_LIVE_VALIDATION_REPORT.md` (this file — updated with
  the §3.1 Production-like addendum)
- `docs/H4_10_VALIDATION_REPORT.md` (updated separately; see its own §4)

No other file in the repository was modified or deleted by this
validation or its addendum. The container used for §3.1 was removed
after the run; no volume was mounted into the working tree, and
`git status` was confirmed unchanged before and after.

## 作成ファイル一覧 (Files Created)

None in this update. (This file itself was originally created in an
earlier session.)
