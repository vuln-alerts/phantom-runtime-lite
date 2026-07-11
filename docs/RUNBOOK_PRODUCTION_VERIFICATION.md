# Production Verification Runbook — P5-4 Adaptive Runtime Calibration

**Version:** 1.1
**Target Feature:** P5-4 Adaptive Runtime Calibration（`src/runtime_client/calibration.py`, `src/runtime_client/main.py`）／ §11以降: TransportGateway Session Lifecycle（`src/runtime/transport_gateway.py`）
**Related:** `docs/RUNBOOK.md`（Cloud Run構築・デプロイ・運用手順）, `docs/MIGRATION_MATRIX.md`, `docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md`, `docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md`（§11の根拠となった原因調査・修正・実機検証の詳細）
**Last Updated:** 2026-07-12

---

# 1. Purpose

本Runbookは、P5-4 Adaptive Runtime Calibration（Startup Calibration / Dynamic Contamination Threshold / Speech Gate導出）の Production Verification 手順を示します。

本Runbookで確認する内容

- Startup Calibration の動作確認（実機マイクを使用）
- Adaptive Runtime Calibration（Dynamic Contamination Threshold / Speech Gate導出）の動作確認
- Cloud Runへの接続確認
- `--production-verification` フラグによる調査用ログ出力の確認

本Runbookは Cloud Run自体の構築・デプロイ手順は対象外です。デプロイ済みの Cloud Run サービスが既に存在することを前提とします（構築手順は `docs/RUNBOOK.md` §4-§9 を参照）。

---

# 2. Prerequisites

以下がインストール・設定済みであること。

- Python 3.13+
- venv（推奨。`requirements.txt` の依存関係を分離するため）
- Google Cloud SDK（`gcloud`）
- 実機マイク（内蔵マイク、または外部USBマイク）
- GitHub `main` の最新版（`src/runtime_client/calibration.py`, `main.py`, `config.py`, `audio_bridge.py` を含む）

Cloud Run側（デプロイ済みサービス）に以下が設定済みであること。`OPENAI_API_KEY` / `GEMINI_API_KEY` は Runtime Client 側の環境変数ではなく、Cloud Run サービスに対して `gcloud run services update` で設定するサーバー側の値です（`docs/RUNBOOK.md` §6 参照）。

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

Runtime Client側のセットアップ例

```bash
cd /path/to/phantom-runtime-lite
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

# 3. Environment Verification

Cloud Run側の状態を確認します。手順は `docs/RUNBOOK.md` §4-§5 と同一です。値は環境ごとに異なるためプレースホルダーで示します。

## 3.1 Login確認

```bash
gcloud auth list
```

期待

```
ACTIVE
*
<YOUR_GCLOUD_ACCOUNT>
```

異常時

```bash
gcloud config set account <YOUR_GCLOUD_ACCOUNT>
```

## 3.2 Project確認

```bash
gcloud config get-value project
```

期待

```
<PROJECT_NAME>
```

異常時

```bash
gcloud config set project <PROJECT_NAME>
```

## 3.3 Cloud Run Service確認

```bash
gcloud run services list \
  --region <REGION>
```

期待: `<SERVICE_NAME>` が表示される。

## 3.4 Cloud Run URL取得

```bash
gcloud run services describe <SERVICE_NAME> \
  --region <REGION> \
  --format="value(status.url)"
```

期待

```
<CLOUD_RUN_URL>
```

以降、この値を `<CLOUD_RUN_URL>` として使用する。

---

# 4. Audio Device Verification

Runtime Clientが使用する入力デバイス（マイク）を確認します。

```bash
cd src
python -m runtime_client --list-devices
```

実装（`src/runtime_client/main.py` `_list_input_devices()`）は `sounddevice.query_devices()` を呼び、`max_input_channels > 0` のデバイスのみを `[index] name` 形式で列挙します。

期待される出力形式（実機構成に応じて内容は変わる）

```
[runtime_client] available input devices:
  [<index>] <デフォルトマイクの名称（例: MacBook Pro Microphone）>
  [<index>] <外部マイクの名称>
  [<index>] <仮想デバイスの名称（例: BlackHole 2ch）>
