# Runbook — Runtime Verification (Verification Runtime / Trust Runtime / Dashboard / FastAPI)

**Version:** 1.0
**Target Feature:** H4 Runtime Extension — Verification Runtime (`src/verification/`), Trust Runtime (`src/trust/`), Dashboard Runtime (`src/dashboard/`), Event Aggregator (`src/aggregator/`), FastAPI Presentation Layer (`src/api/`), Runtime Adapter (`src/runtime/event_adapter.py`)
**Related:** `docs/RUNBOOK.md`（Cloud Run構築・デプロイ・運用手順）, `docs/RUNBOOK_PRODUCTION_VERIFICATION.md`（Runtime Client側のCalibration/WebSocket検証手順）, `docs/H4_RUNTIME_EVENT_CONTRACT.md`（Frozen）, `docs/H4_IMPLEMENTATION_PLAN.md`（Frozen）, `docs/H4_STATUS.md`, `docs/H4_10_VALIDATION_REPORT.md`
**Last Updated:** 2026-07-11

---

# 1. Purpose

本Runbookは、Cloud Run Runtime(`phantom_runtime.py`)が発行するTyped Eventを起点とする、以下4コンポーネントの動作確認および実運用手順を示します。いずれも`docs/H4_STATUS.md`でCompleted判定済みの機能です。

## Verification Runtime の目的

`src/verification/verification_runtime.py`。Typed Event(RuntimeEvent)を1件ずつ受け取り、`docs/H4_RUNTIME_EVENT_CONTRACT.md`が定義する必須フィールド・型・値範囲・イベント順序ルールに照らして検証し、1件のVerificationResult(`gap_detected` / `gap_reason` / `fallback_detected` / `fallback_reason` / `reliable` / `reliability_score` / `warnings` / `explanation`)を生成します。読み取り専用・イベント駆動のRuntimeであり、Provider・Whisper・Runtime内部状態には一切アクセスしません。

## Trust Runtime の目的

`src/trust/trust_runtime.py`。VerificationResultを唯一の入力とし、明示的なTrust Policy(重み付けルール: `reliability_score`を50%、gap検出ペナルティ0.2、fallback検出ペナルティ0.2、warning 1件あたり0.05・最大0.15)を通してtrust_score・trust_level(TRUSTED/CAUTION/UNTRUSTED)・human_review_required(レビュー推奨フラグのみ、レビュー自体は実施しない)を算出します。完全にステートレスです。

## Dashboard の目的

`src/dashboard/dashboard_runtime.py`。VerificationResultとTrustResultの2つを入力に、両者の既算出フィールドをそのまま読み出して可視化向けの1つのDashboardResultへ再構成します。スコアリング・Trust Policy判定・検証ロジックは一切行わない、表示専用レイヤーです。

## FastAPI の目的

`src/api/api_server.py`。Event Aggregator(`src/aggregator/event_aggregator.py`)が生成したEventAggregateを受け取り、JSONとしてそのまま返却する、読み取り専用・ステートレスなHTTPプレゼンテーション層です。実装済みルートは`GET /health`と`POST /aggregate`の2つのみです(§6参照)。

## 本Runbookの対象範囲

本Runbookが対象とするのは、**Verification Runtime / Trust Runtime / Dashboard / FastAPI** の4コンポーネント、およびそれらを橋渡しするRuntime Adapter(`src/runtime/event_adapter.py`)のみです。

以下は本Runbookの対象外であり、既存の別Runbookを参照してください。

| 対象外の範囲 | 参照先 |
|---|---|
| Cloud Run構築・デプロイ・運用手順 | `docs/RUNBOOK.md` |
| Runtime Client(マイク入力・Calibration・Speech Gate・WebSocket送信) | `docs/RUNBOOK_PRODUCTION_VERIFICATION.md` |
| Production Verification(実機マイク・実Cloud Run環境でのStartup Calibration/Transcript生成/応答確認) | `docs/RUNBOOK_PRODUCTION_VERIFICATION.md` |

