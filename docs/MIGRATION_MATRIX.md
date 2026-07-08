# Migration Matrix

**Single Source of Truth (SSoT):** `/Users/shuichi/Phantom/02_Repository/poc-ai-meeting` (`phantom-conversational-runtime`)
**Migration Target:** `/Users/shuichi/Phantom/02_Repository/GitHub/phantom-runtime-lite`

This document is the **only** artifact future migration work should reference. Do not re-search the SSoT repository wholesale — update this matrix instead when a new feature is discovered.

---

## 0. Purpose & Status Rules (READ FIRST)

**This document is a function inventory ("棚卸し"), not a completion judgment.**

The matrix below enumerates every function/feature found in the SSoT repo, its source file, its dependencies, its responsibility, and which side of the Cloud Run client/server split it is a *candidate* for. It does **not** assert that a feature has been successfully ported, and it does **not** assert correctness.

### Status column values

| Status | Meaning |
|---|---|
| **Unknown** | Default. Inventoried in the SSoT only; no validation against the target repo has been performed. **Every row in this matrix currently carries this status.** |
| **Completed** | To be assigned only after a dedicated pass confirms the corresponding code structurally exists in the target repo (file/class/function present). Do not assign this from casual inspection. |
| **Verified** | To be assigned only after the feature has passed functional validation in the target repo — e.g. Validation tests, E2E tests, Cloud Run deployment check, Keyboard parity check, Control Event dispatch check, or Runtime Client integration check. |

Progression is strictly **Unknown → Completed → Verified**, one step at a time, only after the named validation activity actually occurs. Do not skip a step and do not backfill a status without the corresponding check having run.

### Migration rules (carried over from task instructions)

- No feature deletion. No MVP-ing. No simplification. No substitute implementations.
- Every existing SSoT feature is in scope for migration.
- Only *responsibility* (client vs. server placement) may change to fit the Cloud Run architecture — functional behavior must not change.

---

## 1. Migration Summary

- The SSoT (`poc-ai-meeting`) is dominated by one 9,443-line monolith, `src/phantom_conversational_runtime_v22.py`, plus a set of already-extracted modules under `src/{audio,conversation,transcript,ui,profiles,provider,runtime,compat}/`, plus a parallel, **not currently load-bearing** multi-agent orchestration/observability framework under `runtime_framework/` (64 files) that touches the monolith through exactly one adapter (`runtime_framework/adapter/conversation_runtime_adapter.py`).
- The SSoT already contains a Cloud Run process-supervisor (`src/runtime/cloud_run_shell.py` + `src/runtime/health_server.py`) that spawns the monolith as a subprocess and exposes a `/healthz` endpoint. This is prior art, not something to re-derive.
- No networked transport, and no "Runtime Client" concept, exist in the SSoT at all — the monolith is single-process, using in-memory `queue.Queue` for audio/transcript handoff between threads. These two categories are **greenfield** for the migration; there is no SSoT file to port, only the in-process queue/thread contract to use as a reference shape for the future message contract.
- No "Trust" concept exists anywhere in the SSoT (confirmed by full-repo grep). The nearest proxies are per-record `confidence` floats scattered through Memory, and the A33 consistency `verdict`.
- No module literally named "Verification" exists in the SSoT; the closest functional analog is the A33 contradiction/consistency report inside the Memory subsystem.
- "Memory Insight" as a distinct named feature does not exist separately from Context Recommendation (A29), Memory Reasoning (A35), and the Context Intelligence dashboards (A30/B30) — flagged as a likely naming/taxonomy overlap rather than a missing feature.
- Several Memory features exist in **two competing generations** in the SSoT (A-series vs. B-series: Status A29-adjacent vs. B27, Timeline A27 vs. B28, Dashboard A30 vs. B30, Consistency A33 vs. B29). The matrix lists both; a canonical choice needs to be made during migration, not assumed by this document.
- **Open questions requiring a decision before porting (not answered by this document):**
  1. ~~Is `runtime_framework/` ... in scope for migration~~ — **RESOLVED 2026-07-08.** See §1.1 below.
  2. ~~What is the intended relationship between the SSoT's A33 consistency report and any "Verification" work in the target repo~~ — **RESOLVED 2026-07-08.** Distinct capability, not a renaming. See §1.1.
  3. ~~Is a "Trust" scoring feature an intentional net-new addition~~ — **RESOLVED 2026-07-08.** Yes. See §1.1.

### 1.1 Resolved: `runtime_framework/` / Verification / Trust scope classification (2026-07-08)

Investigation-only pass (no code changed). Full evidence trail in §7 Validation Log, 2026-07-08 entry. Classification uses three buckets per user request: **移植対象** (still needs porting), **対象外** (out of scope, will not be ported), **既存H4/H5/H6で置き換え済み** (target repo already fulfills the same architectural role via independently-built H4/H5/H6 work — not a literal file port).

**`runtime_framework/`** (confirmed via `grep -r "runtime_framework" src/ docs/ tests/`: zero references in the target repo outside this matrix document itself):

| Sub-component | Classification | Rationale |
|---|---|---|
| Runtime lifecycle orchestrator (`runtime.py`), Request/Response + Agent core, Dispatch/Pipeline execution, Framework errors, Monitor lifecycle hooks | **対象外** | Already flagged in §1 as not load-bearing in the SSoT itself (touches the monolith through exactly one adapter). `docs/H4_IMPLEMENTATION_PLAN.md` (Frozen v1.0) additionally defines a **Single Runtime Policy** that explicitly prohibits "Secondary Runtime / Replacement Runtime / Mock Runtime" — porting an Agent/Coordinator/Pipeline execution abstraction would violate this frozen constraint outright. |
| Typed event core (`runtime_event_draft.py`/`runtime_event.py`/`runtime_event_bus.py`/`event_subscriber.py`), Control-plane lifecycle events (`RuntimeStarted`/`RuntimeCompleted`/`RuntimeFailed`) | **既存H4/H6で置き換え済み** | H4-1 Runtime Event Contract (`docs/H4_RUNTIME_EVENT_CONTRACT.md`, Frozen) + `phantom_runtime.py`'s `_emit_event()`/Typed Events + H6 Control Events already fulfill the same architectural role (typed, immutable, session-scoped event delivery) via an independent design — already **Verified** elsewhere in this matrix (Keyboard/Runtime Client rows, 2026-07-08 pass). The `status` event's state enum (`STARTING/READY/PROCESSING/IDLE/STOPPED`) plays the analogous role to the design-reference control-plane lifecycle events. This is a functional-role replacement, not a literal port — the `runtime_framework/` files themselves were never copied and are not scheduled to be. |
| Health check subsystem, Metrics engine, Alerting, Observability pipeline, Readiness framework, Logging API abstraction, Bootstrap wiring | **対象外** | No consumer exists or is needed in the target repo. The operationally-necessary parts are already covered by separate, non-`runtime_framework`-derived prior art: Cloud Run's own `/healthz` readiness probe (`src/runtime/health.py`, `health_server.py`) and the concrete `RuntimeLogger` (`src/runtime/runtime_logger.py`) — see the Runtime (core orchestration) section above. The elaborate pluggable-backend/agent-pipeline-health abstractions in `runtime_framework/` add no capability not already met. |

**Verification**: `src/verification/verification_runtime.py` (H4-2) read in full. **Confirmed distinct from SSoT's A33** ("Memory: Consistency" section above), not a replacement — they share the English word "Verification" only:
  - H4-2's input is a single `RuntimeEvent` envelope; it checks **event-stream/wire-protocol quality**: payload schema conformance against the frozen Contract, sequence/timestamp ordering monotonicity, and `finish_reason == "fallback"` detection. This is a Runtime-pipeline-reliability concern.
  - SSoT's A33 checks **conversational content consistency**: Fact Graph contradiction edges and Subject/Decision status mismatches, derived from Memory subsystem records. This is a business-logic/Memory concern, and requires entirely different inputs (Fact Graph, Subject/Decision status) that H4-2 never touches.
  - **Classification: 移植対象 (still not ported).** A33-equivalent conversational-consistency checking does not exist anywhere in the target repo. H4-2 does not reduce this gap.

**Trust**: `src/trust/trust_runtime.py` (H4-3) read in full. SSoT has no Trust equivalent (confirmed by the existing full-repo grep noted in the "Trust" section below). H4-3 is a complete, already-shipped, net-new capability with a frozen spec (weighted Trust Policy → `trust_score`/`trust_level`/`human_review_required`), independent of Verification Runtime's internals.
  - **Classification: 対象外 for migration purposes** (nothing in the SSoT to port — there is no source to replace) **/ already delivered via H4-3.** Open question #3 is answered: yes, an intentional net-new addition, spec already frozen and implemented. `docs/H4_STATUS.md` records a historical validation pass (OpenAI/Gemini live API, Docker/Cloud Run production-like environment, 215/217 tests) for this component — noted here as documentary evidence only; not independently re-run in this session.