```

確認ポイント

- デフォルトマイク（内蔵マイク）が一覧に表示されること
- 外部マイクを使用する場合、対象デバイス名が一覧に表示されること
- `--input-device` にはデバイス名の部分一致文字列、またはインデックス番号（例: `"2"`）を指定できる（`src/runtime_client/audio_bridge.py` `resolve_input_device()` → `audio/devices.py` `resolve_device_id()`）
- 指定したデバイスが見つからない場合、`main.py` はシステムデフォルト入力にフォールバックし、警告と共に利用可能デバイス一覧を再表示する

---

# 5. Production Verification

## 5.1 起動コマンド

```bash
cd src
PHANTOM_CALIBRATION_DEBUG=1 \
python -m runtime_client \
  --url <CLOUD_RUN_URL> \
  --provider openai \
  --input-device "<入力デバイス名>" \
  --production-verification
```

`--provider` は `openai` または `gemini` を指定する。

`--production-verification` を指定すると、`src/runtime_client/config.py` の `ClientConfig.production_verification=True` が設定され、`main.py` の `main()` が以下を自動実行する。

- `PHANTOM_CALIBRATION_DEBUG=1` を明示的に設定していなくても等価な状態にする
- `logs/calibration_<YYYYMMDD_HHMMSS>.log` を開き、Calibration内部トレースをtee出力する
- Startup Calibration完了後（成功・失敗いずれの場合も）に `logs/root_cause_summary.txt` を生成する

なお `--production-verification` は調査用の計測支援フラグであり（`src/runtime_client/debug_sink.py` モジュールdocstring参照）、Calibrationアルゴリズム自体は変更しない。Startup Calibration完了後の通常動作（AudioBridge起動・WebSocket接続・Keyboard操作）には影響しない。

## 5.2 起動シーケンス

`main.py` の `_amain()` は、以下の順序で処理を行う（AudioBridge / WebSocket接続より前に Startup Calibration が実行される）。

1. 入力デバイス解決（`resolve_input_device`）
2. 出力デバイス解決（TTS用、`resolve_output_device_id`）
3. **Startup Calibration**（`_perform_startup_calibration`） — 本Runbookの主対象
4. TTS Provider構築
5. Keyboard Thread構築
6. AudioBridge構築・起動（Calibration結果の `RecalibrationController` を注入）
7. WebSocket接続（`RuntimeWebSocketClient.run()`）

---

# 6. Expected Output

以下は一般的な期待出力の形式です。実測値は §10 Production Verification Result を参照してください。

## 6.1 Startup Calibration（成功時）の画面出力

```
[runtime_client] Production Verification mode: debug log -> logs/calibration_<timestamp>.log
[runtime_client] Phantom Runtime Client
  target:       wss://<CLOUD_RUN_HOST>/ws?provider=openai
  input device: <入力デバイス名>
  sample rate:  16000 Hz, 1ch, block=1600 frames
  tts:          off
  (Ctrl+C or 'q' to quit)