本Runbook内でこれらに触れる箇所(§4.1 Runtime起動、§5 Step1/Step2、§8.2/§8.5のTroubleshooting等)は、いずれも上記2つのRunbookの手順を前提として参照するのみで、手順自体を重複記載しません。

---

# 2. Architecture

## 2.1 データフロー

```
Cloud Run Runtime (phantom_runtime.py, _emit_event())
    │  {"version": 1, "type": ..., "timestamp": ..., "payload": {...}}
    ▼
runtime/transport_gateway.py  ── WebSocket経由でRuntime Clientへ生イベント行を中継 ──
    │
    ▼
Runtime Adapter (runtime/event_adapter.py, RuntimeEventAdapter.translate())
    │  Contract形式へ変換: {schema_version, event_id, timestamp, session_id, sequence, type, payload}
    ▼
Verification Runtime (verification/verification_runtime.py, VerificationRuntime.handle())
    │  -> VerificationResult
    ▼
Trust Runtime (trust/trust_runtime.py, TrustRuntime.handle())
    │  -> TrustResult
    ▼
Dashboard Runtime (dashboard/dashboard_runtime.py, DashboardRuntime.render())
    │  -> DashboardResult
    ▼
Event Aggregator (aggregator/event_aggregator.py, EventAggregator.aggregate())
    │  -> EventAggregate
    ▼
FastAPI (api/api_server.py, POST /aggregate)
    │
    ▼
JSON
```

## 2.2 各コンポーネントの責務・入力・出力

| コンポーネント | モジュール | 入力 | 出力 | 常駐プロセスか |
|---|---|---|---|---|
| Runtime Adapter | `runtime/event_adapter.py` (`RuntimeEventAdapter`) | 生の`_emit_event()`ワイヤ形式dict(`version`/`type`/`timestamp`/`payload`) | Contract形式のRuntimeEvent dict(`schema_version`/`event_id`/`timestamp`/`session_id`/`sequence`/`type`/`payload`) | いいえ(呼び出し元プロセス内のインスタンス) |
| Verification Runtime | `verification/verification_runtime.py` (`VerificationRuntime`) | Contract形式のRuntimeEvent dict | `VerificationResult` | いいえ(ステートレスに近いが、セッション単位のsequence/timestamp監視のため軽微な内部状態を持つ) |
| Trust Runtime | `trust/trust_runtime.py` (`TrustRuntime`) | `VerificationResult` | `TrustResult` | いいえ(完全ステートレス) |
| Dashboard Runtime | `dashboard/dashboard_runtime.py` (`DashboardRuntime`) | `VerificationResult` + `TrustResult` | `DashboardResult` | いいえ(完全ステートレス) |
| Event Aggregator | `aggregator/event_aggregator.py` (`EventAggregator`) | `VerificationResult` + `TrustResult` + `DashboardResult` | `EventAggregate` | いいえ(完全ステートレス) |
| FastAPI | `api/api_server.py` (`app`) | HTTPリクエスト(`GET /health`, `POST /aggregate`のJSONボディ) | HTTPレスポンス(JSON) | **はい**(`uvicorn`で起動する唯一の常駐HTTPサービス) |

## 2.3 重要な前提(コードから確認できる事実)

- **Verification Runtime / Trust Runtime / Dashboard Runtime / Event Aggregatorは、いずれも「起動する」対象ではありません。** 呼び出し元のPythonプロセスが各クラスをインスタンス化し、`handle()` / `render()` / `aggregate()`を明示的に呼び出すことで動作する、ステートレスなライブラリコンポーネントです。常駐プロセスとして単独で起動するエントリポイントはコード上存在しません。
- **`runtime/transport_gateway.py`が中継する生イベントを自動的にRuntime Adapter以降へ流し込む常駐コンシューマは、本リポジトリの`src/`配下に存在しません。** `docs/H4_10_VALIDATION_REPORT.md` §7 "Remaining Risks" に明記されている既知の事実です(*"No live consumer wired to the adapter yet... nothing in production today reads that stream, runs it through RuntimeEventAdapter, and feeds the H4-2..H4-6 chain automatically"*)。このRunbookの§5・§6で示す確認手順は、いずれもこの前提に基づき、パイプラインを手動またはテストスイート経由で駆動する方法を示します。
- **FastAPIの実装済みルートは`/health`と`/aggregate`のみです。** `docs/H4_IMPLEMENTATION_PLAN.md`が言及する`/events` `/verification` `/trust` `/timeline`は`src/api/api_server.py`に実装されていません(`docs/H4_10_VALIDATION_REPORT.md` §7で既知のギャップとして記録済み)。