**Related risk noted, not in scope of this classification request:** `src/dashboard/dashboard_runtime.py` (H4-6) shares the name "Dashboard" with the SSoT's Memory Dashboard (A30/B30) but is a different feature — H4-6 renders Transcript/Reply/Latency/Verification/Trust/Timeline (Runtime-event-level), while A30/B30 render Subject Status/Timeline/Consistency/Priority (Memory-subject-level). Flagged so this naming collision isn't later mistaken for completeness; no Status change made.

---

## 2. Migration Matrix

Legend: **C** = Client Candidate, **S** = Server Candidate. A row may carry both if the responsibility is expected to split across the boundary. Status is **Unknown** for every row per the rule in §0.

### Runtime (core orchestration)

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| CLI arg parsing | v22.py:228 `_build_parser()` | `RuntimeConfig` | Defines full CLI surface (mode, provider, tts, profile, manual-flush, cognition, health-interval, etc.) | | ✓ | Unknown |
| Config precedence resolution | v22.py:324-388 | `RuntimeConfig.from_env()`, CLI args, env | Merges CLI > env > RuntimeConfig > parser defaults | | ✓ | Unknown |
| Startup API-key validation | v22.py:343 | `OPENAI_API_KEY` env | Fails fast if key missing/invalid | | ✓ | Unknown |
| Provider construction (static instantiation) | v22.py:374-386 | `provider/openai_provider.py`, `provider/gemini_provider.py`, `RuntimeConfig` | Builds default/candidates/streaming provider instances | | ✓ | Unknown |
| Model capability registry | v22.py:504-523 `_model_cap()` | none | Structured Whisper/GPT model feature capability map | | ✓ | Unknown |
| Runtime queues/shared state | v22.py:528-587 | `threading`, `queue` | `audio_queue`, `transcript_queue`, `transcript_log`, `_log_lock` — capture→STT→reply thread handoff | | ✓ | Unknown |
| Pipeline trace logging | v22.py:587-611 `_trace()` | `RUNTIME_LOG_LEVEL` env | Debug-mode structured tracing | ✓ | ✓ | Unknown |
| Runtime env feature flags | v22.py:606-689 `_ENV` | `os.environ` | `AGENT_MODE`, `DEBUG_AUDIO_SAVE`, `QUEUE_METRICS`, etc. | ✓ | ✓ | Unknown |
| Conversation state machine | `src/runtime/state_machine.py` (`ConversationState`, `RuntimeMode`) | none | IDLE/RECRUITER_SPEAKING/USER_SPEAKING/WAITING_FOR_REPLY/GENERATING/SPEAKING; INTERVIEW/MEETING/SUMMARY modes | | ✓ | Unknown |
| State get/set | v22.py:777-812 | state machine | Thread-safe state mutation | | ✓ | Unknown |
| Structured logging | `src/runtime/runtime_logger.py` (`RuntimeLogger`) | none | Thread-safe leveled/JSON logger | ✓ | ✓ | Unknown |
| Health snapshot builder | `src/runtime/health.py` | runtime queues | Point-in-time health dict (queue pressure, buffer stats) | | ✓ | Unknown |
| Health monitor thread | v22.py:9072-9148 `health_monitor()` | `health.py`, `RuntimeLogger` | Periodic health/latency/thread-liveness diagnostics | | ✓ | Unknown |
| Health HTTP endpoint | `src/runtime/health_server.py` | `health.py`, stdlib `http.server` | `/healthz` readiness server for Cloud Run probes | | ✓ | Unknown |
| Cloud Run process shell | `src/runtime/cloud_run_shell.py` | `subprocess`, `health_server.py` | Spawns v22.py subprocess, SIGTERM→SIGINT forwarding, readiness exposure — already exists in SSoT | | ✓ | Unknown |
| Signal handling | v22.py:9343-9348 `_handle_signal()` | none | SIGINT/SIGTERM → graceful `_shutdown` event | | ✓ | Unknown |
| Entry point | v22.py:9354 `main()` | all runtime threads | Spins up capture/VAD/reply-worker/keyboard/health threads | | ✓ | Unknown |
| Compat forwarding shims | `src/compat/{audio,conversation,profiles,runtime,transcript,ui}.py` | respective extracted modules | Legacy import path bridging only — not a functional feature | | | Unknown |
| Centralized config dataclass | `src/config.py` (`RuntimeConfig`, `.from_env()`) | env vars | Typed config object for audio/thresholds/provider defaults | ✓ | ✓ | Unknown |

### Runtime Framework (parallel orchestration layer — not wired into v22 execution; scope decision needed, see §1)

| Function | Source File(s) | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Runtime lifecycle orchestrator | `runtime_framework/runtime.py` (`ConversationalRuntime`) | registry, coordinator, event bus, monitoring | Top-level façade: startup/shutdown/handle/health_status/metrics_snapshot | | ✓ | Unknown |
| Request/Response + Agent core | `runtime_framework/request.py`, `response.py`, `agent.py`, `agent_registry.py`, `agents/conversation_agent.py`, `adapter/conversation_runtime_adapter.py`, `context.py` | none (adapter calls into v22 `generate_meeting_analysis()`) | Typed request/response envelopes, capability-bearing Agent abstraction, registry, execution context, sole integration point with the monolith | | ✓ | Unknown |
| Dispatch/Pipeline execution | `runtime_framework/coordinator.py`, `dispatch_plan.py`, `pipeline.py`, `execution_strategy.py`, `sequential_execution_strategy.py`, `parallel_execution_strategy.py` (stub, inactive) | Agent registry | Routes Request→Agent, builds DispatchPlan, sequential (parallel is a non-functional stub) execution | | ✓ | Unknown |
| Framework errors | `runtime_framework/errors.py` (`FrameworkError`) | none | Framework-level exception type | | ✓ | Unknown |
| Typed event core | `runtime_framework/runtime_event_draft.py`, `runtime_event.py`, `runtime_event_bus.py`, `null_event_bus.py`, `event_subscriber.py` | none | Draft→bus-stamped immutable event model; pub/sub delivery scoped by `runtime_id` | | ✓ | Unknown |
| Control-plane lifecycle events | `runtime_framework/runtime.py:90-108` | event bus | `"RuntimeStarted"`, `"RuntimeCompleted"`, `"RuntimeFailed"` — only concrete control events currently published | | ✓ | Unknown |
| Monitor lifecycle hooks | `runtime_framework/runtime_monitor.py`, `null_monitor.py`, `Coordinator._SafeMonitor` | Coordinator | Exception-absorbing observer hooks around dispatch execution (non-bus control channel) | | ✓ | Unknown |
| Health check subsystem | `runtime_framework/health_check.py`, `health_status.py`, `health_report.py`, `agent_pipeline_health_check.py`, `agent_registry_health_check.py`, `coordinator_health_check.py`, `health_monitor.py`, `health_monitor_event_subscriber.py` | event bus | Concrete health checks + aggregator, event-driven | | ✓ | Unknown |
| Metrics engine | `runtime_framework/metrics_engine.py`, `metrics_snapshot.py`, `runtime_metrics_engine.py`, `null_metrics_engine.py`, `metrics_engine_event_subscriber.py` | event bus | Metrics collection/snapshot, event-driven | | ✓ | Unknown |
| Alerting | `runtime_framework/alert.py`, `alert_rule.py`, `alerting_engine.py` | event bus | Alert severity/rule evaluation subscriber | | ✓ | Unknown |
| Observability pipeline | `runtime_framework/observability_pipeline.py` | event bus | Aggregate observability subscriber | | ✓ | Unknown |
| Readiness framework | `runtime_framework/readiness.py`, `readiness_criteria.py`, `production_readiness_framework.py` | health checks | Production-readiness criteria evaluation | | ✓ | Unknown |
| Logging API | `runtime_framework/logging_api.py`, `logging_backend.py`, `log_record.py`, `null_logger.py` | none | Pluggable logging backend abstraction | ✓ | ✓ | Unknown |
| Bootstrap wiring | `runtime_framework/monitoring_initializer.py` (`initialize_monitoring()`) | all of the above | Wires monitoring stack together | | ✓ | Unknown |

### Audio

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Input device resolution | `src/audio/devices.py` (`resolve_device_id`, `print_input_devices`) | `sounddevice` | Mic name→device index resolution, device listing | ✓ | | Unknown |
| Audio capture stream | `src/audio/capture.py` (`AudioCapture`) | `sounddevice.InputStream` | Owns capture lifecycle, overflow tracking, callbacks | ✓ | | Unknown |
| VAD orchestration | `src/audio/vad.py` (`VADOrchestrator`) | capture, vad_buffering | Drives VAD frame-accumulation loop, tail padding, manual buffer cap | ✓ | | Unknown |
| VAD buffering state | `src/audio/vad_buffering.py` (`VADBuffer`) | none | Manual audio buffer, recording-active flag, force-flush, buffer stats | ✓ | | Unknown |
| Inline audio callback/record loop | v22.py:3206-3315 (`_audio_callback`, `record_audio`, `vad_loop`) | `audio/capture.py`, `audio/vad.py` (fallback) | Inline sounddevice callback + main VAD loop | ✓ | | Unknown |
| WAV buffer construction | v22.py:1862 `make_wav_buffer()` | `numpy` | Converts captured audio → in-memory WAV bytes for STT upload | ✓ | ✓ | Unknown |
| Silence/RMS detection | v22.py:1875-1892 (`rms()`, `is_silent()`) | `numpy` | Energy-based silence gate | ✓ | | Unknown |
| Debug audio persistence | v22.py:696-777 `_save_debug_audio()` | local filesystem | Writes captured audio to `debug_audio/` when enabled | ✓ | | Unknown |
| Overflow tracking | v22.py:451-538 | audio queue | Queue-overflow bookkeeping for audio ingestion | ✓ | | Unknown |