=== Audio Calibration ===
0/25 blocks (0.0s / 2.5s)
...
=== Calibration Complete ===
Noise Floor:   <RMS>
Speech Gate:   <RMS> (= Noise Floor x 3.0, clamp[150, 2500])
Microphone:    <入力デバイス名>
```

`show_calibration_start` / `show_calibration_progress` / `show_calibration_complete`（`src/runtime_client/typed_event.py`）が描画する画面。

## 6.2 デバッグログ出力形式（`PHANTOM_CALIBRATION_DEBUG=1` 有効時）

```
[calibration-debug] Block 01 RMS=<value>
[calibration-debug] Block 02 RMS=<value>
...
[calibration-debug] Window end: sample_count=10 min=<value> max=<value> percentile_target=90 contaminated=False noise_floor=<value>
[calibration-debug] CalibrationEngine result: success=True noise_floor=<value> speech_gate=<value>
[calibration-debug] Dynamic Contamination Threshold: <value> RMS (baseline_sample_count=10)
[calibration-debug] Attempt 1 started
[calibration-debug] _run_initial_calibration start: window_seconds=2.50 window_blocks=25
...
[calibration-debug] Window end: sample_count=25 min=<value> max=<value> percentile_target=90 contaminated=False noise_floor=<value>
[calibration-debug] Attempt 1: window clean -> ObservationResult success=True noise_floor=<value> sample_count=25 attempts=1
[calibration-debug] CalibrationEngine result: success=True noise_floor=<value> speech_gate=<value>
```

先頭の `Block NN RMS=... / Window end / CalibrationEngine result` のブロックは **Baseline Observation**（`_run_baseline_observation`、window_blocks=10、contamination_threshold=+inf の無条件サンプリング）であり、続く `Attempt 1 started` 以降が **Dynamic Contamination Thresholdを適用した本Calibration**（`EnvironmentObserver` + `CalibrationEngine`）である。

## 6.3 Speech Gate導出後の通常動作

Calibration完了後、`AudioBridge` はブロック毎に `calibration_controller.active_result.speech_gate` を読み、RMSがそれを下回るブロックを送信しない（Silence Gate）。RECORDING ON時、Speech Gateを超えるブロックのみがWebSocket経由でCloud Runへ転送される。

---

# 7. Generated Files

`--production-verification` 指定時に生成されるファイル（いずれも `.gitignore` の `logs/` により追跡対象外）。

## 7.1 `logs/calibration_<YYYYMMDD_HHMMSS>.log`

`src/runtime_client/main.py` の `main()` が起動時に `debug_sink.open_session_log()` で開くセッションログ。`calibration.py` の `_debug_log()` および `main.py` の `_calibration_debug_log()` が出力する `[calibration-debug] ...` 行がすべてteeされる。Baseline Observationおよび本Calibrationの、ブロック単位のRMS値・窓終了時の統計・CalibrationEngineの入出力が記録される。

## 7.2 `logs/root_cause_summary.txt`

`main.py` の `_write_root_cause_summary()` が Startup Calibration完了直後（成功・Calibration Failedいずれも）に1回だけ生成する、要約テキストファイル。フォーマット:

```
Production Verification - Root Cause Summary
generated_at: <ISO8601タイムスタンプ>
session_log: <対応する calibration_*.log のパス>

