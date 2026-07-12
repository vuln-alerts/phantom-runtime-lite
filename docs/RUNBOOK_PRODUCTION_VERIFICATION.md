# Production Verification Runbook — Operator Runbook

**Version:** 2.1
**Target Feature:** Production Verification 全体（Cloud Run Runtime, Runtime Client, Dashboard/FastAPI, TransportGateway Session Lifecycle）
**Audience:** Operator（本Runbookのみを見て、Production Verification / Operator E2E を最初から最後まで実施できることを目的とする）
**Related:** `docs/RUNBOOK.md`（Cloud Run構築・デプロイ手順）, `docs/RUNBOOK_DASHBOARD.md`（Dashboard機能の詳細仕様）, `docs/RUNBOOK_RUNTIME_VERIFICATION.md`（Verification/Trust/Dashboard Runtimeの内部呼び出し仕様）, `docs/H4_RUNTIME_EVENT_CONTRACT.md`, `docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md`, `docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md`
**Last Updated:** 2026-07-12

---

# 変更履歴

- **v2.1（2026-07-12）**: Release Candidate Review指摘の是正。Dashboard APIポートをREADME/`docs/RUNBOOK_DASHBOARD.md`と統一（既定8081、§4.1参照）。`ENABLE_QUEUE_METRICS`の環境変数名誤記を修正（§7）。`.env`/ローカル実行コマンドの誤り（`python -m src.phantom_runtime`は実行不能）を修正。本ファイル末尾「制約」節の記述を、実際に同時改訂した他ファイルと整合するよう修正。ソースコードは変更していない。
- **v2.0（2026-07-12）**: Operator向けに全面再構成。§4 Dashboard起動を新規追加（コード確認済み）。旧版（v1.1）の内容（§11 TransportGateway Session Lifecycle Verification、2026-07-10実施結果等）は本版の §2/§8/§9/§10 に統合し、削除していない。
- **v1.1（2026-07-12）**: P5-4 Adaptive Runtime Calibration + TransportGateway Session Lifecycle Verification（§11）を追加。

---

# 1. Prerequisites

## 1.1 必要条件

| 項目 | 内容 | 根拠 |
|---|---|---|
| Python | 3.13+（ローカル直接実行の場合）。Dockerイメージは `python:3.14-slim`（`Dockerfile` ARG `BASE_IMAGE`） | `Dockerfile` |
| 仮想環境 | venv推奨（`requirements.txt` の依存関係を分離） | 既存慣習（本Runbook §1.3） |
| `.env` | リポジトリルート直下（`phantom_runtime.py` 内 `load_dotenv(_os.path.join(_REPO_ROOT_EARLY, ".env"))`、スクリプトの場所を基準に解決するためカレントディレクトリに依存しない）。ローカルで `src/` から `python -m phantom_runtime` を直接実行する場合、またはRuntime Clientをローカル実行する場合に読み込まれる（リポジトリルートから `python -m src.phantom_runtime` を実行するのは誤りで、`ModuleNotFoundError: No module named 'provider'` になる。`src/__init__.py` が存在せず、`provider`/`audio`等をトップレベルパッケージとして相対importしているため）。**Dockerコンテナ内では読み込まれない**（`.dockerignore` が `.env` を除外するため、コンテナは `docker run -e` で渡された環境変数のみを使用する）。実Cloud Run本番環境でも `.env` は使用されず、`gcloud run services update --update-env-vars` で設定した値のみが使われる（`docs/RUNBOOK.md` §6） | `src/phantom_runtime.py`（`load_dotenv` 呼び出し箇所）、コミット `1bdd922` |
| `OPENAI_API_KEY` | 必須。Whisper STT（Provider選択に関わらず常時使用）、および `provider=openai` セッションのLLM応答生成に使用 | `README.md` "Required Environment Variables" |
| `GEMINI_API_KEY` | `provider=gemini` セッションを検証する場合のみ必須 | 同上 |
| BlackHole | 物理マイクの代わりに、Zoom/Meet/Teams等のアプリ音声、または合成音声（`say`コマンド等）をRuntime Clientの入力として使うための仮想オーディオデバイス。本リポジトリはBlackHole自体のインストール手順を持たない（サードパーティ製macOSドライバ）。物理マイクで実発話を行う場合は不要 | `src/runtime_client/config.py:85`, `src/runtime_client/tts.py:15-16`, `docs/RUNBOOK.md` §11.6 |
| マイク | 物理マイク（内蔵 or 外部USB）。BlackHoleを使わない場合はこちらが必須 | `docs/RUNBOOK_PRODUCTION_VERIFICATION.md`（本ファイル）§1.2 |
| ネットワーク | Runtime ClientからCloud Run URL（`wss://`）へ到達可能なこと | 本Runbook §3 |

## 1.2 確認方法

```bash
# Python バージョン
python3 --version

# venv構築（初回のみ）
cd /path/to/phantom-runtime-lite
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` に含まれる主要ライブラリ（`docs/RUNBOOK_RUNTIME_VERIFICATION.md` §3 で確認済み）:

