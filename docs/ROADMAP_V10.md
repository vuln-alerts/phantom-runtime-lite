# Phantom Runtime Lite — Roadmap (V10 / H4 Runtime Extension)

**Document:** ROADMAP_V10.md
**Status:** H4 Runtime Extension — Completed
**Source:** docs/H4_STATUS.md, docs/H4_10_VALIDATION_REPORT.md

This roadmap records the completion status of the H4 Runtime Extension. It
does not introduce new roadmap items beyond what is already recorded in
docs/H4_STATUS.md and docs/H4_10_VALIDATION_REPORT.md.

---

## H4 Runtime Extension: Completed

| Item | Component | Status |
| --- | --- | --- |
| H4-1 | Runtime Event Contract | Completed |
| H4-2 | Verification Runtime | Completed |
| H4-3 | Trust Runtime | Completed |
| H4-4 | Event Aggregator | Completed |
| H4-5 | FastAPI | Completed |
| H4-6 | Dashboard | Completed |
| H4-7 | Integration | Completed |
| H4-8 | OpenAI Validation | Completed |
| H4-9 | Gemini Validation | Completed |
| H4-10 | Final Validation | Completed |

Source: docs/H4_STATUS.md.

---

## Validation Results

* Tests Collected: 217
* Passed: 215
* Skipped: 2
* Failures: 0
* Errors: 0

| Validation | Result |
|---|---|
| OpenAI Live Validation | PASS |
| Gemini Live Validation | PASS |
| Production-like Validation | PASS |
| Regression | PASS |

Full detail: docs/H4_10_VALIDATION_REPORT.md, docs/H4_10_LIVE_VALIDATION_REPORT.md,
docs/H4_10_GEMINI_LIVE_VALIDATION_REPORT.md.

---

## Hackathon Submission Ready

* H4-10 Status: Completed
* Validation: PASS
* Production-like Validation: PASS
* Regression: PASS
* Hackathon Validation: Ready

Source: docs/H4_STATUS.md.

---

## Future Work

The following items are tracked as Remaining Risks in
docs/H4_10_VALIDATION_REPORT.md §7 and are not yet implemented:

1. Live consumer wiring — no standing process yet connects to
   `runtime/transport_gateway.py`'s `/ws` stream and drives it through the
   Runtime Adapter / H4-2..H4-6 chain automatically in production.
2. FastAPI endpoint expansion — `/events`, `/verification`, `/trust`,
   `/timeline` are documented in docs/H4_IMPLEMENTATION_PLAN.md but not yet
   implemented in `src/api/api_server.py` (only `/health` and `/aggregate`
   exist today).
3. Status vocabulary alignment — `status.state` passes through untranslated,
   so real status events (including `idle`) report as `undefined state` to
   Verification Runtime under the current design.
4. Durable identity/session persistence — `event_id`/`session_id`/`sequence`
   are adapter-local and are not persisted or recoverable across adapter
   process restarts.

These items are recorded here only as pointers; full detail, rationale, and
scope boundaries are documented in docs/H4_10_VALIDATION_REPORT.md §7 and are
not duplicated in this roadmap.

---

## H5-2 Runtime Queue Reliability Improvement (Post Hackathon)

**Status:** Planned
**Priority:** Medium

**Reason:** Hackathon提出には影響しないが、Scenario-2 Race Stress Test
（`_enqueue_latest()` の latest-wins Queue を対象としたレース誘発ストレステスト）
により、Queue設計上の制約が確認されたため。

### Background

* Scenario-1（通常利用ペース）: 10/10 PASS — Raceは再現しなかった。
* Scenario-2（Race Stress、Whisper処理中に次Segmentを連続投入）:
  * `Queue: dropped` が発生
  * 送信 Segment数: 25
  * Transcript数: 14
  * Queue Drop数: 11
  * Transcript欠落数: 11
* Queue Drop数とTranscript欠落数が完全一致したため、latest-wins Queue設計が
  高頻度発話時の欠落要因であることを確認した。
* Whisper / OpenAI SDK / Cloud Run / Transport Gateway / Typed Event の各層に
  欠陥は確認されなかった（例外・タイムアウト・ProviderRejected・
  SessionSpawnErrorはいずれも0件、クライアント受信typed event数はサーバー側
  発火数と完全一致）。

この制約は、Scenario-2 Stress Testにおいて意図的にRace条件を誘発した場合にのみ
再現したものであり、通常利用ペースのScenario-1検証（10/10 PASS）、
Acceptance Test、Final Validation、およびCloud Run実機検証では再現しなかった。
そのため本件はHackathon提出を妨げる欠陥ではなく、Hackathon提出後のRuntime
信頼性改善項目として記録する。

### Scope (Hackathon後、今回は未実装)

| Item | Title | Goal |
| --- | --- | --- |
| H5-2-1 | Queue Drop可視化 | `segment_dropped` または `status` イベント追加によるユーザーへの欠落通知 |
| H5-2-2 | Queue Strategy改善 | latest-wins → FIFO または Merge Strategy の評価 |
| H5-2-3 | Merge Strategy評価 | 短い連続発話をWhisper送信前に統合する方式の検討 |
| H5-2-4 | Parallel Whisper評価 | 限定的並列化の可能性調査（順序保証・Rate Limit・コストを評価対象） |
| H5-2-5 | Stress Test追加 | Scenario-2を正式Regression Testへ追加予定 |

### Important

Scenario-2はStress Testであり、通常利用品質の検証ではない。Scenario-1では
10/10 PASS・Acceptance PASS・Final Validation PASS・Cloud Run実機PASSが
確認されており、Hackathon提出品質には問題ない。そのため本件はHackathon
提出後対応として扱う。

---

## P5-4 Adaptive Runtime Calibration

**Source:** `docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md`,
`docs/designs/IMPLEMENTATION_PLAN_P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md`

| Phase | Component | Status |
| --- | --- | --- |
| Phase 1 | Environment Observation | Completed |
| Phase 2 | Calibration Engine | Completed |
| Phase 3 | Runtime UI | Completed |
| Phase 4 | Re-calibration | Completed |
| Phase 5 | Integration | Completed |

Source: `docs/MIGRATION_MATRIX.md` §7 Validation Log, 2026-07-10 entry.