Attempts: <試行回数>
ObservationResult: <EnvironmentObserver.result() の repr>
NoiseFloor: <導出されたNoise Floor、またはNone>
SpeechGate: <導出されたSpeech Gate、またはFallback値>
Fallback: <True/False>
Calibration: <SUCCESS/FAILED>
Resolved Input Device: <解決された入力デバイス名>
Candidate Cause: Unknown（本ファイルは事実の記録のみで、根拠なしに原因を推定しない方針。詳細は同名の calibration_*.log を参照）
```

`Candidate Cause` は常に `Unknown` 固定であり、これは仕様（`_write_root_cause_summary()` のdocstring）。原因推定はこのファイル単体では行わず、対応する `calibration_*.log` のブロック単位の記録を確認すること。

---

# 8. Success Criteria

以下をすべて満たす場合、Production Verification は **PASS** と判断する。

| 項目 | 判定基準 | 確認方法 |
|---|---|---|
| Calibration結果 | `root_cause_summary.txt` の `Calibration: SUCCESS` | §7.2ファイル |
| Fallback | `Fallback: False`（固定値700 RMSへのフォールバックが発生していない） | §7.2ファイル |
| Noise Floor / Speech Gate | いずれも `None` ではなく、実測値が記録されている | §7.2ファイル |
| Attempts | `DEFAULT_MAX_ATTEMPTS`（3）以内で成功している | §7.2ファイル（`Attempts` フィールド） |
| Resolved Input Device | `--input-device` を指定した場合、意図したデバイス名が記録されている | §7.2ファイル |
| 起動画面 | `=== Calibration Complete ===` が表示され、Noise Floor / Speech Gate / Microphone が表示される | §6.1相当の標準出力 |
| WebSocket接続 | Calibration完了後、`RuntimeWebSocketClient` がCloud Runへ接続し、Control Eventが送受信できる | 標準出力・Keyboard操作（`s` キーでの状態表示等） |
| RECORDING | `r` キーでRECORDING ONにした状態で発話し、Speech Gateを超えるブロックのみが転送される | AudioBridgeログ・Server側の応答（`transcript` Typed Event等） |
| Gemini STT | 発話が実際にtranscriptとして返る（Whisper/OpenAI STTではなくGemini STT経由の場合） | Runtime Client画面の `◎ <transcript>` 表示、`transcript` Typed Event |
| Gemini LLM | transcriptに対して `reply` Typed Eventが返る | Runtime Client画面の `[JP] <reply>` 表示、`reply` Typed Event |
| Dashboard | `GET /dashboard` が最新の `DashboardResult` を返す（未投入時404は許容） | `docs/RUNBOOK_DASHBOARD.md` §5 |
| Conversation Traceability | `GET /dashboard` / `GET /` に `conversation_line` / `speaker` / `transcript` が反映される | `docs/RUNBOOK_DASHBOARD.md` §8A.5 |
| Verification Runtime | Runtime Eventに対する `gap_detected` / `fallback_detected` 判定が生成されている（`DashboardResult` に含まれる） | `docs/RUNBOOK_DASHBOARD.md` §6 `GET /dashboard` 期待結果 |
| Trust Runtime | `DashboardResult` に `trust_score` / `trust_level` / `human_review_required` が生成されている | 同上 |
| **1011（keepalive ping timeout）** | Operator E2E全体を通して **0件** | §11.4 WebSocket健全性確認、`Session TEARDOWN` 系Runtime Trace |
| **409（reconnect conflict）** | Operator E2E全体を通して **0件** | 同上 |
| Healthz正常性 | `GET /healthz` の応答時間が常時 **数十ms以内**（目安: 最大約10ms、§11.3） | §11.3 Health Check |
| Session Lifecycle Trace正常性 | `Session START` → `DISCONNECT` → `TEARDOWN START` → `TEARDOWN END` が過不足なく対になっている（欠落・二重実行がない） | §11.2 Session Lifecycle、`PHANTOM_TRACE=1` |

---

# 9. Troubleshooting

## 9.1 固定Contamination Threshold（150 RMS）による Calibration Failed

**現象**: `logs/calibration_*.log` で全Attemptが `-> contamination` となり、`root_cause_summary.txt` が `Calibration: FAILED` / `Fallback: True` / `SpeechGate: 700.0` を記録する。

**原因**: 修正前の実装は `EnvironmentObserver` の `contamination_threshold` を `DEFAULT_NOISE_FLOOR_SAFETY_FLOOR = 150.0`（固定値）で初期化していた。実機（特に外部マイク）では室内ノイズフロアが150 RMSを恒常的に超えるケースがあり、この場合すべての観測窓が「汚染」と判定され、`DEFAULT_MAX_ATTEMPTS`（3）を使い切って `success=False` となる。

実測例（本機、修正適用前）:

```
Attempts: 3
ObservationResult: ObservationResult(success=False, noise_floor=None, sample_count=25, attempts=3)
SpeechGate: 700.0
Fallback: True
Calibration: FAILED
Resolved Input Device: 外部マイク
```

**対処（適用済み修正）**: `src/runtime_client/main.py` に `_run_baseline_observation()` / `_derive_dynamic_contamination_threshold()` を追加。Calibration本番実行の前に、無条件（`contamination_threshold=+inf`）で短い Baseline Observation（10ブロック）を実施し、その結果に `CalibrationEngine.calibrate()`（既存の `clamp(noise_floor * 3.0, 150, 2500)` 式を再利用）を適用して、その環境専用の Dynamic Contamination Thresholdを導出する。この値を本Calibrationの `contamination_threshold` として使用することで、固定値150 RMSでは汚染と誤判定されていた実際の室内ノイズフロアを正しく「クリーンな窓」として認識できるようになる。

修正適用後の実測例（本機、同一環境・同一マイク）:

```
Dynamic Contamination Threshold: 401.9 RMS (baseline_sample_count=10)
Attempts: 1
ObservationResult: ObservationResult(success=True, noise_floor=163.5..., sample_count=25, attempts=1)
SpeechGate: 490.5...
Fallback: False
Calibration: SUCCESS
```

Baseline取得自体が `shutdown` により中断された場合は `_derive_dynamic_contamination_threshold()` が `DEFAULT_NOISE_FLOOR_SAFETY_FLOOR`（150.0、修正前と同じ動作）にフォールバックするため、最悪の場合でも修正前より悪化しない。

## 9.2 Cloud Run Cold Start

**現象**: Runtime Client起動直後のWebSocket接続がタイムアウト、または初回リクエストの応答が遅い。

**対処**: `RuntimeWebSocketClient` は `--max-reconnect-attempts` / `--backoff-base-seconds`（デフォルト3回・1.0秒基準の指数バックオフ）で再接続を試みる。Cloud Runの最小インスタンス数が0の場合、初回リクエストでコンテナ起動（Cold Start）が発生し数秒〜十数秒を要することがある。Health Check（`docs/RUNBOOK.md` §10.1 `/healthz`）を事前に一度叩いてコンテナをウォームアップしておくと再現しにくい。

## 9.3 マイクデバイスが見つからない

**現象**: `--input-device` に指定した名前・インデックスが解決できず、`main.py` が警告を出してシステムデフォルト入力にフォールバックする。

**対処**: `python -m runtime_client --list-devices`（§4）で正確なデバイス名・インデックスを再確認し、`--input-device` に部分一致する文字列、またはインデックス番号（文字列として）を指定する。

## 9.4 `keepalive ping timeout`（close 1011）→ reconnect → `409`

**現象**: STT/LLMが数ラウンド正常動作した後、`[websocket_client] connection closed (... keepalive ping timeout ...)` に続き `sent 1011 (internal error)` が出力され、直後の自動reconnectが `handshake rejected (409)` で拒否される（または応答自体がタイムアウトする）。Operator E2Eが継続不能になる。

**対処**: 詳細な切り分け手順・原因・修正内容は §11 TransportGateway Session Lifecycle Verification を参照。2026-07-12時点で根本原因（TransportGatewayのイベントループが `session_teardown()` の同期ブロッキングで最大約12秒停止する設計欠陥）は修正済み（`docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md`）。本現象を観測した場合は、まず §11.3 Health Checkの手順で `GET /healthz` の応答時間を確認し、イベントループが実際にブロックしているかどうかを最初に切り分けること。

---

# 10. Production Verification Result (2026-07-10)

本節は、本機・実機マイク・実Cloud Run環境を用いて実際に実施した検証結果を記録する。

## 10.1 検証環境

- 実Cloud Run（デプロイ済みProductionサービス）
- 実機マイク（外部マイク）
- `python -m runtime_client --production-verification` による実行

## 10.2 確認結果

- **Startup Calibration**: 実機マイクを用いた Baseline Observation が成功した（`window_blocks=10`, `contamination_threshold=+inf`, `noise_floor=133.96 RMS`）。
- **Dynamic Contamination Threshold**: Baseline結果から `401.9 RMS` が導出された。修正前の固定値（150.0 RMS）を、実機環境の実測ノイズフロアに基づいて置き換える値である。
- **Calibration: SUCCESS**（Attempt 1/3、`noise_floor=163.5 RMS`, `speech_gate=490.5 RMS`）。
- **Fallback: False**（固定Fallback値700 RMSは使用されなかった）。
- 同一マイク・同一環境で、修正適用前のセッションは3/3 Attemptすべてが汚染判定となり `Calibration: FAILED` / `Fallback: True`（`speech_gate=700.0` の固定Fallback値）であったことを記録済みである。この比較により、**Production Startup Calibrationブロッカーに対する修正（Dynamic Contamination Threshold導入）が、実機マイクを用いた実測ベースで有効であることを確認した**（cfb6d71 “fix(runtime): resolve Production Startup Calibration blocker with adaptive threshold”）。
- **WebSocket接続**: Calibration完了後、Runtime Clientは実Cloud Runサービスへ WebSocket接続し、正常に動作した。この項目はオペレーターによる実施時確認に基づく記録であり、Startup Calibrationの実測値（上記各項目）のように `calibration_<timestamp>.log` / `root_cause_summary.txt` として保存されたログを伴わない（Startup Calibrationは `main.py` の `_amain()` 内でAudioBridge/WebSocket接続より前に完了する処理であり、採取済みログはCalibration部分のみをカバーする）。

## 10.3 実測データの出典

Startup Calibrationに関する実測値（10.2の各項目）は、以下のファイル（本リポジトリの作業ツリーに実在する記録。`.gitignore` の `logs/` により追跡対象外）に基づく。

- 修正適用後（成功）: `src/logs/calibration_20260710_185125.log`, `src/logs/root_cause_summary.txt`
- 修正適用前（失敗、比較用）: `logs/calibration_20260710_132730.log`, `logs/root_cause_summary.txt`

---

# 11. TransportGateway Session Lifecycle Verification

本節は、`src/runtime/transport_gateway.py`（Cloud Run Compatibility ShellのWebSocket/セッション終了処理）を対象に、Operator（Production運用者）が **① 正常稼働の確認、② 障害発生時の迅速な切り分け** を行うためのRunbookである。2026-07-12に修正した「Session Teardownがイベントループをブロックし `1011` → reconnect競合 → `409` に至る」障害（`docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md`）を踏まえて追加した。

## 11.1 Root Cause（背景）

`TransportGateway` は `/healthz` と全ての `/ws` セッションを**単一のasyncioイベントループ**で処理する。セッション終了処理（`reader_thread.join()` / `RuntimeSession.teardown()` の `child.wait()` / `child.kill()`）を、このイベントループ上で**同期ブロッキング**のまま実行すると、そのブロック中は同じループを共有する他の全ての処理（Ping/Pong keepalive、新規接続のハンドシェイク）が進行できなくなる。

```
Session Teardown が同期ブロッキングでイベントループを占有
        │
        ▼