---

# 3. Prerequisites

以下がインストール・設定済みであること。

- Python 3.13+(`Dockerfile`のベースイメージ`python:3.14-slim`と整合)
- venv(推奨)
- `requirements.txt`の依存関係:

```bash
cd /path/to/phantom-runtime-lite
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

  `requirements.txt`に含まれる主要ライブラリ: `fastapi>=0.110.0`, `openai>=1.30.0`, `google-genai>=1.0.0`, `sounddevice>=0.4.6`, `numpy>=1.24.0`, `python-dotenv>=1.0.0`, `websockets>=13.0`。

- **`uvicorn`は`requirements.txt`に含まれていません。** FastAPIをHTTPサービスとして起動するには別途インストールが必要です。

```bash
pip install uvicorn
```

- Cloud Run: `GET /health` / `POST /aggregate`単体の確認、および§5 Step1-Step3(Runtime起動〜Typed Event生成確認)以外はCloud Run接続を必須としません。Cloud Run経由の実イベントを使った検証(§5全体、実運用相当の確認)を行う場合は、`docs/RUNBOOK.md`の手順でデプロイ済みのCloud Runサービスが必要です。
- OpenAI API Key: Verification Runtime / Trust Runtime / Dashboard / FastAPIのいずれも、Provider(OpenAI/Gemini)を一切呼び出しません(`tests/test_api_server.py`の`DependencyTests`がAST解析で禁止importを検証済み)。OpenAI API Keyが必要になるのは、Cloud Run Runtime自体を実発話で駆動しTyped Eventを生成させる場合のみで、`docs/RUNBOOK_PRODUCTION_VERIFICATION.md`の前提条件と同一です。

---

# 4. Startup Procedure

## 4.1 Runtime起動(Cloud Run Runtime / Runtime Client)

本Runbookの対象外です。`docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §5「Production Verification」の手順をそのまま使用します(重複記載しません)。Typed Eventは、Runtime起動後にCloud Run側の`_emit_event()`が発行するJSON行として観測できます。

## 4.2 Verification Runtime / Trust Runtime / Dashboard Runtime

§2.3で述べた通り、これら3つは常駐プロセスではないため「起動コマンド」は存在しません。Pythonプロセス(スクリプト、REPL、またはテストスイート)内で、以下のように直接インスタンス化して呼び出します(`tests/test_h4_10_integration_validation.py`の`_run_pipeline()`と同一の呼び出し順序)。

```python
from runtime.event_adapter import RuntimeEventAdapter
from verification.verification_runtime import VerificationRuntime
from trust.trust_runtime import TrustRuntime
from dashboard.dashboard_runtime import DashboardRuntime
from aggregator.event_aggregator import EventAggregator

adapter = RuntimeEventAdapter()          # 1プロセス/1接続につき1インスタンス
vr_runtime = VerificationRuntime()       # セッション単位のsequence/timestamp監視を持つため使い回す

event = adapter.translate(raw_emit_event_dict)
verification_result = vr_runtime.handle(event)
trust_result = TrustRuntime().handle(verification_result)
dashboard_result = DashboardRuntime().render(verification_result, trust_result)
event_aggregate = EventAggregator().aggregate(verification_result, trust_result, dashboard_result)
```

終了方法: 呼び出し元のPythonプロセスを終了するのみ(内部状態を持たないため、特別なシャットダウン手順は不要)。

## 4.3 FastAPI起動

```bash
cd src
python -m uvicorn api.api_server:app --host 127.0.0.1 --port 8080
```

起動確認(別ターミナル):

```bash
curl -s http://127.0.0.1:8080/health
# {"status":"ok"}
```

