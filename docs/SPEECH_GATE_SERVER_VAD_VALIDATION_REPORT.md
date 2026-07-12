# Speech Gate × Server VAD 設計不整合修正 — Validation Report

**Document:** SPEECH_GATE_SERVER_VAD_VALIDATION_REPORT.md
**Status:** Code Fix + Synthetic Validation = Final / Live Validation = Deferred（オペレーター実施待ち）
**対象コンポーネント:** `src/audio/vad.py`（VADOrchestrator）, `src/audio/vad_buffering.py`（VADBuffer）
**変更禁止（今回未変更を確認済み）:** Verification Runtime, Trust Runtime, Dashboard Runtime, Runtime Event Contract, API Contract, Dashboard Contract, Runtime Pipelineの判定ロジック（`_route_segment` / `_enqueue_latest`）

---

## 1. Root Cause Report

**症状:** Server VAD の自動フラッシュが `reason=force` のみで発生し、`reason=silence` が一度も発生しない（実測 85/85 force, 0 silence）。

**原因:**

```
Client Speech Gate (runtime_client/audio_bridge.py _run_pump)
    if rms < gate: continue   -- 無音ブロックはそもそも送信されない
        │
        ▼
Server VAD (audio/vad_buffering.py VADBuffer.process_frame)
    _silence_streak は「受信した無音フレームの連続数」でカウント
```

Client Speech Gate は無音時にブロックを一切送信しないため、Server VAD は無音フレームを一度も受信しない。`process_frame` の `_silence_streak` はフレーム受信のたびにしか進まないカウンタであるため、無音フレームが届かない限り増加せず、`silence_flush`（`_silence_streak >= silence_blocks`）の条件に到達不能。結果として、すべてのセグメントは `max_samples`（`--max-sec` 上限）到達による `force_flush` でのみ終了する。

**契約への影響範囲確認:** `flush_reason`（`"force"` / `"silence"`）は `process_frame` 内でログ出力・デバッグトレース出力にのみ使用され、`_route_segment` → `_enqueue_latest` に渡るのは確定済みの音声波形（`np.ndarray`）のみで `flush_reason` 自体は一切伝播しない。したがって本修正は Server VAD 内部に完全に閉じており、Runtime Pipeline の判定ロジック・Runtime Event Contract・API/Dashboard Contract・Verification/Trust/Dashboard Runtime のいずれにも影響しない。

---

## 2. Code Fix

契約変更を伴わない方針（Server側で最後のAudio到着時刻から無音時間を判定）で実装。

| File | 変更 | 概要 |
|---|---|---|
| `src/audio/vad_buffering.py` | +52 行 | `VADBuffer.check_idle_timeout(idle_sec, silence_timeout_sec)` を新規追加。`process_frame` のフレーム数ベース `_silence_streak` に対する、壁時計時間ベースの対になるメソッド。発話中セグメントが存在し、`min_samples` を満たしており、最後にブロックが実際に届いてからの経過時間（`idle_sec`）が `silence_timeout_sec` を超えた場合、`process_frame` の `silence_flush` 分岐と同じ経路で `reason="silence"` として確定する。 |
| `src/audio/vad.py` | +22 行 | `VADOrchestrator.run()` に `_last_block_ts` を追加し、既存の `queue.get(timeout=0.5)` の `queue.Empty` 分岐（従来は単なる `continue`）から新メソッドを駆動。閾値は既存の `silence_blocks * block_size / sample_rate` から導出し、新しいCLIフラグやconfigスキーマ変更は一切なし。 |

Client→Server Contract（WebSocket音声フレーム形式）は無変更。新規の設定項目・CLIフラグなし。

---

## 3. Validation Result

| 項目 | 結果 |
|---|---|
| py_compile | ✅ `src/audio/vad.py`, `src/audio/vad_buffering.py`, `src/` 全体 |
| 既存ユニットテスト | ✅ 474 passed / 2 skipped（修正前ベースライン 467 passed / 2 skipped — リグレッションなし） |
| 新規ユニットテスト | ✅ `tests/test_audio_vad_idle_silence.py`（147行、7ケース）全パス。`check_idle_timeout` の境界条件（無発話時/閾値未満/min_samples未満/正常発火/状態リセット）に加え、Client が完全に送信を止めるケースで `VADOrchestrator` 経由で `reason=silence` に到達することをエンドツーエンドで確認 |
| Runtime Pipeline Trace 動作確認 | ✅ Synthetic Validation（§5）にて `runtime_trace`（`PHANTOM_TRACE=1`）経由で `VAD START` / `VAD FLUSH` イベントが整形式JSONLとして正しく出力されることを確認 |
| **Live Validation（localhost / cloud_run_shell / runtime_client / 実Whisper API / BlackHole, ChatGPT Voiceによる約12.5分シナリオ再生）** | **⏸ Deferred — 今回は対象外。最終確認はオペレーターが実機で実施（§6）** |

### Live Validation 試行時に判明した運用上の注意点（参考情報）

今回、ローカルでの実施を試みた際に以下が判明した。いずれも本Fix自体の欠陥ではないが、オペレーターが実機確認を行う際の参考として記録する。

1. `cloud_run_shell` 起動時に `--audio-source fd` を明示しないと、spawnされるRuntime子プロセスは既定値 `--audio-source mic` で動作し、Clientからのパイプ音声ではなく**サーバー側ローカルマイクを直接キャプチャ**してしまう（実施したいテストの前提が崩れる）。実機確認時は必ず `python -m runtime.cloud_run_shell -- --audio-source fd` を使用すること。
2. 上記の誤設定下での試行中、WebSocket が `keepalive ping timeout` で切断 → 再接続が `409 (fatal)` で拒否され、Client が終了する事象を観測した。Root Causeは未特定（本タスクのスコープ外）。既出の reconnect 関連バグ（`docs/bugs/BUG-2026-07-11-runtime-event-display-stops-after-reconnect.md`）と合わせて、実機確認中に再現した場合は別途バグ報告を推奨する。

