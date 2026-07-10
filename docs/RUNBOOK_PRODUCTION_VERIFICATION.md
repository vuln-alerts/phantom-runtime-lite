# Production Verification Runbook — P5-4 Adaptive Runtime Calibration

**Version:** 1.0
**Target Feature:** P5-4 Adaptive Runtime Calibration（`src/runtime_client/calibration.py`, `src/runtime_client/main.py`）
**Related:** `docs/RUNBOOK.md`（Cloud Run構築・デプロイ・運用手順）, `docs/MIGRATION_MATRIX.md`, `docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md`
**Last Updated:** 2026-07-10

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

# 制約

本Runbookの作成にあたり、以下は変更していない。

- `README.md`
- `docs/ROADMAP_V10.md`
- `docs/MIGRATION_MATRIX.md`

新規作成したファイルは本ファイル（`docs/RUNBOOK_PRODUCTION_VERIFICATION.md`）のみ。