終了方法: `Ctrl+C`(uvicornのSIGINT/SIGTERMハンドリングで即座に停止)。

---

# 5. Runtime Verification Procedure

## Step1 Runtime起動

本Runbookの対象外です。`docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §5の手順でCloud Run Runtimeおよび Runtime Client を起動する。Startup CalibrationのSUCCESS、WebSocket接続成功を同Runbookの基準で確認する。

## Step2 WebSocket確認

本Runbookの対象外です。`runtime/transport_gateway.py`が`phantom_runtime.py`の`_emit_event()`出力行を、接続中のRuntime ClientへWebSocket経由で verbatim(そのまま)中継していることを確認する。確認方法は`docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §6.3(通常動作)と同一。

## Step3 Typed Event生成確認

Cloud Run Runtimeが発行する生イベント(`_emit_event()`のワイヤ形式)を確認する。形式は`{"version": 1, "type": <str>, "timestamp": <str>, "payload": {...}}`(`phantom_runtime.py:597-602`相当)。Contract上定義されている6種類のイベント種別(`docs/H4_RUNTIME_EVENT_CONTRACT.md` "Event Types"): `transcript` / `reply` / `analysis` / `latency` / `status` / `error`。

以下は、実際の`_emit_event()`呼び出し箇所(`phantom_runtime.py`)を再現した形式例です(`tests/test_h4_10_integration_validation.py`の`RAW_FIXTURES`と同一形式。本Runbook作成時にCloud Runを稼働させて新規に取得したものではありません)。

```json
{"version": 1, "type": "transcript", "timestamp": "2026-07-06T12:00:00+00:00",
 "payload": {"text": "hello there", "lang": "en", "ts": 1720000000.0, "speaker": "user"}}
```

## Step4 Verification Runtime確認

生イベントを`RuntimeEventAdapter.translate()`でContract形式へ変換し(Runtime Adapterの変換のみ、追加ロジックなし)、`VerificationRuntime().handle(event)`へ渡す。**Verification Runtimeへ実際にイベントを投入して取得した実測結果**:

```
VerificationResult(
    schema_version='1.0',
    gap_detected=True,
    gap_reason="missing required field(s) ['confidence', 'is_final'] for event type 'transcript'",
    fallback_detected=False,
    reliable=True,
    reliability_score=0.5,
    warnings=["gap detected: missing required field(s) ['confidence', 'is_final'] for event type 'transcript'"],
)
```

`gap_detected=True`は不具合ではなく、実際のCloud Run Runtimeが`confidence`/`is_final`をワイヤ上に出力していないことを正しく検出した結果です(§7で詳述)。

## Step5 Trust Runtime確認

Step4のVerificationResultを`TrustRuntime().handle(verification_result)`へ渡す。**同じ実測データでの実測結果**:

```
TrustResult(
    schema_version='1.0',
    trust_score=0.5,
    trust_level='CAUTION',
    human_review_required=False,
    contributing_factors=[
        "gap_detected (missing required field(s) ['confidence', 'is_final'] for event type 'transcript')",
        '1 verification warning(s)',
    ],
)
```

## Step6 Dashboard確認

Step4/Step5の結果を`DashboardRuntime().render(verification_result, trust_result)`へ渡す。DashboardResultには、Verification側フィールド(`gap_detected`/`gap_reason`/`fallback_detected`/`reliability_score`/`reliable`/`warnings`)とTrust側フィールド(`trust_score`/`trust_level`/`human_review_required`/`review_reason`/`contributing_factors`)の双方が、値の改変なくそのまま反映される設計になっている(`dashboard_runtime.py`のコード確認)。**DashboardResultの実際の値は、Step6単体では確認しておらず、Step7のPOST /aggregateレスポンスに含まれる`dashboard_result`フィールドを通じて確認した**(§6参照)。

## Step7 FastAPI確認

`EventAggregator().aggregate(...)`の出力を`dataclasses.asdict()`でJSON化し、起動済みFastAPI(§4.3)の`POST /aggregate`へ送信する。詳細な確認手順・実測レスポンス(Step6のDashboardResultを含む)は§6を参照。