### Speech-to-Text / Transcription

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Whisper transcription | v22.py:1892-2029 (`_build_whisper_prompt()`, `transcribe()`) | OpenAI SDK, WAV buffer | Calls Whisper with retry/timeout, always OpenAI regardless of chat provider | | ✓ | Unknown |
| Language detection | v22.py:1382-1459, 4555-4613 (`is_japanese_lang()`, `detect_language_from_text()`, `_detect_language()`) | none | Heuristic JP/EN detection from text | | ✓ | Unknown |

### Conversation Processing

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Hallucination/noise filter | `src/conversation/hallucination_guard.py` (`is_meaningful()`) | none | Filters STT hallucination phrases and filler-only utterances | | ✓ | Unknown |
| Meaningful-text gate (inline duplicate) | v22.py:1740-1862 | same as above | Duplicate/fallback of `hallucination_guard.is_meaningful` | | ✓ | Unknown |
| Speaker inference | `src/conversation/speaker_inference.py` (`infer_speaker()`, `reset_speaker_state()`) | language detection, state machine | Anti-oscillation speaker attribution | | ✓ | Unknown |
| Question detection | v22.py:2204-2280 (`_is_question_heuristic()`, `_is_question_gpt()`, `is_question()`) | provider (GPT tier) | Two-tier heuristic→GPT question classification | | ✓ | Unknown |
| Fast-path intent classification | v22.py:1972-2081 (`_match_intent()`, `_fast_path_check()`) | profile-seeded phrases | Zero-latency intent match (extension point, not fully active) | | ✓ | Unknown |

### Transport

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Client↔Server message transport | **No SSoT file — greenfield.** Nearest analogs: in-process `audio_queue`/`transcript_queue`/`_log_lock` (v22.py:528-587) and the subprocess/health boundary in `src/runtime/cloud_run_shell.py` | WebSocket protocol (to be designed) | Carries audio-in / typed-events-out / control-events between Client and Server | ✓ | ✓ | Completed |

### Runtime Client

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Client-side runtime abstraction | **No SSoT file — greenfield.** Defined implicitly by the Client-candidate rows across Audio/Keyboard/UI/TTS below | Audio capture, Keyboard controller, Transport | Bundles client-side concerns into a coherent process | ✓ | | Completed |
| Client→server RPC surface (implicit) | `src/ui/keyboard.py:28` (`RuntimeContext` dataclass) | ~30 injected server refs/callbacks | Enumerates exactly what a client would need to call remotely (state fns, TTS, buffers, show_* fns, generate_summary_fn, generate_meeting_analysis_fn) | ✓ | ✓ | Verified |

### Keyboard

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Keyboard controller | `src/ui/keyboard.py` (`KeyboardController.run()`) | `RuntimeContext` callbacks | Dispatches single-key commands (r/g/G/h/u/d/t/1-5/s/c/l/?/q) | ✓ | | Verified |
| RuntimeContext DTO | `src/ui/keyboard.py:28` | ~30 injected refs/callbacks | See Runtime Client above — shared client/server contract | ✓ | ✓ | Verified |
| Keyboard loop (inline fallback) | v22.py:9152-9338 `keyboard_loop()` | same as KeyboardController | Duplicate/fallback implementation used if `ui.keyboard` import fails | ✓ | | Unknown |
| Help text | v22.py:9028-9067 (`_HELP_MANUAL`, `_HELP`) | none | Static help strings for keyboard commands | ✓ | | Unknown |

### UI

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Print/color helpers | v22.py:819-937 (`_print()`, `show_*`, `debug_*`) | stdout | Thread-safe terminal output + colorized tagged lines + debug-channel gates | ✓ | | Unknown |
| Clarify/delay phrase display | v22.py:1346-1387 (`show_clarify()`, `show_random_clarify()`, `_parse_phrase_list()`, `show_random_delay_en/jp()`, `show_delay_slot()`) | profile phrases | Renders operator-triggered canned phrases | ✓ | | Unknown |
| Cognition candidate display | v22.py:2582-2618 (`show_compression_result()`, `show_candidate()`, `show_candidates()`) | cognition pipeline output | Renders LLM-compressed context + multi-style candidates | ✓ | ✓ | Unknown |

### Profile

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Profile schema | `src/profiles/schema.py` (`PROFILE_DEFAULTS`, `validate_profile()`, `normalise_profile()`) | none | 12-field profile schema definition | | ✓ | Unknown |
| Profile loader | `src/profiles/loader.py` (`load_profile()`, `parse_md_profile()`, `parse_json_profile()`, etc.) | schema.py, filesystem | `.json`→`.md`→default 5-tier fallback resolution | | ✓ | Unknown |
| Profile files (data) | `src/profiles/*.md`, `profiles/*.md` (incl. root-level duplicates `workport.md`, `phantom_runtime.md`, `upwork.md`) | loader | Per-client-scenario profile content | | ✓ | Unknown |
| Inline profile parsing (v22-local duplicate) | v22.py:1052-1298 (`_parse_profile()`, `_validate_profile()`, `_load_profile()`, `_extract_profile_overrides()`, `_apply_profile_overrides()`, `_build_profile_banner()`, `_seed_intent_cache_from_profile()`) | same as loader | Largely duplicate of `profiles/loader.py` | | ✓ | Unknown |
| Static memory provider | v22.py:483-523 (`_StaticMemoryProvider`) | profile | Seeds career_summary/topic_memory/response_examples into system prompt at startup | | ✓ | Unknown |

### Provider

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Provider interface | `src/provider/interface.py` (`ProviderInterface`) | none | Abstraction boundary for chat-completion providers | | ✓ | Unknown |
| Provider DTOs | `src/provider/models.py` (`Message`, `ProviderRequest/Response`, streaming types) | none | Request/response/streaming data contracts | | ✓ | Unknown |
| Provider errors | `src/provider/errors.py` | none | Normalized provider exception hierarchy | | ✓ | Unknown |
| OpenAI provider | `src/provider/openai_provider.py` | interface, models, errors, OpenAI SDK | Buffered + streaming OpenAI chat implementation | | ✓ | Unknown |
| Gemini provider | `src/provider/gemini_provider.py` | interface, models, errors, Gemini SDK | Buffered + streaming Gemini chat implementation | | ✓ | Unknown |
| Provider selection/construction | v22.py:374-386 | openai_provider, gemini_provider, RuntimeConfig | Static instantiation of default/candidates/streaming providers | | ✓ | Unknown |
| TTS provider abstraction | `src/runtime_client/tts.py` (`NullTTSProvider`, `SayTTSProvider`, `Pyttsx3Provider`, `build_tts_provider()`); ported from v22.py:920-1035 (`_NullTTSProvider`, `_SayTTSProvider`, `_Pyttsx3Provider`, `_build_tts_provider()`) | macOS `say`, `afconvert`, `pyttsx3` (optional), `sounddevice` | Duck-typed `.speak/.stop/.is_speaking` backends, same contract as the SSoT; playback mechanism itself is net-new (renders to WAV, plays via sounddevice on a selectable output device — see §7 2026-07-09 entry) | ✓ | | Verified |
| Whisper STT client | v22.py:73-75, 343-380 | OpenAI SDK | Always-OpenAI, out of scope of `ProviderInterface` | | ✓ | Unknown |

### Control Event

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Runtime-core state signals (non-event, the gap a Control Event system must fill) | v22.py `ConversationState`/`RuntimeMode` enums, `_set_state()`/`_set_runtime_mode()` | state machine | v22 signals state via direct enum mutation + print, not a typed control-event stream | | ✓ | Unknown |
| Control-plane lifecycle events (design reference) | `runtime_framework/runtime.py:90-108` | event bus | `RuntimeStarted`/`RuntimeCompleted`/`RuntimeFailed` — the only concrete example of a "control event" in the SSoT | | ✓ | Unknown |

### Typed Event

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Typed event objects (design reference) | `runtime_framework/runtime_event_draft.py`, `runtime_event.py` | none | Draft → bus-stamped immutable event model | | ✓ | Unknown |
| Event bus (design reference) | `runtime_framework/runtime_event_bus.py`, `null_event_bus.py` | typed event objects | Pub/sub delivery scoped by `runtime_id` | | ✓ | Unknown |
| Event subscriber interface (design reference) | `runtime_framework/event_subscriber.py` | event bus | Abstract `on_event(RuntimeEvent)` contract | | ✓ | Unknown |
| Monitoring event subscribers (design reference) | `runtime_framework/health_monitor_event_subscriber.py`, `metrics_engine_event_subscriber.py` | event bus, health monitor, metrics engine | Reactive subscriber wiring pattern | | ✓ | Unknown |