```text
openai>=1.30.0
google-genai>=1.0.0
sounddevice>=0.4.6
numpy>=1.24.0
python-dotenv>=1.0.0
websockets>=13.0
fastapi>=0.110.0
```

**`uvicorn` は `requirements.txt` に含まれていない。** §4 Dashboard起動で使用するため、別途 `pip install uvicorn` が必要（§3 参照）。

```bash
# .env の確認（値そのものは画面に出さないこと）
ls -la .env
grep -c "OPENAI_API_KEY\|GEMINI_API_KEY" .env
```

```bash
# 入力デバイス（マイク / BlackHole）の確認
cd src
python -m runtime_client --list-devices
```

期待される出力形式（実機構成により内容は変わる。`src/runtime_client/main.py` `_list_input_devices()` が `sounddevice.query_devices()` の `max_input_channels > 0` のデバイスのみを列挙する）:

```
[runtime_client] available input devices:
  [<index>] <デフォルトマイクの名称（例: MacBook Pro Microphone）>
  [<index>] <外部マイクの名称>
  [<index>] <BlackHole 2ch>（インストール済みの場合のみ表示される）
```

`--input-device` にはデバイス名の部分一致文字列、またはインデックス番号を指定できる（`src/runtime_client/audio_bridge.py` `resolve_input_device()`）。指定したデバイスが見つからない場合、システムデフォルト入力にフォールバックし警告を表示する。

## 1.3 ネットワーク確認

```bash
curl -i <CLOUD_RUN_URL>/healthz
```

詳細な期待結果は §3 Health Check を参照。

---

# 2. Runtime起動

「Runtime」は Cloud Run 上で稼働する `runtime.cloud_run_shell`（`GET /healthz` + `WS /ws` を公開する Cloud Run Compatibility Shell、`phantom_runtime.py` を子プロセスとして起動する）を指す。本番のProduction Verificationでは、以下いずれかの方法でRuntimeを用意する。

## 2.1 パターンA: 実Cloud Run（本番、推奨）

デプロイ済みのCloud Runサービスが既に存在することを前提とする（未デプロイの場合の構築・デプロイ手順は `docs/RUNBOOK.md` §4-§9を参照。本Runbookの対象外）。

```bash
gcloud run services describe phantom-runtime-lite \
  --region asia-northeast1 \
  --format="value(status.url)"
```

期待

```
https://phantom-runtime-lite-<PROJECT_NUMBER>.asia-northeast1.run.app
```

以降、この値を `<CLOUD_RUN_URL>` として使用する（WebSocket接続時は `https://` を `wss://` に読み替える。`docs/RUNBOOK.md` §5.2）。

Cloud RunはHTTPSのオンデマンド起動基盤であり、Operatorが明示的に「起動コマンド」を実行する必要はない。初回リクエスト時にコンテナが起動する（Cold Start、§10.2参照）。

**期待ログ**（`gcloud run services logs read phantom-runtime-lite --region asia-northeast1 --limit=50`、`docs/RUNBOOK.md` §10.2）:

```
transport gateway listening
runtime child started
readiness = healthy
```

## 2.2 パターンB: ローカルDocker（gcloud不要、`docs/RUNBOOK.md` §7.1で検証済み）

実Cloud Runへの接続権限がない場合の代替。同一の `Dockerfile` をローカルで起動する。

```bash
docker build --platform linux/amd64 -t phantom-runtime-lite:local .

docker run --rm --name phantom-local -p 8080:8080 \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  -e PORT=8080 \
  phantom-runtime-lite:local
```

以降、`<CLOUD_RUN_URL>` の代わりに `http://localhost:8080` を使用する。

**注意（ポート競合）**: FastAPI（`api.api_server:app`）はuvicornの既定ポートが `8080` であり、パターンBのRuntime（同じく8080）と衝突する。この衝突を避けるため、§4 Dashboard起動では常に `--port 8081` を使う（パターンA〈実Cloud Run、リモート〉の場合はそもそも8080との競合は発生しないが、手順を1本化するためパターンA/Bどちらでも8081を使う）。

**期待ログ**（コンテナの標準出力）:

```
transport gateway listening
runtime child started
readiness = healthy
```

## 2.3 正常状態の確認

いずれのパターンでも、次の §3 Health Check で `200 OK` が返ることを起動完了の判定基準とする。

---

# 3. Health Check

```bash
curl -i <CLOUD_RUN_URL>/healthz
```

期待

```
HTTP/2 200
ok
```

（ローカルDockerの場合は `curl -i http://localhost:8080/healthz` で同様に `HTTP/1.1 200 OK` / `ok`）

異常時

| Status | 原因 |
|---|---|
| 404 | URL誤り |
| 503 | Runtime起動中、またはShutdown中 |

## 3.1 応答時間の継続監視（TransportGateway健全性）

```bash
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" <CLOUD_RUN_URL>/healthz
```

| 状態 | 期待応答時間 | 備考 |
|---|---|---|
| **正常時** | 最大約10ms程度（Operator Validation実績: 1196サンプル中最大10.77ms / 平均2.22ms、20分間の継続稼働・4回のreconnectを含む条件下） | イベントループが常に応答可能な状態 |
| **異常時（イベントループブロック）** | 数秒〜十数秒（Operator Validation実績: 修正前コードで最大2003.8ms、5秒タイムアウトでの応答失敗も観測） | `session_teardown()` 等の同期ブロッキング処理がイベントループを占有している可能性。§8/§10参照 |

