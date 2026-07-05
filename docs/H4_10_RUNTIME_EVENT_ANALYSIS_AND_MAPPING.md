# H4-10 Runtime Event Analysis & Contract Mapping Report

**Document:** H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md
**Status:** Approved — decisions recorded in "Resolved Decisions" at the end
**Scope:** Tasks 1–3 of H4-10 (Runtime Event Analysis, Contract Mapping, and the
design questions that must be resolved before any adapter code is written).

This report does not modify `phantom_runtime.py`, `runtime/cloud_run_shell.py`,
`runtime/transport_gateway.py`, any Provider, or any H4-2..H4-6 component. It is
read-only analysis of what is already on disk (currently uncommitted H3
client-cloud transport work: `_emit_event`, `--audio-source fd`,
`runtime/transport_gateway.py`).

---

## 1. Runtime Event Analysis — what Cloud Run Runtime actually emits

`_emit_event` (src/phantom_runtime.py:593) is a no-op unless `PHANTOM_EVENT_FD`
is set (i.e. unless spawned by `runtime.cloud_run_shell`). When active, every
call writes one JSON line:

```json
{"version": 1, "type": "<event_type>", "timestamp": "<iso8601 utc>", "payload": {...}}
```

`runtime/transport_gateway.py` relays these lines verbatim to a connected
WebSocket client — it does not parse or reshape them (confirmed by reading
`_pump_events_from_pipe`/`_drain_event_queue`). So the JSON shown below is
exactly what a downstream consumer receives today; nothing further along the
Shell touches it.

There are **9 call sites**, covering all 6 Contract event types:

| Line | Type | Call | Payload keys emitted |
|---|---|---|---|
| 822 | status | `_set_state` | `state`, `previous` |
| 892 | error | `show_err` | `label`, `message` |
| 900 | latency | `show_latency` | `stt_ms`, `gpt_ms`, `total_ms` |
| 2250 | reply | `_emit_line` ([JP] line) | `lang="ja"`, `text`, `speaker="agent"` |
| 2254 | reply | `_emit_line` ([EN] line) | `lang="en"`, `text`, `speaker="agent"` |
| 2258 | reply | `_emit_line` ([READ] line) | `lang="pronunciation"`, `text`, `speaker="agent"` |
| 3097 | transcript | `reply_worker` (heard speech) | `text`, `lang`, `ts`, `speaker` |
| 3222 | reply | `reply_worker` (final agent reply) | `text`, `lang="en"`, `speaker="agent"`, `ts` |
| 9134 | analysis | `generate_meeting_analysis` | `text` |

Key facts that affect mapping:

- `state` values come from `ConversationState` (phantom_runtime.py:801):
  `idle`, `recruiter_speaking`, `user_speaking`, `waiting_for_reply`,
  `generating`, `speaking` — lowercase, and **none of the non-idle values
  have any equivalent in the Contract's 5-state enum**.
- `lang` values observed: `"ja"`, `"en"`, `"pronunciation"`, `"english"`
  (transcript's `lang` comes from `transcribe()`/`_detect_language()`, not
  shown above but distinct from the reply-side `"ja"/"en"` literals).
- `label` values (error) are free-form call-site strings (e.g. `"Audio"`,
  seen in the fd audio-source code at line ~3300+), not a formal error-code
  vocabulary.
- No call site ever passes `event_id`, `session_id`, or `sequence` — this
  matches the gap already documented in
  `src/verification/verification_runtime.py`'s module docstring (lines
  34–41), which treats all three as `Optional` and expects the gap "to
  close by H4-10 Final Validation."
- No call site passes `provider` or `model` for `reply` events, and no
  `finish_reason` is ever captured from the Provider's `ProviderResponse`
  into an emitted event.

---

## 2. Field-by-field Contract Mapping