---

# 6. FastAPI Verification

## 起動方法

§4.3参照。

```bash
cd src
python -m uvicorn api.api_server:app --host 127.0.0.1 --port 8080
```

## GET /health

```bash
curl -s -w "\nHTTP_STATUS:%{http_code}\n" http://127.0.0.1:8080/health
```

**本Runbook作成時に実行して取得した実測結果**:

```
{"status":"ok"}
HTTP_STATUS:200
```

## POST /aggregate

`EventAggregate`(`schema_version` / `source_event_id` / `session_id` / `timestamp` / `verification_result` / `trust_result` / `dashboard_result`)をJSONボディとして送信する。§5 Step4-Step5で生成した実データを`dataclasses.asdict()` → JSON化し、`curl`で送信して確認した**実測結果**(200 OK、値の改変なし)。**Step6のDashboardResultの実際の値は、このレスポンスの`dashboard_result`フィールドを通じて確認したものである**:

```bash
curl -s -w "\nHTTP_STATUS:%{http_code}\n" -X POST http://127.0.0.1:8080/aggregate \
  -H "Content-Type: application/json" \
  -d @aggregate_payload.json
```

```json
{
  "schema_version": "1.0",
  "source_event_id": "ad00c2f7-8aac-4529-ae1a-52c1b6606e75",
  "session_id": "sess-runbook-demo",
  "timestamp": "2026-07-11T09:48:41.748610Z",
  "verification_result": {
    "gap_detected": true,
    "gap_reason": "missing required field(s) ['confidence', 'is_final'] for event type 'transcript'",
    "fallback_detected": false,
    "reliable": true,
    "reliability_score": 0.5,
    "warnings": ["gap detected: missing required field(s) ['confidence', 'is_final'] for event type 'transcript'"]
  },
  "trust_result": {
    "trust_score": 0.5,
    "trust_level": "CAUTION",
    "human_review_required": false,
    "contributing_factors": ["gap_detected (...)", "1 verification warning(s)"]
  },
  "dashboard_result": {
    "gap_detected": true,
    "reliability_score": 0.5,
    "trust_score": 0.5,
    "trust_level": "CAUTION"
  }
}
```

(`HTTP_STATUS:200`。上記は実測レスポンスの抜粋。全文は`verification_result`/`trust_result`/`dashboard_result`それぞれのフィールドを完全に含む。)

上記に加え、本Runbook作成時に以下も実際に送信して確認済み(実測結果)。

- 必須フィールドを欠いたJSON(例: `{"schema_version": "1.0"}`のみ)を送信 → `422 Unprocessable Entity`、`detail`配列に`"loc":["body","source_event_id"],"msg":"Field required"`等の不足フィールド一覧が返る。
- `GET /events` を送信 → `404 Not Found`(未実装であることの確認)。

## 期待結果

| リクエスト | 期待レスポンス | 根拠 |
|---|---|---|
| `GET /health` | `200 OK`, `{"status": "ok"}` | 本Runbook実測(上記) |
| `POST /aggregate`(正しい形の`EventAggregate` JSON) | `200 OK`, 送信したEventAggregateと同一構造・同一値のJSON(フィールドの欠落・改名・再計算なし) | 本Runbook実測(上記)、`tests/test_api_server.py`の`AggregateEndpointTests`でも保証 |
| `POST /aggregate`(必須フィールド欠落) | `422 Unprocessable Entity`, `detail`配列に不足フィールド一覧 | 本Runbook実測(上記) |
| `GET /events`, `/verification`, `/trust`, `/timeline` | `404 Not Found`(未実装) | 本Runbook実測(`/events`)、コード確認(`src/api/api_server.py`にルート定義なし) |

---

# 7. Expected Results

## 正常時(参考例 — `docs/H4_10_VALIDATION_REPORT.md`に記録されている検証結果)

以下は`docs/H4_10_VALIDATION_REPORT.md` §1「Integration Validation Report」に記録されている、同レポート作成時にliteralな`_emit_event()`フィクスチャを用いて検証された結果の引用です。本Runbook作成にあたって再実行したものではありません。