継続監視する場合は1秒間隔程度でポーリングし、応答時間の推移を記録することを推奨する。

---

# 4. Dashboard起動

Dashboard（`GET /dashboard` JSON, `GET /` HTML）は `src/api/api_server.py` が提供するFastAPIアプリケーションであり、**Cloud Run Runtime（§2）とは完全に独立したプロセス**である。README記載の通り、Cloud Runに現在デプロイされているのはRuntime本体（`/healthz`, `/ws`）のみで、Dashboard/FastAPIは本番Cloud Runにはデプロイされておらず、**ローカルで別途起動する**（コード・ドキュメント確認済み: `README.md` "Architecture", `docs/RUNBOOK_DASHBOARD.md` §2.3）。

## 4.1 起動コマンド

Dashboard APIは既定で **`8081`** を使う（`README.md` "Quick Start"/"Dashboard API" と統一）。理由: パターンB（§2.2 ローカルDocker）でRuntimeがローカル8080番を使う構成が本Runbookの主経路であり、Dashboard APIを8080のままにするとポート競合（`[Errno 48] Address already in use`）で起動できない。パターンA（実Cloud Run）の場合は8080との競合は発生しないが、手順を1本化しどちらのパターンでも同じコマンドで再現できるようにするため、Dashboard APIは常に8081を使う。

```bash
pip install uvicorn   # requirements.txt に含まれていないため別途必要
cd src
python -m uvicorn api.api_server:app --host 127.0.0.1 --port 8081
```

## 4.2 URL

| 用途 | URL |
|---|---|
| ブラウザ表示（HTML） | `http://127.0.0.1:8081/` |
| JSON取得 | `http://127.0.0.1:8081/dashboard` |
| このFastAPIプロセス自体のヘルスチェック | `http://127.0.0.1:8081/health`（Runtime側の `/healthz` とはパスも別プロセスである点も異なるので混同しないこと） |
| データ投入 | `POST http://127.0.0.1:8081/aggregate` |

（`api/api_server.py` に定義されているルートは上記4つのみ。`/events` `/verification` `/trust` `/timeline` は未実装で `404` になる — `docs/RUNBOOK_RUNTIME_VERIFICATION.md` §2.3/§6実測済み）

## 4.3 依存するRuntime/API

- **§2 Runtime（Cloud Run Runtime）への依存はない。** `api_server.py` はCloud Run Runtime・Provider・Whisperを一切import/呼び出ししない（コード確認、`api_server.py` docstring）。
- **Cloud Run Runtimeが発行するTyped Eventを自動的にDashboardへ流し込む常駐コンシューマは、本リポジトリに存在しない**（`docs/RUNBOOK_RUNTIME_VERIFICATION.md` §2.3、`docs/H4_10_VALIDATION_REPORT.md` §7 Remaining Risksに明記された既知のギャップ）。したがってOperator E2E中にRuntime側で実際に会話が進んでも、Dashboardの表示内容は**自動的には更新されない**。
- Dashboardへデータを投入する唯一の経路は `POST /aggregate` であり、以下のいずれかで手動またはスクリプト経由で駆動する。
  - `scripts/post_dashboard_event.py`（§4.4）
  - Python REPLで `RuntimePipelineOrchestrator().run(raw_event)` を呼び出し、その結果を `curl -X POST` で送信する（`docs/RUNBOOK_DASHBOARD.md` §5 Step2-3）

## 4.4 起動確認

```bash
curl -s -w "\n%{http_code}\n" http://127.0.0.1:8081/dashboard
# → 404（まだ何も投入していない場合。異常ではない）

curl -s http://127.0.0.1:8081/
# → "No DashboardResult yet. POST an EventAggregate to /aggregate, then reload this page." のHTML
```

## 4.5 正常起動ログ

`api_server.py` 自体は起動時の独自ログを出力しない（コード確認、`app = FastAPI(...)` の定義に `startup` イベントハンドラや `print`/`logging` 呼び出しは存在しない）。したがって観測できるのは **uvicorn標準の起動ログ**のみ。

```
INFO:     Started server process [<pid>]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8081 (Press CTRL+C to quit)
```

## 4.6 データ投入とブラウザ確認

`scripts/post_dashboard_event.py` の `--url` 既定値は `http://127.0.0.1:8080` である（本Runbookの既定8081とは異なる）ため、8081で起動した場合は `--url` を明示する。

```bash
python scripts/post_dashboard_event.py --url http://127.0.0.1:8081
```

投入する `raw_event` は次の優先順位で決まる（`scripts/post_dashboard_event.py`）。

1. `--input PATH`（JSONファイル）
2. 標準入力（パイプ）
3. どちらも無ければ組み込みサンプルイベント

```bash
curl -s http://127.0.0.1:8081/dashboard
```

ブラウザで `http://127.0.0.1:8081/` を開くと、`GET /`のHTML（`src/api/templates/dashboard.html`）が表形式で表示される。