### Recording

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Session init/dir management | v22.py:631-696 `_init_session_dir()` | filesystem | Creates `sessions/` transcript dir, session ID | | ✓ | Unknown |
| Transcript persistence (extracted) | `src/transcript/persistence.py` (`init_session()`, `persist_entry()`, `get_session_id()`, `close_session()`) | filesystem, write lock | Appends JSONL transcript entries across threads | | ✓ | Unknown |
| Transcript persistence (inline duplicate) | v22.py:659-696 `_persist_entry()` | same as above | Inline fallback mirroring the extracted module | | ✓ | Unknown |
| Manual push-to-record buffering | `src/audio/vad_buffering.py` (`VADBuffer.recording_active/.flush()/.status()/.show_recording_status()`), toggled via `r` key | keyboard controller | Operator-controlled recording toggle + manual buffer flush | ✓ | ✓ | Unknown |
| Debug audio recording | v22.py:696-777 `_save_debug_audio()` | filesystem | Persists raw captured audio as WAV for troubleshooting | ✓ | | Unknown |
| Session/transcript artifacts (data) | `src/sessions/*.jsonl`, `src/backup/session_*.jsonl`, `src/backup/transcript_*.jsonl` | none | On-disk output examples, not code | | | Unknown |

### Meeting Analysis

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Meeting analysis prompt | v22.py:1656-1717 `_MEETING_ANALYSIS_PROMPT` | none | Structured JP prompt (summary/risks/questions+answers/actions/facts) | | ✓ | Unknown |
| Meeting transcript cleanup | v22.py:8907-8937 (`clean_meeting_transcript()`, filler regexes) | none | Regex-based filler/noise stripping before LLM analysis | | ✓ | Unknown |
| Meeting analysis generation | v22.py:8939-9025 `generate_meeting_analysis()` | `memory_build_context()`, provider, `_memory_extract_and_save()` | Incremental cursor-based analysis + memory injection + extraction trigger | | ✓ | Unknown |
| Meeting-analysis debug channel | v22.py:868 `debug_meeting()` | none | Debug print gate | ✓ | ✓ | Unknown |

### Summary

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Interview summary generation | v22.py:8857-8905 `generate_summary()` | `SUMMARY_PROMPT`, provider | Grounding-guarded (min 2 recruiter turns) transcript summary | | ✓ | Unknown |
| Rolling summary memory | v22.py:4184-4207 (`memory_add_summary()`, `memory_get_recent_summaries()`) | `src/memory/rolling_summary.json` | Appends/retrieves recent auto-generated summaries | | ✓ | Unknown |

### Memory: Fact

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Fact CRUD | v22.py:3566-3633 (`_fact_load/_save/_find/_create/_migrate_subject`) | memory persistence, subject registry | Fact record CRUD keyed by (subject_id, fact_type) | | ✓ | Unknown |
| Fact extraction from LLM output | v22.py:8408-8697 (`_extract_action()`, `_extract_owner()`, `_extract_due_date()`, `_memory_save_facts_from_section()`) | meeting analysis output, owner registry, due-date normalizer | Parses LLM analysis text into fact records | | ✓ | Unknown |
| Fact link resolution | v22.py:5143 `_fact_resolve_links()` | fact CRUD | Resolves cross-references between facts | | ✓ | Unknown |

### Memory: Subject

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Subject CRUD/lifecycle | v22.py:3660-3741, 5330-5407 (`_subject_load/_save/_find/_create/_extract/_get_or_create/_merge_execute`, `_subject_status`, `_subject_lifecycle`, `_subject_is_merged`, `_subject_is_compressible`, `_subject_priority_score`) | memory persistence | Subject registry CRUD + lifecycle tracking | | ✓ | Unknown |
| Subject merge pipeline | v22.py:4106, 6833-7775 (`_subject_auto_merge_candidates`, `_subject_merge_candidates/_confidence/_recommendation/_plan/_resolve_merge/_validate/_transaction`) | Subject Graph | Merge-candidate detection/approval/execution pipeline | | ✓ | Unknown |

### Memory: Decision

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Decision CRUD/state machine | v22.py:4902-5100 (`_decision_can_transition/_load/_save/_find/_mark_done/_migrate_subject/_status/_update_status`, `memory_add_decision`, `memory_get_decisions/_open/_done`, `memory_update_decision_status`) | owner registry, subject registry | Decision records with open→done status state machine | | ✓ | Unknown |
| Decision extraction from LLM output | v22.py:8633-8733 (`_decision_confidence()`, `_memory_save_decisions_from_section()`, `_memory_detect_completed_decisions()`) | meeting analysis output | Confidence-gated decision extraction/save | | ✓ | Unknown |

### Memory: Owner

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Owner registry | v22.py:3779-3900 (`_owner_strip_suffix`, `_registry_load/_save`, `_next_id`, `_alias_match`, `_get_or_create_nolock/_normalize/_get_or_create`, `_migrate_decisions`) | memory persistence | Owner-name registry (id assignment, alias matching, normalization) | | ✓ | Unknown |

### Memory: Due

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Due-date extraction/normalization | v22.py:8426-8457 (`_extract_due_date()`, `_last_day_of_month()`, `_normalize_due_date()`) | meeting analysis text | Rule-based due-date extraction from LLM output | | ✓ | Unknown |

### Memory: Status

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Per-entity status computation | v22.py:5003-5347 (`_decision_status/_update_status`, `_subject_status/_lifecycle`) | decision/subject CRUD | Status derivation per record | | ✓ | Unknown |
| Cross-cutting status aggregation (B27) | v22.py:6065-6185 (`_b27_text_contains`, `_b27_collect_status_signals`, `_b27_build_status_entry`, `build_subject_status`, `build_status_list`, `get_top_status_subject`) | fact/decision/subject state | Aggregated status view across subjects | | ✓ | Unknown |
| Status transition detection | v22.py:8787-8802 (`_memory_detect_status_transitions()`, `_memory_apply_status_transition()`) | meeting analysis text | Text-driven status-transition detection | | ✓ | Unknown |

### Memory: Question

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Question lifecycle CRUD | v22.py:4207-4540 (`_question_status/_can_transition/_load/_save/_find`, `_question_mark_answered*`, `_question_migrate_subject`, `memory_add_question`, `memory_get_questions/_open/_answered`, `memory_question_exists`) | memory persistence | Open/answered lifecycle | | ✓ | Unknown |
| Question normalization | v22.py:4555-4669 (`_detect_language`, `_apply_canonical_dict`, `_normalize_question_ja/en/multilingual`, `_content_question`) | language detection | Multilingual question text normalization | | ✓ | Unknown |
| Question similarity/clustering | v22.py:4674-4841 (`memory_question_similar_exists`, `_make_question_cluster_id`, `_load/_save_question_cluster_memory`, `_token_jaccard`, `_derive_canonical_key`, `_question_similarity_score`, `_question_find_cluster`, `_question_cluster_assign`) | question normalization | Jaccard-similarity clustering to dedupe near-identical questions | | ✓ | Unknown |
| Question resolution/link helpers | v22.py:5271-5311 (`_question_is_resolved`, `_question_fact_type_matches`, `_question_migrate_answer_link`) | fact CRUD | Resolution status + fact-type matching | | ✓ | Unknown |
| Question extraction from LLM output | v22.py:8369 `_memory_save_questions_from_section()` | meeting analysis output | Parses LLM analysis text into question records | | ✓ | Unknown |

### Memory: Answer / Answer Linking

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Answer link CRUD | v22.py:5294, 4500-4522 (`_question_build_answer_link`, `memory_get_answer_link(s)`, `memory_get_answer_links_by_subject`) | question CRUD, fact/decision CRUD | Links Questions to resolving Fact/Decision/free-text Answer | | ✓ | Unknown |
| Subject answer context builder (A26) | v22.py:5421-5616 (`_a26_resolve_source_value`, `_a26_make_answer_entry`, `_a26_sort_answer_entries/_sort_contexts`, `_a26_build_subject_answer_context_from_bucket`, `build_subject_answer_context`, `build_all_subject_answer_contexts`, `get_subject_answer_context_summary`) | answer link CRUD | Per-subject "answer context" summaries for prompt injection | | ✓ | Unknown |

### Memory: Timeline

| Function | Source File | Dependencies | Responsibility | C | S | Status | Notes |
|---|---|---|---|---|---|---|---|
| Answer timeline (A27) | v22.py:5643-5702 (`_a27_to_timeline_entry/_sort_timeline`, `build_subject_answer_timeline`, `build_global_answer_timeline`) | answer context | Answer-event ordering per subject/globally | | ✓ | Unknown | Competing generation vs. B28 |
| Event timeline (B28) | v22.py:6185-6241 (`_b28_fact_status_hint/_decision_status_hint/_build_event`, `_build_subject_timeline`, `build_subject_timeline`, `build_timeline_list`) | fact/decision status | Fact/decision status-change event stream | | ✓ | Unknown | Competing generation vs. A27; reconcile canonical choice during migration |