| 項目 | 結果 |
|---|---|
| Contract compliance | PASS — 変換後の全イベントがContract envelopeの7キーちょうどを持ち、`type`は必ずContract定義の6種のいずれか |
| Identity propagation | PASS — Adapter生成の`event_id`/`session_id`が`VerificationResult → TrustResult → DashboardResult → EventAggregate → JSON`まで不変で伝播 |
| No field loss / rename / recomputation | PASS — JSON応答のフィールド集合が各dataclassの`__dataclass_fields__`と完全一致。`reliability_score`/`trust_score`はin-process結果とJSON応答でbit-identical |
| JSON output | PASS — 6種類全てのイベント種別が`/aggregate`へ`HTTP 200`で到達 |

## 異常時(=既知の"想定内"の挙動。バグではない)

- **実際のCloud Run Runtimeが発行するイベントは、`transcript`/`reply`/`analysis`/`latency`/`status`のすべてで`gap_detected=True`になります。** これはContractが要求するフィールド(例: `transcript`の`confidence`/`is_final`)を、現在の`_emit_event()`実装がワイヤ上に出力していないためであり、`docs/H4_10_VALIDATION_REPORT.md` §6で「correctly and by design, not as an adapter defect」と明記されている、既知の想定内挙動です。本Runbook§5 Step4の実測結果も同一の`gap_detected=True`を示しています。
- **`status`イベントの`state`は、常に`undefined state`として報告されます。** `docs/H4_10_VALIDATION_REPORT.md` §7 Remaining Risks 3に記録された恒久的な既知事象で、Contractの状態enum(大文字)と実Runtimeの状態文字列(例: `recruiter_speaking`)が語彙として一致していないためです。
- 上記2点により、実Runtimeイベントに対する`trust_score`は概ね`0.5`(`trust_level="CAUTION"`)になります。本Runbook§5 Step5・§6で実際に取得した実測値(`trust_score=0.5`, `trust_level="CAUTION"`)は、`docs/H4_10_VALIDATION_REPORT.md` §4 Production-like Validationに記録されている値と一致しています。

---

# 8. Troubleshooting

## 8.1 FastAPIが起動しない

**現象**: `python -m uvicorn api.api_server:app` が `ModuleNotFoundError: No module named 'uvicorn'` で失敗する。

**原因**: `uvicorn`は`requirements.txt`に含まれていません(§3参照)。

**対処**: `pip install uvicorn`を実行する。

**現象**: `[Errno 48] Address already in use`。

**原因**: 指定ポート(既定8080)が既に他プロセスで使用中。

**対処**: `--port`で別ポートを指定するか、既存プロセスを停止する。

## 8.2 Typed Eventが来ない

本Runbookの対象外の切り分けを含みます。

**現象**: Runtime Client側でWebSocket接続は成功しているが、Cloud Run側の`_emit_event()`出力行が観測できない。

**原因候補**: `docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §9の各項目(固定Contamination Threshold・Cloud Run Cold Start・マイクデバイス未検出)を参照。

**重要な前提**: `runtime/transport_gateway.py`が中継するイベント行を自動的にVerification Runtime以降のパイプラインへ流し込む常駐コンシューマは存在しません(§2.3)。「Typed Eventは来ているのにVerification Runtimeまで届かない」という場合、コンシューマ側が実装されていないことが原因であり、Verification Runtime自体の不具合ではない可能性を最初に確認してください。

## 8.3 VerificationResultが生成されない

**現象**: `VerificationRuntime().handle(event)`が例外を送出する。

**原因候補**: `event`が`dict`(または`Mapping`)ではない、あるいは`type`/`payload`キーを持たない構造になっている可能性がある。`VerificationRuntime.handle()`は`event.get("type")`/`event.get("payload")`のように`.get()`で読み出すため、`Mapping`互換でない入力(例: JSON文字列そのもの)を渡すとエラーになる。`json.loads()`でdict化してから渡しているか確認する。

**留意点**: `gap_detected=True`や`reliability_score`が低い値になること自体は正常な検証結果であり、エラーではない(§7参照)。

## 8.4 Dashboardが更新されない

**現象**: 同じDashboardResultが繰り返し表示され、新しい値に変わらない。

**原因**: Dashboard Runtimeは状態を持たない純粋関数(`render()`)であり、「表示を保持・更新するダッシュボード」という概念自体がコード上存在しません。呼び出し元が新しい`VerificationResult`/`TrustResult`のペアで`render()`を再度呼び出していない場合、当然ながら出力は変化しません。

## 8.5 Cloud Runに接続できない

本Runbookの対象外です。`docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §9.2(Cloud Run Cold Start)、および`docs/RUNBOOK.md` §3-§9(Cloud Run構築・デプロイ)を参照。本Runbookのコンポーネント(Verification/Trust/Dashboard/FastAPI)自体はCloud Runへの接続を一切行わないため、この問題はCloud Run Runtime側(参照先2つのRunbookのスコープ)の切り分けが必要です。