## 4.7 停止方法

`Ctrl+C`（uvicornのSIGINT/SIGTERMハンドリングで即座に停止。`docs/RUNBOOK_RUNTIME_VERIFICATION.md` §4.3実測）。

---

# 5. Runtime Client起動

`src/` から実行する。`--url` と `--provider` は必須（`--list-devices`/`--list-output-devices` 指定時を除く。`src/runtime_client/config.py`）。

## 5.1 Gemini

```bash
cd src
PHANTOM_CALIBRATION_DEBUG=1 \
python -m runtime_client \
  --url <CLOUD_RUN_URL> \
  --provider gemini \
  --input-device "<入力デバイス名>" \
  --production-verification
```

## 5.2 OpenAI

```bash
cd src
PHANTOM_CALIBRATION_DEBUG=1 \
python -m runtime_client \
  --url <CLOUD_RUN_URL> \
  --provider openai \
  --input-device "<入力デバイス名>" \
  --production-verification
```

`--production-verification` を指定すると、`src/runtime_client/config.py` の `ClientConfig.production_verification=True` が設定され、`main.py` が `logs/calibration_<YYYYMMDD_HHMMSS>.log`（Calibration内部トレースのtee出力）と `logs/root_cause_summary.txt`（Startup Calibration完了後の要約）を自動生成する。Calibrationアルゴリズム自体・通常動作には影響しない。

## 5.3 期待ログ（起動画面、Provider共通）