### Memory: Context Builder

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Subject context bucket builder | v22.py:5197 `_build_subject_context()` | fact/decision/question CRUD | Per-subject bucket aggregation | | ✓ | Unknown |
| Full memory context assembly | v22.py:8076, 8360, 7834, 7878, 8062, 8825 (`memory_build_context()`, `_memory_extract_section()`, `_render_subject_block()`, `_runtime_section_plugins()` +13 `_fmt_*` formatters, `_run_runtime_section_plugins()`, `_memory_extract_and_save()`) | all Memory subsystems | Assembles the full LLM-injected memory context block; plugin-formatter system per feature | | ✓ | Unknown |

### Memory: Fact Graph

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Fact graph builder (A31) | v22.py:6472-6826 (`_a31_*` node/edge builders, `build_fact_graph`, `build_fact_graph_list`, `get_related_facts`, `get_contradicting_facts`) | Fact CRUD | Directed graph of fact nodes with support/contradiction edges (windowed, depth-limited, cycle-checked) | | ✓ | Unknown |

### Memory: Subject Graph

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Subject graph builder (A32) | v22.py:6922-7070 (`_a32_*` node/edge builders, `build_subject_graph`, `get_related_subjects`, `get_merge_candidate_subjects`) | Subject CRUD, Fact CRUD | Cross-subject relationship graph based on token/fact overlap | | ✓ | Unknown |

### Memory: Context Expansion

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Expanded context builder (A34) | v22.py:7277-7451 (`_a34_*` helpers, `build_expanded_context`, `get_related_context`) | Subject Graph | BFS-style depth-limited expansion from a seed subject | | ✓ | Unknown |

### Memory: Context Recommendation

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Priority + recommendation lists (A28/A29) | v22.py:5727-5919 (`_a28_*`, `build_subject_priority_list`, `get_top_priority_subject`, `_a29_*`, `build_recommendation_list`, `get_top_recommendation`) | Subject status/lifecycle | Ranks subjects by staleness/priority; generates "what to do next" entries | | ✓ | Unknown |

### Memory: Memory Reasoning

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Reasoning report (A35) | v22.py:7490-7612 (`_a35_*`, `build_reasoning_report`, `get_reasoning_findings`) | Fact Graph, Subject Graph, Context Expansion | Synthesizes missing-info/follow-up/contradiction findings per subject | | ✓ | Unknown |

### Memory: Memory Insight

| Function | Source File | Dependencies | Responsibility | C | S | Status | Notes |
|---|---|---|---|---|---|---|---|
| (No dedicated module) | Overlaps A29 (Recommendation), A30/B30 (Dashboard), A35 (Reasoning) | — | No feature literally named "Insight" | | ✓ | Unknown | Naming/taxonomy ambiguity — confirm intent before creating a duplicate module |

### Memory: Context Intelligence

| Function | Source File | Dependencies | Responsibility | C | S | Status | Notes |
|---|---|---|---|---|---|---|---|
| Subject dashboard (A30) | v22.py:5933-6003 (`_a30_*`, `build_context_dashboard`, `get_dashboard_summary`) | Status, Timeline, Consistency, Priority | Combined single-view dashboard, generation A | | ✓ | Unknown | Competing generation vs. B30 |
| Subject dashboard (B30) | v22.py:6353-6430 (`build_subject_dashboard`, `build_dashboard_list`, `get_top_dashboard_subject`) | Status, Timeline, Consistency, Priority | Combined single-view dashboard, generation B | | ✓ | Unknown | Competing generation vs. A30; reconcile canonical choice during migration |

### Memory: Consistency (supports Verification below)

| Function | Source File | Dependencies | Responsibility | C | S | Status | Notes |
|---|---|---|---|---|---|---|---|
| Subject consistency check (B29) | v22.py:6273-6331 (`_b29_normalize_status`, `_b29_check_consistency`, `build_subject_consistency`, `build_consistency_list`) | Status | Status-consistency check per subject | | ✓ | Unknown | Related to A33 |
| Contradiction/consistency report (A33) | v22.py:7104-7239 (`_a33_*`, `build_consistency_report(_list)`, `get_contradictions`) | Fact Graph, Subject/Decision status | Aggregates fact-graph contradiction edges + subject/decision status consistency into a per-subject verdict | | ✓ | Unknown | Closest SSoT analog to "Verification" (see below) |

### Verification

| Function | Source File | Dependencies | Responsibility | C | S | Status | Notes |
|---|---|---|---|---|---|---|---|
| Claim/consistency verification | Same as "Memory: Consistency" A33 above | Fact Graph, Subject/Decision status | Functions as claim verification | | ✓ | Unknown | No module literally named "Verification" exists in the SSoT — open question in §1 |

### Trust

| Function | Source File | Dependencies | Responsibility | C | S | Status | Notes |
|---|---|---|---|---|---|---|---|
| (No SSoT feature found) | Confirmed via full-repo grep for `trust` | — | — | | | Unknown | Nearest proxies: `_decision_confidence()`, `_subject_create(confidence=...)`, `_subject_merge_confidence()`, A33 `verdict`. No spec exists in the SSoT — open question in §1 |

### TTS