## 8.6 `/events` `/verification` `/trust` `/timeline` が404になる

**現象**: これらのエンドポイントへリクエストすると`404 Not Found`が返る(本Runbook§6で`/events`について実測確認済み)。

**原因**: `docs/H4_IMPLEMENTATION_PLAN.md`で言及されているが、`src/api/api_server.py`には未実装(§2.3、§6参照)。修正すべき不具合ではなく、既知のギャップとして`docs/H4_10_VALIDATION_REPORT.md` §7に記録済み。

---

# 9. Acceptance Criteria

**Hackathon提出条件(§9.1)と実運用条件(§9.2)は、評価する対象が異なる別の基準です。** §9.1は「H4 Runtime Extensionとして実装・検証が完了しているか」を判定するものであり、本ドキュメント作成時点で全項目を満たしています。§9.2は「常時稼働の自動パイプラインとして運用できるか」を判定するものであり、§2.3で述べた「常駐コンシューマ未実装」というギャップにより、現時点では条件を満たしていません。**この2つは独立した基準であり、§9.2の未達がHackathon提出条件(§9.1)の達成状況を損なうものではありません。**

## 9.1 Hackathon提出条件

`docs/H4_STATUS.md`に記録された完了状態を基準とする。

| 項目 | 判定基準 | 根拠 | 状態 |
|---|---|---|---|
| H4-1〜H4-10 全項目 | Completed | `docs/H4_STATUS.md` "Completion Status"表 | 全項目Completed |
| テストスイート | 0 failures, 0 errors | `docs/H4_STATUS.md` "Validation Summary"(既存ドキュメント記録) | 217 collected / 215 passed / 2 skipped(想定内のself-skip) / 0 failures / 0 errors |
| Production-like Validation | Docker Build〜Container Shutdownまで全項目PASS | `docs/H4_10_VALIDATION_REPORT.md` §4(既存ドキュメント記録) | 全項目PASS(OpenAI provider・`PROVIDER=gemini`の両方で確認済みと記録) |
| FastAPI実装範囲 | `GET /health`, `POST /aggregate`が正常応答すること | 本Runbook§6実測 | 200 OKを確認済み |

上記の通り、Hackathon提出条件は満たされています。

## 9.2 実運用条件

- Cloud Run Runtimeが発行するTyped Eventを、Runtime Adapter以降のパイプラインへ**自動的に**流し込む常駐コンシューマは、本ドキュメント作成時点で未実装です(§2.3、§11参照)。したがって「実運用」として常時稼働させるには、`runtime/transport_gateway.py`のWebSocketストリームを購読し、本Runbook§4.2の呼び出し順序を自動実行する常駐プロセスの追加実装が別途必要です(コード変更を伴うため、本Runbookの対象外)。
- 現時点で実運用可能な範囲は、(a) FastAPI(`GET /health` / `POST /aggregate`)を常駐HTTPサービスとして起動すること、(b) 個別のTyped Eventに対してパイプラインを手動またはスクリプト経由で駆動し、その結果をFastAPIへ送信すること、の2点です。
- 上記のギャップは、H4 Runtime Extensionの実装・検証範囲(§9.1)そのものの不備ではなく、"次の常駐化ステップ"として`docs/H4_10_VALIDATION_REPORT.md` §7 Remaining Risksに明示的に記録されている、既知かつスコープ外の課題です。