---

## 4. Flush Statistics（Before / After）

### Before（既知の実測値、約16.5分のローカル実行）

```
force_flush   : 85
silence_flush : 0
```

### After（Synthetic Validation — 実運用コード・デフォルト設定・実時間ベース、§5参照）

```
Total delivered segments : 104
  reason=force   : 24  (23.1%)
  reason=silence : 80  (76.9%)
  discarded（無発話フラッシュ）: 0
```

`silence_flush` が「構造的に到達不能（0%）」から「主体（76.9%）」に変化したことを、同一デフォルト閾値（`--max-sec 8.0`, `--silence-sec 0.25`）下で確認。

### セグメント長（Synthetic Validation）

```
              avg     max     min
  全体        4.53s   8.80s   1.00s
  force       8.80s   8.80s   8.80s   （max_samples上限 + tail_padding 0.80s。全件が上限到達で妥当）
  silence     3.24s   5.90s   1.00s
```

**注記:** 上記 After 統計は実際の本番コード（`VADOrchestrator` / `VADBuffer`、修正部分以外無変更）を実時間で駆動した Synthetic Validation によるものであり、Whisper品質（文重複・ハルシネーション等の実例比較）は実音声を要するため未取得。この比較は §6 の実機確認で取得する。

---

## 5. Synthetic Validation / Runtime Pipeline Trace Summary

**方法:** `audio.vad.VADOrchestrator` + `audio.vad_buffering.VADBuffer`（修正後・無改変コード）を実際のスレッド構成で駆動。80件の合成発話（70%: 1–6秒、30%: 9–11秒＝強制フラッシュ狙い）を、発話間に実時間の無音ギャップ（0.6〜1.8秒、`silence_timeout_sec=0.20秒` に対し十分なマージン）を挟んで投入。Client Speech Gate の実際の挙動に合わせ、無音区間ではブロックを一切キューに投入しない（無音ブロックの送信でごまかさない）。`PHANTOM_TRACE=1` でRuntime Pipeline Traceを収集。再現可能（`SYN_SEED=20260712`）。

**Trace結果:** `VAD START` 104件 / `VAD FLUSH` 104件（`reason=force` 24件, `reason=silence` 80件, discarded 0件）— 1:1で整合、JSONL形式で正常出力。

---

## 6. Operational Validation

本変更の最終確認はオペレーターによる実機確認で実施する。

**構成:** localhost / cloud_run_shell（`--audio-source fd` 必須, §3参照） / runtime_client / 実Whisper API / BlackHole / ChatGPT Voiceによるシナリオ再生

**確認項目:**

- [ ] Runtime Conversation が最後まで生成されること
- [ ] Runtime Event が最後まで生成されること
- [ ] Dashboard が更新されること
- [ ] `reason=silence` が発生すること
- [ ] `force_flush` のみにならないこと
- [ ] Whisper 例外が発生しないこと
- [ ] WebSocket が正常維持されること

上記が全て確認された時点で、本Fixの実運用環境での有効性が最終確定する。

---

## 7. Git Diff Summary

```
 src/audio/vad.py           | 22 ++++++++++++++++++++
 src/audio/vad_buffering.py | 52 ++++++++++++++++++++++++++++++++++++++++++++++
 2 files changed, 74 insertions(+), 0 deletions(-)

 tests/test_audio_vad_idle_silence.py | 147 ++++++++++++++++++++++++ (new file)
```

**注記:** `git status` には本Fixと無関係な、既存の未コミット変更（`phantom_runtime.py`, `runtime/transport_gateway.py`, `runtime_client/audio_bridge.py`, `runtime_client/main.py`, `runtime_client/typed_event.py`, `runtime_client/websocket_client.py`, 新規 `src/runtime_trace.py`）が存在する。これは別の調査（Runtime Pipeline stall investigation）由来のデバッグ計装（`PHANTOM_TRACE=1` でのみ有効、デフォルトは no-op）であり、本Fixの一部ではない。今回は変更していない。

---

## 8. 影響範囲（Impact Analysis）

| コンポーネント | 影響 |
|---|---|
| Verification Runtime | なし（未変更・未参照） |
| Trust Runtime | なし（未変更・未参照） |
| Dashboard Runtime | なし（未変更・未参照） |
| Runtime Event Contract | なし（`flush_reason` はイベントペイロードに一切伝播しない） |
| API Contract | なし |
| Dashboard Contract | なし |
| Runtime Pipeline 判定ロジック（`_route_segment` / `_enqueue_latest`） | なし（受け取るのは確定済み音声波形のみ） |
| Client→Server Wire Contract | なし（新規フィールド・フラグなし） |

---

## 9. Deliverables Summary

- ✅ Root Cause Report（§1）
- ✅ Code Fix（§2）
- ✅ Unit Test（§3, `tests/test_audio_vad_idle_silence.py`）
- ✅ Synthetic Validation（§5）
- ✅ Runtime Pipeline Trace Validation（§5）
- ✅ Flush Statistics Before/After（§4）
- ✅ Git Diff Summary（§7）
- ⏸ **Live Validation — Deferred**（§6 Operational Validationとしてオペレーター実施待ち）