Ping/Pong keepalive が応答不能
        │
        ▼
どちらかの側で keepalive ping timeout → close code 1011
        │
        ▼
再接続要求がイベントループの空き待ちで滞留・タイムアウト
        │
        ▼
ループ再開後、先に滞留していた（既に見捨てられた）接続要求が
空いたセッションスロットを先に掴んでしまう
        │
        ▼
オペレーターの本当の再接続が 409（handshake rejected）
```

2026-07-12時点でこの原因は修正済み（`reader_thread.join()` / `session_teardown()` を `loop.run_in_executor()` でイベントループ外のバックグラウンドスレッドへ退避）。本節はその修正が正しく機能していることを、Production環境で運用者が確認するための手順である。

## 11.2 正常なSession Lifecycle状態遷移

1接続につき、以下の5状態が**過不足なく・重複なく**発生する。

```
CONNECTED
   ↓   (WebSocketが切断される)
DISCONNECTING
   ↓   (共有スロット [_active_connection / _active_session] をイベントループ側で解放してから
        バックグラウンドスレッドへteardownを委譲する)
TEARDOWN START
   ↓   (reader_thread.join() → RuntimeSession.teardown()
        [SIGINT → child.wait() → 必要ならSIGKILL → child.wait()] を実行)
TEARDOWN END
   ↓