```
[runtime_client] Production Verification mode: debug log -> logs/calibration_<timestamp>.log
[runtime_client] Phantom Runtime Client
  target:       wss://<CLOUD_RUN_HOST>/ws?provider=<openai|gemini>
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

Calibration完了後、`RuntimeWebSocketClient` がCloud RunへWebSocket接続する。接続失敗時の挙動は §10 Troubleshooting を参照。

---

# 6. Operator E2E

15〜20分間、実発話（または§1.1のBlackHole経由音声）でRuntime Client（Gemini / OpenAI いずれか、または両方を別セッションで）を継続稼働させる。

## 6.1 実施方法

1. §2〜§3 でRuntimeが `healthz` 200を返すことを確認する
2. §5 の手順でRuntime Clientを起動し、`=== Calibration Complete ===` を確認する
3. `r` キーでRECORDING ONにする（`src/runtime_client/keyboard_bridge.py`）
4. 15〜20分間、通常の会話・質問応答を続ける。途中で以下を最低1回ずつ実施する
   - `s`: 状態表示（`state=... mode=... tts=...`、録音状態）
   - `l`: Conversation History表示
   - `g`: ミーティング分析（Manual Flush、`analysis` Typed Event）
   - `G`: サマリー生成
5. 意図的に一定時間沈黙する（`reason=silence` の発話区切りを発生させるため）
6. 必要に応じてネットワーク切断・再接続を試み、reconnect挙動を観測する

## 6.2 シナリオ開始

Runtime Client起動 → Calibration完了 → `r` キーでRECORDING ON。

## 6.3 終了方法

`q` キー、または `Ctrl+C`（`src/runtime_client/main.py`、Keyboard UX既存仕様）。Runtime Client側でクリーンな終了（exit 0）を確認する。

---

# 7. Dashboard確認

Dashboard（§4）が実際に表示・保持するフィールドと、それ以外の「Runtime全体の状態確認」を混同しないよう、以下では**Dashboardの`GET /dashboard`/`GET /`に実在するフィールドかどうか**を明記した上で、各項目の確認方法・正常時・異常時を示す。

| # | 項目 | Dashboardのフィールドか | 正常時 | 異常時 |
|---|---|---|---|---|
| 1 | **Runtime Status** | いいえ。DashboardResultにRuntime稼働状態そのものを示すフィールドは無い（コード確認、`src/dashboard/dashboard_runtime.py`） | §3 Health Check で `/healthz` 200、または Runtime Client `s` キーで `state=...` が表示される | `/healthz` が404/503、または `s` キー実行時に応答がない |
| 2 | **Transcript** | はい（`transcript`、Conversation Traceability経由） | 直近に投入されたイベントの発話内容が文字列で表示される | `metadata.transcript` 未投入、または対象イベントが`transcript`以外の場合は `null` |
| 3 | **Conversation** | はい（`conversation_line`, `speaker`） | 発話番号・話者（`YOU`/`AGT`）が表示される | `metadata` 未投入時は `null`（推測して埋めない仕様。`docs/H4_RUNTIME_EVENT_CONTRACT.md`） |
| 4 | **Verification Runtime** | はい（`gap_detected`, `gap_reason`, `fallback_detected`, `reliable`, `reliability_score`, `warnings`） | 各フィールドが値を持つ。**`gap_detected=True` は実Runtimeの既知の想定内挙動**（`confidence`/`is_final`がワイヤ上に出力されていないため。`docs/RUNBOOK_RUNTIME_VERIFICATION.md` §7）であり、それ自体は異常ではない | `VerificationRuntime().handle()` が例外を送出する場合（入力が dict/Mapping でない等）。§10参照 |
| 5 | **Trust Runtime** | はい（`trust_score`, `trust_level`, `human_review_required`, `review_reason`） | 実Runtimeイベントに対しては概ね `trust_score=0.5` / `trust_level="CAUTION"` になる（上記gap_detectedに起因、既知の想定内挙動） | `trust_level="UNTRUSTED"` が継続する、または想定外の例外 |
| 6 | **Runtime Metrics** | いいえ。「Metrics」という名称のフィールドはDashboardに存在しない | Runtime本体（`phantom_runtime.py`）の `health_monitor()` が `args.health_interval` 秒毎（既定60秒）に標準出力へ `[health]`/`[latency]` 行を出す。`ENABLE_QUEUE_METRICS` 環境変数有効時は `[metrics] audio_q_util=...%` も出力される（`src/phantom_runtime.py:701` で `ENABLE_QUEUE_METRICS` を読み取り内部キー `QUEUE_METRICS` に格納、`:9338`で参照。設定する環境変数名は `ENABLE_QUEUE_METRICS` であり `QUEUE_METRICS` ではない点に注意） | `[health] DEAD THREADS: [...]` が出力される場合（reply-worker/audio-captureスレッドの異常終了） |
| 7 | **Health** | いいえ。#1と同様、DashboardにHealthフィールドは無い | §3 Health Check を参照 | 同上 |
| 8 | **Session Lifecycle** | いいえ。Dashboardはセッションのライフサイクル状態を保持しない | §8 Runtime Trace の `Session START`/`DISCONNECT`/`TEARDOWN START`/`TEARDOWN END` で確認する | いずれかのイベントが欠落、または同一`session_id`で2回以上出現（二重実行） |
| 9 | **Conversation Traceability** | はい（`conversation_line`, `speaker`, `transcript` の3項目、`docs/RUNBOOK_DASHBOARD.md` §8A） | `GET /`のHTML上で `Conversation`/`Speaker`/`Transcript` 行に値が表示される | Conversation情報未投入時は3項目とも `null`（`docs/RUNBOOK_DASHBOARD.md` §8A.5） |
| 10 | **Typed Events** | 部分的。個々のTyped Event一覧表示機能はDashboardに無い（直近1件の`DashboardResult`のみ保持、`docs/RUNBOOK_DASHBOARD.md` §2.3）。`event_id`/`source_event_id`フィールドが、直近1件がどのTyped Eventに由来するかを示すのみ | 個々のTyped Event自体（`transcript`/`reply`/`analysis`/`latency`/`status`/`error`）はRuntime Client画面表示（§5.3相当の`[JP] <reply>`等）で確認する | Typed EventがRuntime Client側に届かない場合は`docs/RUNBOOK_RUNTIME_VERIFICATION.md` §8.2参照 |

**重要**: WebSocket側で `1011`/`409` が発生している間も、Dashboard自体（本セクション対象のコンポーネント）は独立したサブシステムであるため、直近に投入した内容を保持し続ける（`docs/RUNBOOK_DASHBOARD.md` §7.4）。Operator E2E中にreconnectが発生した前後で、Dashboardの表示内容が意図せず消失・巻き戻っていないことを確認する。

---

# 8. Runtime Trace確認

`PHANTOM_TRACE=1`（必要に応じ `PHANTOM_TRACE_FILE=<path>`）を設定すると、以下のイベントが構造化ログとして出力される（`src/runtime_trace.py` / 各呼び出し元モジュール、コード確認済み）。ユーザーが列挙した項目名と、実際にコード上で使われている `stage` 文字列が異なる場合は、実際の文字列を明記する。

| 確認したい概念 | 実際の `stage` 文字列 | 出力元 | 主なフィールド |
|---|---|---|---|
| Speech START | `Speech START` | `src/phantom_runtime.py`（STT呼び出し開始） | `provider`, `audio_sec` |
| Speech END | `Speech END` | `src/phantom_runtime.py`（STT呼び出し終了） | `provider`, `success`, `exception` |
| reason=force / reason=silence | `VAD FLUSH`（`reason` フィールドの値として`force`/`silence`が入る。`VAD START`/`VAD FLUSH`という文字列自体は「Speech START/END」ではなく、Server VADのセグメント検出を指す） | `src/audio/vad_buffering.py` | `reason`, `dur_sec`, `discarded` |
| Session START | `Session START` | `src/runtime/transport_gateway.py` | `session_id`, `provider`, `lifecycle_state` |
| Session DISCONNECT | `Session DISCONNECT` | 同上 | `session_id`, `lifecycle_state` |
| Session TEARDOWN START | `Session TEARDOWN START` | 同上 | `session_id`, `lifecycle_state` |
| Session TEARDOWN END | `Session TEARDOWN END` | 同上 | `session_id`, `lifecycle_state` |
| Queue | 専用の`stage`は無い。関連するのは `audio_queue enqueue` / `transcript_queue enqueue`（`phantom_runtime.py`側キュー、`qsize`/`maxsize`を記録）、および `PIPELINE STATE SNAPSHOT`（`audio_queue`/`transcript_queue`の`size`/`maxsize`/`full_count`/`drop_count`を含む）。**TransportGateway側の`event_queue`（`_EVENT_QUEUE_MAXSIZE=1000`、`src/runtime/transport_gateway.py:86`）はオーバーフロー時に`asyncio.QueueFull`を捕捉して無条件でdropし、Runtime Traceイベントは出力しない**（コード確認、`_enqueue_event()`のコメント: "live stream, not a durable log — drop under sustained backpressure"）。TransportGateway側のQueue溢れは現状Runtime Traceで直接観測できない | `src/phantom_runtime.py`, `src/runtime/transport_gateway.py:502-506` | 上記 |
| Health | 専用の`PHANTOM_TRACE`イベントは無い。`_trace("health/threads", ...)` / `_trace("health/queues", ...)` は別の仕組み（`args.trace` または `args.debug_short_mode` または `RUNTIME_LOG_LEVEL=DEBUG` で有効化される標準出力ログ、`PHANTOM_TRACE`とは独立）。`PHANTOM_TRACE=1`時は`PIPELINE STATE SNAPSHOT`が`args.health_interval`秒毎（既定60秒）に出力され、`threads_alive`辞書を含む | `src/phantom_runtime.py` `_trace()` / `health_monitor()` | `threads_alive`, `speech`, `conversation`, `runtime_event` |

## 8.1 起動方法

```bash
PORT=8080 PHANTOM_TRACE=1 PHANTOM_TRACE_FILE=logs/trace.jsonl \
python -m runtime.cloud_run_shell -- --profile default --mode light --no-color --audio-source fd
```

## 8.2 正常なSession Lifecycle状態遷移

```
CONNECTED → DISCONNECTING → TEARDOWN(START) → TEARDOWN(END) → CLOSED
```

確認ポイント:

- 同一 `session_id` で `Session START` / `DISCONNECT` / `TEARDOWN START` / `TEARDOWN END` の4イベントがすべて出現していること
- `TEARDOWN START` はあるが `TEARDOWN END` が無い場合、`session_teardown()`（子プロセスのSIGINT/SIGKILL待ち）が長時間応答していない可能性がある
- 同一 `session_id` で `TEARDOWN START`/`TEARDOWN END` が2回以上出現している場合は異常（二重実行）

## 8.3 追加のWebSocket Traceイベント（Runtime Client側、`src/runtime_client/websocket_client.py`）

次回のBugチケット（`docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md`）の調査項目（Ping送信/Pong受信/Last Send/Last Receive/Close Code/Close Reason/Handshake開始/終了/Reconnect開始/終了/Session ID/Connection ID/Trace ID）のうち、**現時点でコード上に既に存在するもの**を以下に示す（未存在の項目を存在するかのように書かない）。

| 項目 | 現状 |
|---|---|
| Last Send | `self._state["last_send_ts"]`（`_send_audio()`内で更新） |
| Last Receive | `self._state["last_recv_ts"]`（`_receive_events()`内で更新） |
| Close Reason | `self._state["close_reason"]`（`handshake_rejected_<code>` / `fatal_close_<code>` / `connection_closed_<code>` / `os_error`） |
| Reconnect | `WebSocket RECONNECT` トレースイベント（`event_id=f"reconnect-{attempt}"`, `ws_state=...`） |
| WebSocket SEND / RECEIVE | `WebSocket SEND`（`nbytes`） / `WebSocket RECEIVE`（`nbytes`）トレースイベント |
| Ping送信 / Pong受信 / Close Code（数値そのもの） / Handshake開始・終了 / Connection ID / Trace ID | **コード上に専用のTraceフィールドは確認できなかった。** 存在しない機能として扱うこと |

---

# 9. 合格条件

以下をすべて満たす場合、Production Verificationは**PASS**と判断する。

| # | 項目 | 判定基準 | 確認方法 |
|---|---|---|---|
| 1 | Gemini STT正常 | 発話が実際にtranscriptとして返る | Runtime Client画面の`◎ <transcript>`表示、`transcript` Typed Event |
| 2 | Gemini LLM正常 | transcriptに対し`reply` Typed Eventが返る | Runtime Client画面の`[JP] <reply>`表示 |
| 3 | OpenAI正常 | 上記2項目と同様、OpenAI Providerで確認 | 同上 |
| 4 | Dashboard更新 | `POST /aggregate`後、`GET /dashboard`が最新の`DashboardResult`を返す | §4/§7 |
| 5 | Verification更新 | `gap_detected`/`fallback_detected`判定が`DashboardResult`に含まれる | §7 #4 |
| 6 | Trust更新 | `trust_score`/`trust_level`/`human_review_required`が`DashboardResult`に含まれる | §7 #5 |
| 7 | Conversation Traceability更新 | `conversation_line`/`speaker`/`transcript`がDashboardに反映される | §7 #9 |
| 8 | Health正常 | `GET /healthz`応答時間が継続して数十ms以内（目安: 最大約10ms） | §3.1 |
| 9 | Queue正常 | `phantom_runtime.py`側`[health] DEAD THREADS`が出力されない。`audio_queue`/`transcript_queue`の`drop_count`が異常増加しない | §7 #6、§8 Queue行 |
| 10 | reason=silence確認 | Operator E2E中に`VAD FLUSH reason=silence`が最低1回発生している | §8 |
| 11 | reason=force確認 | Operator E2E中に`VAD FLUSH reason=force`が最低1回発生している | §8 |
| 12 | 1011なし | `keepalive ping timeout`によるClose 1011が**0件** | §10 |
| 13 | 409なし | `handshake rejected (409)`が**0件** | §10 |
| 14 | Runtime Exceptionなし | Runtime Client/Runtime本体で未処理例外によるクラッシュがない | Runtime Client/Cloud Runログ |

## 9.1 参考: 過去の実施記録

- **2026-07-10 Production Startup Calibration**: 実機マイクでBaseline Observation成功（`noise_floor=133.96 RMS`）、Dynamic Contamination Threshold導出（`401.9 RMS`）、Calibration SUCCESS（`speech_gate=490.5 RMS`, Fallback: False）。詳細は本ファイルの旧版相当データとして `src/logs/calibration_20260710_185125.log` / `logs/root_cause_summary.txt`（`.gitignore`の`logs/`により追跡対象外、作業ツリーに実在）を参照。
- **2026-07-12 TransportGateway修正検証**: 20分間・実Gemini STT 248件・実Gemini LLM 209件・強制切断/再接続4回の条件下で、上記PASS条件12・13（1011/409）を含め全項目を満たすことを確認済み（`docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md` §4.3）。
- **2026-07-12 再発**: 上記修正適用後の状態でも、Gemini構成のOperator E2E中に1011→409が再発した事例が別途記録されている（`docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md`、Status: Open）。本項目を実施する際は、この既知の未解決事象を踏まえ、発生した場合は同Bugチケットへ追記すること（推測で原因を書かない）。

---

# 10. Troubleshooting

## 10.1 `OPENAI_API_KEY` not set

**現象**: `ERROR: OPENAI_API_KEY not set or invalid.`（ローカル直接実行時）、またはCloud Run側でOpenAI呼び出しが失敗する。

**確認**: ローカル実行時は`.env`または環境変数、Cloud Run側は`gcloud run services describe phantom-runtime-lite --region asia-northeast1`のEnv varsに`OPENAI_API_KEY`が設定されているか確認する。

**対処**: `gcloud run services update phantom-runtime-lite --region asia-northeast1 --update-env-vars OPENAI_API_KEY="<KEY>"`（`docs/RUNBOOK.md` §6.1）。

## 10.2 `GEMINI_API_KEY` not set

**現象**: `provider=gemini`セッションでLLM呼び出しが失敗する。

**対処**: `gcloud run services update phantom-runtime-lite --region asia-northeast1 --update-env-vars GEMINI_API_KEY="<KEY>"`（`docs/RUNBOOK.md` §6.2）。

## 10.3 Dashboardが表示されない

**現象**: `GET /dashboard`が404のまま、または`GET /`が空状態のまま。

**原因**: `POST /aggregate`がまだ一度も呼ばれていない（想定内。§4.3参照、常駐コンシューマは存在しない）。

**対処**: §4.6の手順で`scripts/post_dashboard_event.py`を実行する。

**現象**: `ModuleNotFoundError: No module named 'uvicorn'`。

**対処**: `pip install uvicorn`（`requirements.txt`に含まれていない、§1.2/§4.1参照）。

**現象**: `[Errno 48] Address already in use`。

**原因**: §4.1の既定手順（Dashboard側は `--port 8081`）から外れて `--port 8080` を指定した場合、パターンB（ローカルDocker、§2.2）ではRuntimeが既に8080を使用しているため衝突する。

**対処**: §4.1の既定通りDashboard側を `--port 8081` で起動する。

## 10.4 Runtime接続失敗

**現象**: Runtime Clientが起動時にCloud Run URLへ接続できない。

**確認**: §3 Health Checkで`/healthz`が200を返すか確認する。Cold Start（下記10.6）の可能性を確認する。

## 10.5 WebSocket接続失敗

**現象**: `websocket_client`が接続できない、または`HTTP400`/`HTTP409`を受ける。

**原因**: `provider`未指定・不正で`HTTP400`（`docs/RUNBOOK.md` §11.4/§11.5）。Single Runtime Policyにより2つ目の同時接続で`HTTP409`（同§13）。

## 10.6 `1011 keepalive timeout` → reconnect → `409`

**現象**: STT/LLMが数ラウンド正常動作した後、`connection closed (... keepalive ping timeout ...)` に続き `sent 1011 (internal error)` が出力され、直後の自動reconnectが `handshake rejected (409)` で拒否される。

**背景（根本原因、修正済みの機構的欠陥）**: `TransportGateway`は`/healthz`と全`/ws`セッションを単一のasyncioイベントループで処理する。セッション終了処理を同期ブロッキングのままイベントループ上で実行すると、Ping/Pong keepaliveと新規接続のハンドシェイクが進行できなくなり、1011→409の連鎖が起きる。2026-07-12に`reader_thread.join()`/`session_teardown()`を`loop.run_in_executor()`でイベントループ外へ退避する修正を適用済み（`docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md`）。

**現状（未解決）**: 上記修正適用後の状態でも、Gemini構成のOperator E2E中に本事象が再発した記録がある（`docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md`、Status: Open）。OpenAI構成では現時点で未再現。TransportGateway/Gemini SDK/WebSocketライブラリ/runtime_client/reply_workerのいずれが原因かは未確認・未証明であり、推測で断定しない。

**対処（本事象が発生した場合）**:

1. まず§3.1の手順で`GET /healthz`の応答時間を確認し、イベントループが実際にブロックしているかを最初に切り分ける
2. §8の手順で`PHANTOM_TRACE=1`を有効化し、`Session TEARDOWN START`〜`END`の間隔、および再接続直前に別の`Session START`が記録されていないかを確認する
3. 発生した事実（再現条件・ログ・§8のTrace結果）を`docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md`に追記する。原因を推測で記載しない

## 10.7 Queue dropped

**現象**: `phantom_runtime.py`側`audio_queue`/`transcript_queue`の`drop_count`（§8 Queue行、`PIPELINE STATE SNAPSHOT`）が増加している。

**対処**: `[health] DEAD THREADS`が出力されていないか確認する（reply-worker/audio-captureスレッドの異常終了が原因である可能性）。TransportGateway側`event_queue`のオーバーフローはRuntime Traceに出力されない仕様のため（§8 Queue行参照）、この経路の溢れはコード上のカウンタを追加しない限り観測できない。

## 10.8 Health NG

**現象**: `GET /healthz`が200を返さない、または応答が遅い。

**対処**: §3.1の応答時間監視、および§10.6の切り分け手順を参照。

## 10.9 マイクデバイスが見つからない

**現象**: `--input-device`に指定した名前・インデックスが解決できず、システムデフォルト入力にフォールバックする。

**対処**: `python -m runtime_client --list-devices`（§1.2）で正確なデバイス名・インデックスを再確認する。

## 10.10 Cloud Run Cold Start

**現象**: Runtime Client起動直後のWebSocket接続がタイムアウト、または初回応答が遅い。

**対処**: `--max-reconnect-attempts`/`--backoff-base-seconds`（既定3回・1.0秒基準の指数バックオフ）で再接続を試みる。事前に`/healthz`を一度叩いてコンテナをウォームアップしておくと再現しにくい。

---

# Validation（本Runbookのセルフレビュー）

本Runbookのみを見て、Operator E2E（§6）が最後まで実施できるかを以下で確認した。

- [x] §1〜§5の順に読み進めれば、Prerequisites確認 → Runtime起動 → Health Check → Dashboard起動 → Runtime Client起動（Gemini/OpenAI）まで、リポジトリに実在するコマンドのみで到達できる
- [x] §4 Dashboard起動は、RuntimeとDashboardが別プロセス・別の依存関係であることを明記しており、Operatorが「Dashboardに会話が自動反映される」という誤解をしないよう記載した
- [x] §6 Operator E2Eのシナリオ開始・終了方法は、実在するKeyboard UX（`r`/`s`/`l`/`g`/`G`/`q`）に基づく
- [x] §7 Dashboard確認は、ユーザー指定の10項目全てに触れつつ、Dashboardの実フィールドではない項目（Runtime Status/Runtime Metrics/Health/Session Lifecycle）については確認先を明示し、実在しない機能を実在するかのように書いていない
- [x] §8 Runtime Traceは、コード上の実際の`stage`文字列を確認した上で記載しており、要求された概念名（Speech START/END, reason=force/silence, Session START/DISCONNECT/TEARDOWN START/END, Queue, Health）と実装の対応・不一致を明示した
- [x] §9 合格条件は§6/§7/§8で確認した内容のみを基準にしており、新たな未検証項目を持ち込んでいない
- [x] §10 Troubleshootingは、2026-07-12時点で未解決のBug（1011→409再発）を「解決済み」と誤記せず、Open状態のまま参照している

**残課題（本Runbook単体では解決できない既知のギャップ、推測で埋めない）**:

- 1011→409再発の根本原因は未特定（§9.1、§10.6、`docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md`）
- TransportGateway側`event_queue`のオーバーフローはRuntime Traceで直接観測できない（§8 Queue行、§10.7）
- Dashboardへの常駐コンシューマ（Runtimeイベントの自動流し込み）は未実装（§4.3）

---

# 制約

本Runbookのv2.1改訂（Release Candidate Review指摘の是正）にあたり、ソースコードは変更していない。今回のレビュー是正で変更したドキュメントは以下の通り（本ファイルのみではない）。

- `README.md`
- `docs/RUNBOOK_PRODUCTION_VERIFICATION.md`（本ファイル）
- `docs/RUNBOOK_DASHBOARD.md`
- `docs/RUNBOOK_RUNTIME_VERIFICATION.md`
- `docs/RUNBOOK.md`
- `docs/HACKATHON_KNOWN_ISSUES_AND_ROADMAP.md`
- `docs/ARCHITECTURE.md`
- `LICENSE`
- `demo/demo_script.md`
- `docs/DEMO.md`