---

# 10. Operational Checklist

- [ ] Runtime起動(Startup Calibration SUCCESS、`docs/RUNBOOK_PRODUCTION_VERIFICATION.md`基準)
- [ ] WebSocket接続確認(Cloud Run Runtime ⇔ Runtime Client)
- [ ] Typed Event生成確認(`_emit_event()`出力行を観測)
- [ ] Runtime Adapter変換確認(`RuntimeEventAdapter.translate()`がContract形式を返す)
- [ ] Verification Runtime確認(`VerificationResult`が生成される。`gap_detected=True`は既知の想定内挙動)
- [ ] Trust Runtime確認(`TrustResult`が生成される。`trust_score`/`trust_level`がTrust Policy通りに算出される)
- [ ] Dashboard確認(`DashboardResult`にVerification/Trust双方のフィールドが改変なく反映される。POST /aggregateレスポンス経由で確認)
- [ ] FastAPI確認(`GET /health` = 200、`POST /aggregate` = 200、フィールドの欠落・改名・再計算がない)
- [ ] Cloud Run接続(該当する場合。`docs/RUNBOOK.md`基準)
- [ ] エラーなし(テストスイート実行で0 failures / 0 errors、本Runbookの手順で例外が発生していない)

---

# 11. Future Improvements

- **常駐コンシューマの実装**: `runtime/transport_gateway.py`の`/ws`ストリームを購読し、Runtime Adapter〜FastAPIまでを自動的に駆動する常駐プロセス(`docs/H4_10_VALIDATION_REPORT.md` §7 Remaining Risks 1の追跡課題)。実装されれば、本Runbook§5・§9.2「実運用条件」の手動駆動前提を置き換えられる。
- **FastAPIエンドポイントの拡充**: `docs/H4_IMPLEMENTATION_PLAN.md`が言及する`/events` `/verification` `/trust` `/timeline`の実装(現状`/health`・`/aggregate`のみ、§2.3・§6・§8.6参照)。
- **`requirements.txt`への`uvicorn`追加**: 現状`uvicorn`は本番/開発いずれの依存関係ファイルにも明記されておらず、クリーンな環境で本Runbook§4.3を実行するには手動インストールが必要(§3・§8.1参照)。
- **`status.state`語彙の整合**: Contractの状態enum(大文字)と実Runtimeの状態文字列の不一致(`docs/H4_10_VALIDATION_REPORT.md` §7 Remaining Risks 3)。Contract側の変更かRuntime側の変更のいずれかが必要で、現状はどちらも対象外(翻訳専用のRuntime Adapterのスコープ外)。
- 上記はいずれも設計・実装が未着手の課題であり、本Runbookは現状のギャップを記録するに留め、対応方針の決定・実装は別タスクとする。

---

# 12. 推奨実施順序

本Runbookおよび関連Runbookは、対象範囲が異なるため、以下の順序での実施を推奨する。

1. **`docs/RUNBOOK.md`** — Cloud Runの構築・デプロイ。デプロイ済みのCloud Runサービスが存在しない場合はここから開始する。
2. **`docs/RUNBOOK_PRODUCTION_VERIFICATION.md`** — Runtime Client側の検証(Startup Calibration・WebSocket接続・Speech Gate・Transcript生成)。Cloud Run Runtimeが実際にTyped Eventを発行できる状態であることを確認する。
3. **`docs/RUNBOOK_RUNTIME_VERIFICATION.md`(本Runbook)** — 2.で発行されたTyped Eventを起点に、Verification Runtime / Trust Runtime / Dashboard / FastAPIの動作を確認する。

2.が未実施、またはTyped Eventが発行される状態にない場合でも、本Runbook§5 Step3以降は、§5 Step3に示した形式例のような、Contract形式に沿ったTyped Eventを手動で用意することで、Cloud Run Runtimeの稼働状態に依存せず個別に検証できる(§4.2参照)。