Contract reference: `docs/H4_RUNTIME_EVENT_CONTRACT.md` ("Runtime Event
Envelope", "Event Payloads").

Legend: **DIRECT** = same name/meaning, no change · **RENAME** = same data,
different key name (defensible 1:1) · **GENERATED** = not present in source;
adapter must synthesize purely structural envelope metadata · **GAP** = data
the Contract requires that the real Runtime does not produce at all today —
the adapter must leave it absent rather than invent it · **EXTRA** = real
field with no Contract counterpart, carried through as additional payload
data (permitted — Contract's Backward Compatibility rule: "Consumers must
ignore unknown fields").

### Envelope

| Contract field | Source | Disposition |
|---|---|---|
| `schema_version` | `version: 1` | RENAME + reformat: `1` → `"1.0"` |
| `event_id` | *(absent)* | GENERATED (uuid4 per event) |
| `timestamp` | `timestamp` (ISO 8601 UTC) | DIRECT |
| `session_id` | *(absent)* | GENERATED (one id per Runtime process/adapter lifetime) |
| `sequence` | *(absent)* | GENERATED (monotonic counter, per session) |
| `type` | `type` | DIRECT — all 6 values already match the Contract's Event Types table exactly |
| `payload` | `payload` | mapped per-type below |

### `transcript`

| Contract field | Source field | Disposition |
|---|---|---|
| `text` | `text` | DIRECT |
| `language` | `lang` | RENAME |
| `confidence` | *(absent)* | **GAP** |
| `is_final` | *(absent)* | **GAP** |
| — | `ts`, `speaker` | EXTRA, passed through |

### `reply`

| Contract field | Source field | Disposition |
|---|---|---|
| `text` | `text` | DIRECT |
| `provider` | *(absent)* | **GAP** — no call site tags which provider produced the reply |
| `model` | *(absent)* | **GAP** |
| `finish_reason` | *(absent)* | **GAP** — `ProviderResponse.finish_reason` exists upstream in `provider.models` but is never threaded into `_emit_event` |
| — | `lang`, `speaker`, `ts` | EXTRA, passed through |

### `analysis`

| Contract field | Source field | Disposition |
|---|---|---|
| `summary` | `text` | RENAME *(judgment call — see Open Decisions)* |
| `intent` | *(absent)* | **GAP** |
| `metadata` | *(absent)* | **GAP** |

### `latency`

| Contract field | Source field | Disposition |
|---|---|---|
| `stt_ms` | `stt_ms` | DIRECT |
| `provider_ms` | `gpt_ms` | RENAME *(judgment call — see Open Decisions)* |
| `total_ms` | `total_ms` | DIRECT |
| `routing_ms` | *(absent)* | **GAP** — routing overhead is not measured as a discrete metric today |

### `status`

| Contract field | Source field | Disposition |
|---|---|---|
| `state` | `state` | RENAME (case) + **value-vocabulary gap** *(judgment call — see Open Decisions)*: source enum (`idle`, `recruiter_speaking`, `user_speaking`, `waiting_for_reply`, `generating`, `speaking`) does not correspond 1:1 to the Contract enum (`STARTING`, `READY`, `PROCESSING`, `IDLE`, `STOPPED`) |
| `message` | *(absent)* | **GAP** — only `previous` (a bare state name) is emitted, no human-readable message |
| — | `previous` | EXTRA, passed through |

### `error`

| Contract field | Source field | Disposition |
|---|---|---|
| `message` | `message` | DIRECT |
| `code` | `label` | RENAME *(judgment call — see Open Decisions: `label` is a free-form string like `"Audio"`, not a formal error code)* |
| `recoverable` | *(absent)* | **GAP** |

---

## 3. What this means for Verification Runtime

`verification.verification_runtime.VerificationRuntime` already implements
Gap Detection exactly for this situation (`_check_payload` /
`_REQUIRED_FIELDS` / `_FIELD_TYPES`). Once real events are translated by the
adapter:

- Every `reply` event from the live Runtime will have `gap_detected=True`
  (missing `provider`, `model`, `finish_reason`) — **regardless of adapter
  design**, since these three fields simply do not exist anywhere upstream
  today.
- Every `transcript` event will have `gap_detected=True` (missing
  `confidence`, `is_final`).
- Every `analysis` event will have `gap_detected=True` (missing `intent`,
  `metadata`).
- Every `latency` event will have `gap_detected=True` (missing
  `routing_ms`).
- Every `status` event will have `gap_detected=True` (missing `message`,
  and — unless the Open Decision below approves a state-vocabulary
  translation — `undefined state` for every value except `idle`/`IDLE`).
- `error` events are the only type that can be made fully gap-free by the
  adapter (once `label`→`code` and the direct `message` mapping are
  applied), since `recoverable` is the only missing field and it's boolean
  metadata with no natural source value — this remains a GAP too.

This is expected and correct: it is the honest, current state of Cloud Run
Runtime's instrumentation against the frozen Contract, not a defect in
Verification Runtime or the adapter. It becomes the central finding of the
Runtime Impact Analysis / Remaining Risks deliverables.

---

## 4. Resolved Decisions

1. **`analysis.text` → `summary`** — **Approved.** Adapter renames `text`
   to `summary`. `intent`/`metadata` remain an honest GAP (never
   fabricated).
2. **`latency.gpt_ms` → `provider_ms`** — **Approved.**
3. **`error.label` → `code`** — **Rejected.** `label` is preserved as an
   EXTRA/informational field only; `code` is left an honest GAP. The
   adapter must not rename `label` to `code`.
4. **`status.state` vocabulary** — **Pass through untranslated.** The
   adapter does not translate or case-fold the real state value; it is
   carried into the Contract's `state` field exactly as emitted (e.g.
   `"idle"`, `"recruiter_speaking"`). Verification Runtime will correctly
   report `undefined state` for every value (including `"idle"`, since
   the Contract enum is uppercase `IDLE`) — this is an accepted, honest
   signal, not an adapter defect.
5. **Identity fields** (`event_id`, `session_id`, `sequence`) — **Approved
   to generate.** One `session_id` (uuid4) per adapter instance/Runtime
   process lifetime, one `event_id` (uuid4) per event, and a monotonic
   `sequence` counter starting at 1. Purely structural envelope metadata;
   no business/verification/trust logic involved.

These decisions are final for the H4-10 Runtime Adapter implementation.