CLOSED
```

### Runtime Traceでの確認方法

`PHANTOM_TRACE=1`（必要に応じて `PHANTOM_TRACE_FILE=<path>` でファイル出力）を `cloud_run_shell` 起動時に設定すると、上記の状態遷移が構造化ログとして出力される（`src/runtime_trace.py` / `src/runtime/transport_gateway.py`）。

```bash
PORT=8080 PHANTOM_TRACE=1 PHANTOM_TRACE_FILE=logs/trace.jsonl \
python -m runtime.cloud_run_shell -- --profile default --mode light --no-color --audio-source fd
```

正常時の出力例（1セッション分、`jq` で整形）:

```
[event:Session START]           session_id=srv-<pid>  lifecycle_state=CONNECTED
[event:Session DISCONNECT]      session_id=srv-<pid>  lifecycle_state=DISCONNECTING
[event:Session TEARDOWN START]  session_id=srv-<pid>  lifecycle_state=TEARDOWN
[event:Session TEARDOWN END]    session_id=srv-<pid>  lifecycle_state=CLOSED
```

**確認ポイント**:

- 4イベント（`Session START` / `DISCONNECT` / `TEARDOWN START` / `TEARDOWN END`）が同一 `session_id` で**すべて出現**していること。`TEARDOWN START` はあるが `TEARDOWN END` が出ていない場合、`session_teardown()`（子プロセスのSIGINT/SIGKILL待ち）が長時間応答していない可能性がある。
- 再接続が発生した場合、**新しい** `Session START` が**古い** `Session TEARDOWN START`/`END` より前後どちらのタイミングで出ていても正常（イベントループがブロックされていなければ、新セッションの受付と旧セッションの後片付けは並行して進む。むしろ新 `Session START` が旧 `TEARDOWN START` より先に出ることは、スロットが正しく即座に解放された証拠であり望ましい）。
- 同一 `session_id` で `TEARDOWN START` / `TEARDOWN END` が2回以上出現している場合は異常（二重実行）であり、直ちに調査すること。

## 11.3 Health Check（`GET /healthz`）

```bash
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" <CLOUD_RUN_URL>/healthz
```

| 状態 | 期待応答時間 | 備考 |
|---|---|---|
| **正常時** | **最大約10ms**（Operator Validation実績: 1196サンプル中 最大10.77ms / 平均2.22ms、20分間の継続稼働・4回のreconnectを含む） | イベントループが常に応答可能な状態 |
| **イベントループブロック時（異常）** | **数秒〜十数秒**（Operator Validation実績: 修正前コードで最大2003.8ms、5秒タイムアウトでの応答失敗も観測） | `session_teardown()` 等の同期ブロッキング処理がイベントループを占有している可能性が高い |

継続監視する場合は1秒間隔程度でポーリングし、応答時間の推移を記録することを推奨する（Production Verification実施時の具体例は §11.7 参照）。

## 11.4 WebSocket健全性確認

| 状態 | 内容 |
|---|---|
| **正常時** | ・`1011`（keepalive ping timeout）発生なし ・`409`（reconnect conflict）発生なし ・Reconnectそのものが不要（切断が起きない）、または発生しても即座（1試行）で成功する |
| **異常時: `1011` (keepalive ping timeout)** | Runtime Client側ログに `connection closed (... keepalive ping timeout ...)` / `sent 1011 (internal error)` が出力される。**確認ポイント**: (1) 直前・直後の `GET /healthz` 応答時間（§11.3）でイベントループのブロックを疑う、(2) Runtime Traceで同時刻の `Session TEARDOWN START`〜`END` の間隔を確認する（長時間開いていればブロックの直接証拠）、(3) 子プロセス（`phantom_runtime.py`）がSIGINTに応答せず残留していないか `ps aux \| grep phantom_runtime.py` で確認する。 |
| **異常時: `409` (reconnect conflict)** | Runtime Client側ログに `fatal: handshake rejected (409); not retrying` が出力される。**確認ポイント**: (1) 直前に `1011` または他の切断が発生していないか確認する（`409`単独で発生することは通常ない）、(2) Runtime Traceで、拒否された再接続の直前に**別の** `Session START` が記録されていないか確認する（記録されていれば、見捨てられた古い接続がスロットを先に掴んだ証拠）、(3) `GET /healthz` の応答が同時刻に遅延していないか確認する。 |

## 11.5 Runtime Trace 確認対象一覧

Production運用中の切り分けで確認すべきRuntime Traceイベント（`PHANTOM_TRACE=1`）:

| Trace Stage | 意味 | 主な確認目的 |
|---|---|---|
| `Session START` | 新しいWebSocketセッション（Runtime子プロセス）が確立された | セッションの生成タイミング・重複がないか |
| `Session DISCONNECT` | WebSocket接続が切断された（切断検知の瞬間） | 切断発生時刻の特定 |
| `Session TEARDOWN START` | 子プロセスの終了処理（SIGINT/SIGKILL/pipe close）が開始された | ブロッキング調査の起点 |
| `Session TEARDOWN END` | 子プロセスの終了処理が完了した | `TEARDOWN START`との間隔＝実際のteardown所要時間 |
| `Speech START` | Server VAD/STTが発話区間の開始を検知した | STT起動タイミング（§11.7 Gemini STT確認） |
| `Speech END` | 発話区間が確定した（`reason` フィールドを伴う） | 下記 `reason=silence` / `reason=force` を参照 |
| `reason=silence` | 無音区間の検出により発話が確定した（正常な区切り） | Server VADが正しく無音を検出できているか |
| `reason=force` | `--max-sec` 上限到達により強制確定した（無音検出に到達できていない） | `reason=silence` が一度も出ない場合、Client Speech GateとServer VADの不整合を疑う（`docs/SPEECH_GATE_SERVER_VAD_VALIDATION_REPORT.md` 参照） |

## 11.6 再発防止（コーディング規約）

**`TransportGateway`（および今後asyncioイベントループ上で動作するコードすべて）に、新たな同期ブロッキング処理を追加しないこと。**

イベントループ上（`async def` の中で `await`/`run_in_executor`/`asyncio.to_thread` を介さない直接呼び出し）で以下を行うことを禁止する。

- `Thread.join()`
- `subprocess.Popen.wait()`
- `subprocess.Popen.kill()` 直後の待機を含む一連の終了処理
- `time.sleep()`
- その他、OSレベルでブロックしうる同期I/O呼び出し全般

これらがどうしても必要な場合は、必ず以下のいずれかでイベントループ外のスレッドへ退避すること。

- `loop.run_in_executor(None, func, *args)`（`src/runtime/transport_gateway.py` `_finish_disconnect()` で採用した方式。デフォルトの `ThreadPoolExecutor` を使用）
- `asyncio.to_thread(func, *args)`（同等の効果を持つ、より新しいAPI）

レビュー観点: `TransportGateway._handler()` のような、複数接続で共有される単一イベントループ上のコルーチンに変更を加える場合、変更後のコードに `.join(`, `.wait(`, `.kill(` 等の同期呼び出しが `await`/`run_in_executor`/`to_thread` を介さずに追加されていないか、必ず確認すること。`tests/test_transport_gateway_session_lifecycle.py` がこの種の回帰を検出する（`test_active_slot_is_free_immediately_even_while_teardown_is_still_running` / `test_teardown_runs_on_a_different_thread_than_the_event_loop`）。

## 11.7 Production Verification Operator E2E チェック項目

15〜30分間のOperator E2Eを実施する際は、§8 Success Criteriaの既存項目に加え、以下を確認する。

- [ ] Gemini STT: 発話が正しくtranscriptとして返る
- [ ] Gemini LLM: transcriptに対して妥当なreplyが返る
- [ ] Dashboard: `GET /dashboard` / `GET /` が最新の `DashboardResult` を表示する（`docs/RUNBOOK_DASHBOARD.md` §5）
- [ ] Conversation Traceability: `conversation_line` / `speaker` / `transcript` がDashboardに反映される（`docs/RUNBOOK_DASHBOARD.md` §8A）
- [ ] Verification Runtime: `gap_detected` / `fallback_detected` の判定がDashboardResultに含まれる
- [ ] Trust Runtime: `trust_score` / `trust_level` / `human_review_required` がDashboardResultに含まれる
- [ ] `1011`（keepalive ping timeout）: **0件**
- [ ] `409`（reconnect conflict）: **0件**
- [ ] Healthz正常: `GET /healthz` 応答時間が継続して数十ms以内（目安: 最大約10ms）
- [ ] Session Lifecycle Trace正常: `Session START`/`DISCONNECT`/`TEARDOWN START`/`TEARDOWN END` が全て対になっている（§11.2）

2026-07-12の修正検証時の実績（`docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md` §4.3）: 20分間・実Gemini STT 248件・実Gemini LLM 209件・強制切断/再接続4回の条件下で、上記すべてを満たすことを確認済み。

---

# 制約

本Runbookの作成にあたり、以下は変更していない。

- `README.md`
- `docs/ROADMAP_V10.md`
- `docs/MIGRATION_MATRIX.md`

新規作成したファイルは本ファイル（`docs/RUNBOOK_PRODUCTION_VERIFICATION.md`）のみ。