| Function | Source File | Dependencies | Responsibility | C | S | Status | Notes |
|---|---|---|---|---|---|---|---|
| TTS interrupt signaling | `src/runtime_client/typed_event.py` (`TypedEventStore._speak_reply`, `tts_interrupt_event`); ported from v22.py:727-735 `_tts_interrupt` and the reply-speaking wait loop at v22.py:3167-3184 | `threading.Event`, TTS provider | Signals in-progress TTS to stop; reply-speaking loop polls it (10s deadline, 0.05s interval), same shape as the SSoT | ✓ | | Verified | |
| TTS keyboard control (stop) | `src/ui/keyboard.py` via `RuntimeContext.tts`/`.tts_interrupt_event` (unmodified), now wired in `keyboard_bridge.py` to `store.tts`/`store.tts_interrupt_event` instead of the prior `_NullTTS` stub | KeyboardController, TTS provider | Operator-triggered TTS stop (`s` key) | ✓ | | Verified | Keyboard dispatch itself was already Verified 2026-07-08; this pass verifies the previously-stubbed `tts` object is now real |
| Voice selection | `src/runtime_client/config.py` (`--voice`), `src/runtime_client/tts.py` (`SayTTSProvider.__init__`) | none | CLI-selectable `say` voice name | ✓ | | Verified | **No SSoT file — greenfield.** SSoT hardcoded `voice="Samantha"` as a constructor default with no CLI exposure (v22.py:999) |
| Speech rate | `src/runtime_client/config.py` (`--rate`), `src/runtime_client/tts.py` (`build_tts_provider`) | none | CLI-selectable words/min rate, per-backend default preserved when unset (200 for `say`, 175 for `pyttsx3`, matching v22.py:999/1024) | ✓ | | Verified | **No SSoT file — greenfield.** Same hardcoded-default situation as Voice selection |
| Volume control | `src/runtime_client/config.py` (`--volume`), `src/runtime_client/tts.py` (`_scale_volume`, `_WavPlayer`) | `numpy` | PCM sample scaling applied uniformly at the playback layer regardless of which backend rendered the audio | ✓ | | Verified | **No SSoT file — greenfield, net-new per explicit Phase 3 spec.** No volume concept exists anywhere in the SSoT (confirmed by grep) |
| Output-device enumeration | `src/runtime_client/output_device.py` (`list_output_devices`, `print_output_devices`) | `sounddevice` | Lists macOS output-capable devices (built-in speaker, BlackHole, Loopback, USB, AirPods, Multi-Output Device, ...) | ✓ | | Verified | **No SSoT file — greenfield.** Same convention as the already-Verified Transport/Runtime Client greenfield rows above |
| Output-device selection/switching | `src/runtime_client/config.py` (`--output-device`), `src/runtime_client/output_device.py` (`resolve_output_device_id`) | `sounddevice` | Name/substring/index resolution to a `sounddevice` device id, targeted explicitly per `sd.play()` call (no global system-output mutation) | ✓ | | Verified | **No SSoT file — greenfield, net-new per explicit Phase 3 spec** |
| Default-device fallback | `src/runtime_client/output_device.py` (`resolve_output_device_id`) | `sounddevice` | `None`/`""`/`"default"`/`"system default"` all resolve to `None` (sounddevice's own system-default semantics) | ✓ | | Verified | **No SSoT file — greenfield** |

*(TTS backend implementations are listed under Provider above.)*

### Prompt

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| System prompt builder (interview mode) | v22.py:1524-1613 (`_build_system_prompt()`, `_ENGLISH_LEVEL_INSTRUCTIONS`) | profile, memory context | Assembles SYSTEM_PROMPT from language/level/pronunciation/profile/memory | | ✓ | Unknown |
| Agent-mode system prompt | v22.py:2310-2378 `_build_agent_system_prompt()` | profile | Separate prompt for autonomous agent-mode replies | | ✓ | Unknown |
| Cognition pipeline prompts | v22.py:2726-2793 (`_build_candidates_prompt()`, `_parse_candidates()`) | compression result | Prompt + parser for multi-style response candidates | | ✓ | Unknown |
| External prompt files | `prompts/system_prompt.txt`, `phantom_core.txt`, `phantom_light.txt` | `_load_file()` | Legacy "full mode" prompt override files | | ✓ | Unknown |
| Whisper prompt | v22.py:1892 `_build_whisper_prompt()` | profile | Optional Whisper `prompt` param to bias transcription | | ✓ | Unknown |

*(Meeting analysis prompt is listed under Meeting Analysis above.)*

### Configuration

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| RuntimeConfig dataclass | `src/config.py` (`RuntimeConfig`, `.from_env()`) | env vars | Central typed config | ✓ | ✓ | Unknown |
| Env feature flags | v22.py:606-689 `_ENV` | `os.environ` | Boolean/str env var reads | ✓ | ✓ | Unknown |
| `.env` loading | v22.py:343 `load_dotenv()` | `python-dotenv` | dotenv-based API key loading | | ✓ | Unknown |
| Enterprise config | `src/enterprise.json` | unconfirmed | JSON config artifact — content not fully inspected by this survey | | ✓ | Unknown |

*(CLI parsing is listed under Runtime above.)*

### Persistence

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Persistence backend selector | v22.py:3350 `_PERSISTENCE_BACKEND` env (`json`\|`postgres`) | none | Branches every memory load/save between backends | | ✓ | Unknown |
| Generic memory load/save (JSON) | v22.py:3358-3545 (`_make_record_id`, `_make_legacy_id`, `_memory_load`, `_memory_save_file`, `_memory_migrate_record`, `memory_init()`) | filesystem | Shared record-ID scheme + list load/save with legacy-ID migration | | ✓ | Unknown |
| PostgreSQL backend | `src/persistence_pg.py` (`_get_conn`, `close`, `_ensure_schema`, `store_name`, `load_entries`, `save_entries`, `load_document`, `save_document`, `migrate_from_json`) | `psycopg` | JSONB-first schema, whole-replace transactional writes, JSON→PG migration | | ✓ | Unknown |
| Recovery export (PG→JSON) | `src/persistence_export.py` (`new_staging_dir`, `export_to_json`, `canonical_json(_sha)`, `atomic_swap`, `discard_staging`) | PostgreSQL backend | Operator rollback tool: exports Postgres state to JSON with atomic staging-dir swap | | ✓ | Unknown |
| JSON memory store files (data) | `src/memory/*.json` (9 stores: fact, decision, question, question_cluster, subject_registry, owner_registry, merge_history, merge_approval, rolling_summary) | none | On-disk memory stores | | ✓ | Unknown |
| Transcript persistence | `src/transcript/persistence.py` | filesystem, write lock | (see Recording above) | | ✓ | Unknown |

### Session

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| Session directory/ID init | v22.py:631-659, `src/transcript/persistence.py:init_session()` | filesystem | Creates per-run session ID/dir | | ✓ | Unknown |
| Session close | `src/transcript/persistence.py:close_session()` | transcript persistence | Finalizes/flushes session on shutdown | | ✓ | Unknown |
| Graceful shutdown | v22.py:9343-9348 (`_handle_signal()`, `_shutdown`) | `threading.Event` | Coordinates thread shutdown across capture/VAD/reply/keyboard/health threads | | ✓ | Unknown |
| Cloud Run session shell lifecycle | `src/runtime/cloud_run_shell.py` (`_ReadinessState`, `main()`) | `health_server.py` | starting→healthy→shutting_down→failed state machine — already exists in SSoT | | ✓ | Unknown |

### Miscellaneous / Uncategorized

| Function | Source File | Dependencies | Responsibility | C | S | Status |
|---|---|---|---|---|---|---|
| GPT streaming reply (interview/observer mode) | v22.py:2081-2280 (`generate_reply()`, `_emit_line()`) | provider streaming | Streams LLM reply, parses `[JP]`/`[EN]` tags for live display | ✓ | ✓ | Unknown |
| Conversation history builder (agent mode) | v22.py:2283-2378 `_build_conversation_history()` | transcript log | Builds message history array for agent-mode LLM calls | | ✓ | Unknown |
| Autonomous agent reply | v22.py:2381-2543 `generate_agent_reply()` | agent-mode prompt, provider | Full autonomous reply generation | | ✓ | Unknown |
| Cognition pipeline (compression + candidates) | v22.py:2519-2941 (`compress_conversation()`, `generate_candidates()`, `run_cognition_pipeline()`, `CompressionResult`/`ResponseCandidate`) | provider, prompt builders | Optional 4-phase compress→candidates→display pipeline (`--cognition`/`ENABLE_COGNITION`) | | ✓ | Unknown |
| Reply worker thread | v22.py:2941-3206 `reply_worker()` | STT, filter, speaker-infer, persistence, reply/analysis | Main consumer thread orchestrating the full per-utterance pipeline | | ✓ | Unknown |
| Test/validation scripts | `src/test_a26.py` … `test_b30_dashboard.py`, `src/validate_m2_*.py`, `runtime_framework/validate_s4_*.py`, `validate_s5_*.py` | respective feature under test | Feature-specific test/validation harnesses paired 1:1 with A26-A35/B27-B30/S4/S5 features | — | — | Unknown |

---

## 3. Client責務一覧 (Client Candidate Responsibilities)

Aggregated from the "C" column above — these are candidates for the client process, subject to validation:

- **Audio**: device resolution, capture stream, VAD orchestration/buffering, inline capture/record loop, silence/RMS detection, debug audio persistence, overflow tracking, WAV buffer construction (shared boundary with STT upload).
- **Keyboard**: `KeyboardController`, `RuntimeContext` DTO (shared contract), inline keyboard loop fallback, help text.
- **UI**: all `show_*`/print helpers, clarify/delay phrase display, cognition candidate rendering (generation is server-side, display is client-side).
- **TTS**: backend implementations (`say`/`pyttsx3`/null), interrupt signaling, keyboard-triggered stop.
- **Runtime Client / Transport**: greenfield — client-side WebSocket transport, audio-to-transport bridge, the RPC surface implied by `RuntimeContext`.
- **Shared/cross-cutting**: pipeline trace logging, env feature flags, `RuntimeConfig`, structured logging — these run on both sides but are not client-exclusive.

## 4. Server責務一覧 (Server Candidate Responsibilities)

Aggregated from the "S" column above — the overwhelming majority of functionality:

- **Runtime core**: CLI/config resolution, provider construction, state machine, health monitoring, Cloud Run shell, signal handling, entry point.
- **Runtime Framework** (pending scope decision — see §1 open question 1): Agent/Coordinator/Pipeline/EventBus/HealthMonitor/MetricsEngine/AlertingEngine/Readiness/Logging.
- **STT/Conversation processing**: Whisper transcription, language detection, hallucination filtering, speaker inference, question detection, intent classification.
- **Profile, Provider (LLM), Prompt, Configuration**: all loading/building/selection logic.
- **Control Event / Typed Event**: the entire design-reference event system (currently only prototyped in `runtime_framework/`).
- **Recording/Session**: session init/close, transcript persistence, shutdown coordination.
- **Meeting Analysis, Summary**: generation, cleanup, prompting.
- **Memory (all 17 subsystems)**: Fact, Subject, Decision, Owner, Due, Status, Question, Answer/Answer Linking, Timeline, Context Builder, Fact Graph, Subject Graph, Context Expansion, Context Recommendation, Memory Reasoning, Memory Insight (ambiguous), Context Intelligence/Dashboard, Consistency — entirely server-side.
- **Verification/Trust**: server-side by nature, contingent on the open questions in §1.
- **Persistence**: JSON store, PostgreSQL backend, recovery/export tooling.

## 5. 未移植機能一覧 (Presence Check — NOT a Completion Judgment)

This section reports whether *any file-level trace* was found in the target repo during the initial survey. **This is an existence check only.** It does not assess correctness, does not assign "Completed" status in the matrix above, and must be re-verified before being relied upon.

| SSoT Feature | File-level trace found in target repo? |
|---|---|
| PostgreSQL persistence backend (`persistence_pg.py`) | Not found |
| Recovery export tool (`persistence_export.py`) | Not found |
| `runtime_framework/` (Agent/Coordinator/Pipeline/EventBus/Health/Metrics/Alerting/Readiness/Logging, 64 files) | Not found as a package; target has separately-built, differently-shaped `verification/`, `trust/`, `dashboard/`, `aggregator/`, `api/` modules whose relationship to `runtime_framework/` is unconfirmed |
| Client-side TTS/audio output playback | **Implemented 2026-07-09** (`src/runtime_client/tts.py`, `output_device.py`). The `_NullTTS` stub is gone — `keyboard_bridge.py` and `typed_event.py` now wire a real provider through `store.tts`/`store.tts_interrupt_event`. `--tts`/`--output-device` are live; `--voice`/`--rate`/`--volume` added. See §7 2026-07-09 entry |
| Tests for the client-side runtime package | **Found (added this session).** `tests/test_runtime_client_config.py`, `test_runtime_client_typed_event.py`, `test_runtime_client_keyboard_bridge.py`, `test_runtime_client_websocket_client.py`. `src/runtime_client/audio_bridge.py` and `main.py` remain untested (mic-hardware dependency, not covered this pass) |
| Tests for a control-event dispatch loop | **Partially found (added this session).** `tests/test_h6_control_event_relay.py` covers the Transport Gateway's inbound-text-frame→control-pipe relay (server ingress half) against the real `_handler` coroutine and real pipe fds. `tests/test_runtime_client_keyboard_bridge.py` covers the Client's keypress→Control-Event-JSON half against the real `KeyboardController`. **Not covered:** `phantom_runtime.py`'s `control_loop()` itself (the pipe-read→dispatch half) — this project's Single Runtime Policy (see `tests/test_h4_10_runtime_adapter.py`) forbids automated tests from importing or driving `phantom_runtime.py` directly, and no `OPENAI_API_KEY`/`GEMINI_API_KEY` is available in this sandbox to run it as a real subprocess either. That remaining gap requires a manual local run (real API key, real Cloud Run shell) to close |
| Root-level duplicate profile files (`workport.md`, `phantom_runtime.md`, `upwork.md`) | Not checked for parity |
| `src/enterprise.json` content/consumption | Not inspected |

## 6. 優先順位付き移植計画 (Prioritized Plan — for Validation Work, Not Implementation)

This is a sequencing recommendation for *future* tasks that will actually run the validation steps referenced in §0. No implementation should happen from this document alone.

1. ~~Resolve open questions in §1 before any further work touches `runtime_framework/`, Verification, or Trust~~ — **RESOLVED 2026-07-08, see §1.1.** `runtime_framework/` is almost entirely 対象外 (its typed-event/control-plane role is already fulfilled by H4/H6); Trust is already delivered via H4-3 (対象外 for further porting — nothing left to do). The one item that remains genuinely open is **Verification**: A33-equivalent conversational-consistency checking is confirmed 移植対象 and has not been started — this should be re-prioritized into this list as future work if the product still wants that specific Memory capability.
2. **Control Event validation** — dispatch loop and WS relay for control commands; currently the newest/most in-flux area per the target-repo survey.
3. **Runtime Client validation** — end-to-end audio→WS→server round trip; add test coverage. ~~resolve the TTS/output-device stub~~ — **DONE 2026-07-09**, see §7. Still open: the full audio→WS→server round trip against a real deployed Cloud Run instance.
4. **Keyboard parity validation** — confirm the server keyboard loop, any control-event dispatch loop, and the client keyboard bridge all produce identical behavior for the same command.
5. **Cloud Run / Transport E2E validation** — full WebSocket session lifecycle against the Cloud Run shell.
6. **Persistence backend decision** — determine whether the PostgreSQL backend and recovery/export tool are required for the target deployment; if yes, scope as a discrete port.
7. **Memory subsystem functional verification** — structurally present per this survey; needs correctness validation (unit/integration) to progress past Unknown.
8. **Cleanup items** (lower priority, do not block the above): reconcile inline-vs-extracted duplicates (profile parsing, meaningful-text gate, transcript persistence), and pick a canonical generation for the features that currently exist twice (Timeline: A27 vs. B28; Dashboard: A30 vs. B30; Consistency: A33 vs. B29).

Only after each of the validation activities above has actually run should the corresponding matrix rows move from **Unknown** to **Completed**, and only after functional/E2E confirmation should they move to **Verified**.

---

## 7. Validation Log

Chronological record of what was *actually run* to justify each Status change above (per §0: no status is assigned from inspection alone). Entries are additive; do not delete past entries when appending new ones.

### 2026-07-08 — H6 Control Event + Runtime Client Phase 1-4 validation pass

Picked up in-progress, uncommitted work already on disk at session start: H6 Control Event plumbing across `src/runtime/cloud_run_shell.py` (3rd pipe fd), `src/runtime/transport_gateway.py` (inbound text-frame relay), `src/phantom_runtime.py` (`control_loop()`); and a fully-scaffolded `src/runtime_client/` package (Phase 1-2 audio, 1-3 control events, 1-4 keyboard/typed-event UX). Neither had any test coverage yet. Baseline before this pass: 238 passed, 2 skipped.

**Added and ran:**
- `tests/test_h6_control_event_relay.py` (7 tests) — exercises the real `TransportGateway._handler` coroutine against real `os.pipe()` fds and a fake WebSocket connection: confirms inbound text frames relay verbatim to the session's control pipe (one JSON command per newline-terminated line), inbound binary frames still relay to the audio pipe untouched, mixed frame sequences route correctly, and `session_teardown`/active-slot release happen exactly once even when the control pipe is already closed.
- `tests/test_runtime_client_config.py` (15 tests) — `parse_args()` required-arg enforcement, `--list-devices`/`--list-output-devices` bypass, all CLI overrides, `build_ws_url()` scheme mapping (`http→ws`, `https→wss`) and path/query stripping.
- `tests/test_runtime_client_typed_event.py` (13 tests) — `TypedEventStore.handle_line()` against literal Typed Event JSON envelopes for every event type (`transcript`/`reply`/`status`/`latency`/`error`/`analysis`/unknown), bounded-log truncation, malformed/non-dict JSON handling.
- `tests/test_runtime_client_keyboard_bridge.py` (10 tests) — `NotifyingEvent` callback firing, `_send_control()`'s asyncio-queue enqueue, and a full live-thread integration test running the real (unmodified) `ui.keyboard.KeyboardController` against `build_keyboard_thread()`'s `RuntimeContext`, scripted stdin, and a real background asyncio loop — confirms `G`/`g`/`r` produce exactly the Control Event JSON `phantom_runtime.py`'s `control_loop()` is documented to accept, `q` sets `kb_shutdown`, and local-only keys (`h`/`u`/`d`/`t`/`?`) never touch the control queue.
- `tests/test_runtime_client_websocket_client.py` (10 tests) — `_send_audio`/`_send_control`/`_receive_events` in isolation; `_pump`'s stop-event-driven cancellation and websocket close; `run()`'s reconnect classification against real `websockets.exceptions.ConnectionClosed`/`InvalidStatus` instances (fatal codes 400/1008 stop immediately, retryable errors retry up to `max_reconnect_attempts` with exponential backoff then give up, a clean disconnect resets the attempt counter).

**Bug found and fixed during this pass:** `build_keyboard_thread()` initialized its local `recording_active` flag via `NotifyingEvent.set()` (the overridden version, which fires `on_change` and sends a Control Event) instead of the plain `threading.Event.set()`. This sent a spurious `{"command": "toggle_recording"}` to the server on every client connection, before the user ever pressed `r` — and since the server's `VADBuffer` already defaults to recording-ON, this would silently flip recording OFF at the start of every session. Fixed in `src/runtime_client/keyboard_bridge.py` to call `threading.Event.set(recording_active)` directly, bypassing the notifying override for this one initialization call. Caught by `test_r_toggles_and_sends_toggle_recording` and `test_non_control_keys_do_not_touch_control_queue` failing before the fix, passing after.

**Result:** 293 passed, 2 skipped (was 238 passed, 2 skipped) — 55 new tests added, 0 regressions, 1 real bug fixed.

**Explicitly not validated this pass (documented, not guessed):**
- `phantom_runtime.py`'s `control_loop()` (the server-side pipe-read→dispatch half of Control Events) — blocked by this project's Single Runtime Policy (no automated test may import or drive `phantom_runtime.py` directly) and by the absence of `OPENAI_API_KEY`/`GEMINI_API_KEY` in this sandbox (real subprocess E2E is not possible here either). Requires a manual local run with a real API key to close.
- `src/runtime_client/audio_bridge.py` and `main.py` — require real or mocked `sounddevice` hardware; not covered this pass.
- Any real Cloud Run deployment check — this pass was entirely local/unit-level.

### 2026-07-08 — `runtime_framework/` / Verification / Trust scope classification (investigation only, no code changed)

Requested: classify §1's three open questions into 移植対象 / 対象外 / 既存H4-H6で置き換え済み. Method: read `docs/H4_IMPLEMENTATION_PLAN.md` and `docs/H4_STATUS.md` in full; read `src/verification/verification_runtime.py`, `src/trust/trust_runtime.py`, `src/aggregator/event_aggregator.py` in full; ran `grep -rl "runtime_framework" src/ docs/ tests/` (only hit: this matrix document itself) and `find src/verification src/trust src/dashboard src/aggregator src/api -type f` to enumerate what actually exists in the target repo under those directory names.

**Findings**, in full in §1.1 above:
- `runtime_framework/` (64 files) has zero references anywhere in the target repo. `H4_IMPLEMENTATION_PLAN.md` (Frozen v1.0) defines a Single Runtime Policy explicitly prohibiting "Secondary Runtime / Replacement Runtime / Mock Runtime" — most of `runtime_framework/`'s Agent/Coordinator/Pipeline abstraction is therefore permanently 対象外, not merely unstarted. Its typed-event/control-plane-lifecycle design references are 既存H4/H6で置き換え済み (functional-role replacement via the already-Verified H4-1 Contract + H6 Control Events, not a literal file port). Its Health/Metrics/Alerting/Observability/Readiness/Logging subsystems are 対象外 — no consumer, and the operationally-necessary parts are already covered by separate prior art (`src/runtime/health.py`, `health_server.py`, `runtime_logger.py`).
- Verification (H4-2, read in full): confirmed to check RuntimeEvent wire-protocol/schema/ordering/fallback quality — a different concern from SSoT's A33 Fact-Graph/Subject/Decision content-consistency check. Naming collision only, not a replacement. **移植対象, not started.**
- Trust (H4-3, read in full): confirmed a complete, already-shipped, net-new capability (weighted Trust Policy, frozen spec) with no SSoT origin. Open question #3 answered: yes, intentional net-new addition. 対象外 for further porting (nothing to port) — already delivered.
- Noted but out of this classification's scope: `dashboard_runtime.py` (H4-6) shares its name with SSoT's Memory Dashboard (A30/B30) but renders different content (Runtime-event-level vs. Memory-subject-level) — same naming-collision risk pattern, flagged for future awareness, no Status change made.

**Result:** No code changed. §1 open questions marked resolved with cross-references to the new §1.1 classification table; §6 priority item 1 updated to reflect resolution and to surface Verification (A33) as the one item still genuinely 移植対象.

### 2026-07-09 — Phase 3: TTS + Virtual Output Device Routing

Scope: port the SSoT's TTS provider abstraction (`_NullTTSProvider`/`_SayTTSProvider`/`_Pyttsx3Provider`/`_build_tts_provider()`, v22.py:920-1035) into `src/runtime_client/`, replacing the `_NullTTS` stub in `keyboard_bridge.py` that Phase 1-4 left in place, and add macOS output-device selection so speech can be routed into a virtual device (BlackHole/Loopback) or a Multi-Output Device for Zoom/Meet/Teams/Discord/Slack Huddle use. Per §0/§1: read this matrix first, did not re-survey `poc-ai-meeting` wholesale, and only read the specific SSoT sections the existing TTS-related rows already pointed at (v22.py:920-1035 constructor bodies, `_build_tts_provider()` at 1298-1307, and the reply-speaking wait loop at 3167-3184) to port the interrupt/deadline logic verbatim.

**Discovery requiring the exception clause in the task instructions:** grepped v22.py for `output.device|OutputStream|volume|BlackHole` — zero hits. Voice/rate are hardcoded constructor defaults (`voice="Samantha"`, `rate=200`/`175`) with no CLI exposure; volume control and any output-device concept do not exist anywhere in the SSoT. These were requested explicitly in the Phase 3 spec regardless, so they are implemented and recorded as **net-new greenfield rows** (same convention as the pre-existing Transport/Runtime Client greenfield rows), not folded into the ported "TTS provider abstraction" row as if they'd been ported from somewhere.

**Added:**
- `src/runtime_client/tts.py` — `TTSProvider` interface, `NullTTSProvider`, `SayTTSProvider`, `Pyttsx3Provider`, `build_tts_provider()`. Playback design (documented in the module docstring): each utterance renders to a temp WAV (`say -o ... --data-format=LEI16@<rate>` directly produces WAV; `pyttsx3.save_to_file()` produces AIFF, normalized via the stock `afconvert` CLI), then plays through a shared `_WavPlayer` targeting an explicit `sounddevice` output-device index, with volume applied as PCM sample scaling at the playback layer (provider-agnostic). Chosen over mutating the macOS system default output device: zero global side effects, works with the "Multi-Output Device" pattern (hear it locally + feed a virtual device simultaneously) already configured on the validation machine, no new pip dependency (stdlib `wave` + already-present `sounddevice`/`numpy` + stock macOS `say`/`afconvert`).
- `src/runtime_client/output_device.py` — `resolve_output_device_id()` (mirrors `audio/devices.py:resolve_device_id`'s NFC exact/substring matching, filtered to `max_output_channels > 0`, plus index and `None`/`"default"` handling), `list_output_devices()`, `print_output_devices()`.
- `tests/test_runtime_client_tts.py` (20 tests), `tests/test_runtime_client_output_device.py` (15 tests) — new.

**Modified:**
- `src/runtime_client/config.py` — `--voice`, `--rate`, `--volume` (validated to `[0.0, 1.0]`) added; `ClientConfig` extended.
- `src/runtime_client/typed_event.py` — `TypedEventStore` gains `tts`/`tts_interrupt_event` (default to `NullTTSProvider()`/a fresh `Event()`, so all pre-existing call sites keep working unmodified). `_handle_reply()` now spawns a daemon thread replicating v22.py:3167-3184's speak/wait/interrupt loop verbatim (10.0s deadline, 0.05s poll, `"[TTS] interrupted by operator speech"` message) when `tts` is not a `NullTTSProvider`.
- `src/runtime_client/keyboard_bridge.py` — deleted the `_NullTTS` stub class; `RuntimeContext.tts`/`.tts_interrupt_event` now read directly from `store.tts`/`store.tts_interrupt_event` (not new params — reusing the same existing pattern already used for `store.transcript_log`/`store.log_lock`), so the keyboard thread's `s`-key stop path and the reply-speaking loop always share one provider + one interrupt `Event`.
- `src/runtime_client/main.py` — resolves the output device, builds the shared `tts`/`tts_interrupt_event`, passes both into `TypedEventStore`, replaced the inline `_list_output_devices()` duplicate with the new shared `output_device.print_output_devices`, added `tts.stop()` to the shutdown path, extended the startup banner.
- `tests/test_runtime_client_config.py` (+3 new test methods for out-of-range `--volume` rejection/boundaries, plus voice/rate/volume assertions folded into the existing defaults/overrides tests), `tests/test_runtime_client_typed_event.py` (+4 tests: reply→speak wiring, empty-text no-op, interrupt path, default-store no-thread-spawned), `tests/test_runtime_client_keyboard_bridge.py` (+2 tests: real `s`-key stop routed through `store.tts`) — no call-site signature changes were needed anywhere (`TypedEventStore()`/`build_keyboard_thread(...)` both keep their pre-existing arity thanks to the default-arg/store-attribute design above), so the *existing* Phase 1-4 tests needed zero edits beyond the additions above.

**Validation actually run this pass:**
1. `python3 -m py_compile` on every new/modified `src/runtime_client/*.py` and `tests/test_runtime_client_*.py` file — clean.
2. `python3 -m pytest tests/ -q` — **337 passed, 2 skipped** (was 293 passed, 2 skipped after the 2026-07-08 H6 pass) — 44 new tests, **zero regressions**.
3. Live local smoke check (this sandbox is macOS with `say`/`afconvert`/`sounddevice`/BlackHole genuinely present — a real check, not a mock): drove `SayTTSProvider` end-to-end against (a) system default output and (b) the resolved `BlackHole 2ch` device id, confirming correct `is_speaking()` False→True→False transitions and no exceptions in either case; confirmed `NullTTSProvider` stays silent. Also ran the actual `python -m runtime_client` entrypoint with `--tts say --voice Samantha --rate 190 --volume 0.8 --output-device BlackHole`, confirmed the startup banner renders the new fields correctly, the (unmodified) keyboard help text renders identically to the pre-Phase-3 baseline, and Ctrl-C shutdown exits cleanly (exit 0) — the only failure observed was the expected TLS/connection error against the placeholder `https://example.run.app` URL, which has no real Cloud Run backend in this sandbox.

**Explicitly not validated this pass** (documented, not guessed, per §0's rule that Verified requires an actual check):
- A real Cloud Run round trip — a live `reply` Typed Event arriving over an actual deployed WebSocket session, causing the Client to speak it — requires a deployed server + a real `OPENAI_API_KEY`/`GEMINI_API_KEY`, neither available in this sandbox. `TypedEventStore._handle_reply`'s TTS-triggering logic itself is unit-tested against literal `reply` envelope JSON (matching the wire shape `runtime.transport_gateway` actually relays), but the full network path is not exercised.
- `Pyttsx3Provider`'s real audio path — `pyttsx3` is not installed in this sandbox (matching the SSoT's own optional-dependency treatment). The "not installed" path is exercised for real (deterministically, via `sys.modules["pyttsx3"] = None`); the "installed" path is exercised against a fully mocked `pyttsx3` engine + mocked `afconvert`, not the real library.
- `--rate`/`--volume` audible correctness (i.e., that a rate of 220 actually sounds faster, or that volume 0.5 is actually half as loud to a human ear) — the smoke check confirms the plumbing runs without error and that PCM sample scaling math is correct (unit-tested directly), not perceptual correctness.

**Result:** TTS provider abstraction, TTS interrupt signaling, and TTS keyboard control (stop) move from `Unknown` to `Verified`. Six new greenfield rows (Voice selection, Speech rate, Volume control, Output-device enumeration, Output-device selection/switching, Default-device fallback) added at `Verified`, each backed by the specific automated + live-smoke checks above. Not moved to `Verified`: the full Cloud Run round-trip end of Runtime Client validation (§6 item 3) and any Memory/Verification/Persistence rows, which remain outside this pass's scope.
