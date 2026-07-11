# Runbook — Dashboard閲覧機能 (RuntimePipelineOrchestrator / DashboardService / GET /dashboard / GET /)

**Version:** 1.1
**Target Feature:** Hackathon提出物 — Runtime Pipeline Orchestrator (`src/runtime/pipeline_orchestrator.py`), DashboardService (`src/api/dashboard_service.py`), Dashboard View (`src/api/dashboard_view.py`, `src/api/templates/`), FastAPI拡張 (`GET /dashboard`, `GET /`)
**Related:** `docs/RUNBOOK_RUNTIME_VERIFICATION.md`（Verification/Trust/Dashboard Runtime・FastAPI起動手順の詳細）, `docs/H4_RUNTIME_EVENT_CONTRACT.md`（Frozen）, `docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §11（TransportGateway Session Lifecycle Verification — WebSocket層の`1011`/`409`切り分け）
**Last Updated:** 2026-07-12

---

# 1. Purpose

## RuntimePipelineOrchestrator の目的

テストコード内にのみ存在していた `_run_pipeline()`（`tests/test_h4_10_integration_validation.py:127-136`）と同一の処理順序 — RuntimeEventAdapter.translate → VerificationRuntime.handle → TrustRuntime.handle → DashboardRuntime.render → EventAggregator.aggregate — を、本番コード（`src/runtime/pipeline_orchestrator.py`）から呼び出せる形にする。VerificationRuntime / TrustRuntime / DashboardRuntime / EventAggregator のロジックは一切変更しない。

## DashboardService の目的

直近1件の `DashboardResult` をプロセス内メモリに保持し、取得できるようにする。責務はこの保持・取得のみで、Pipelineの実行やVerification/Trust/Dashboardの処理は一切持たない。

## GET /dashboard・GET / の目的

DashboardServiceが保持する最新の `DashboardResult` を、それぞれJSON・ブラウザ表示用HTMLとして公開する。

## 本Runbookの対象範囲

Dashboard閲覧機能に関わる範囲のみを扱う。Cloud Run Runtimeの起動・WebSocket接続・Verification/Trust/Dashboard Runtime自体の検証手順は `docs/RUNBOOK_RUNTIME_VERIFICATION.md` を参照。

---

# 2. Architecture

## 2.1 データフロー

```
既存Runtime（Verification→Trust→Dashboard→Aggregatorを経由して
EventAggregateを組み立てる、どの呼び出し元でも良い。RuntimePipelineOrchestrator
はそのための部品として提供される）
        |
        v
   POST /aggregate（契約は無変更: 同じリクエスト/レスポンス形状）
        |
        | (副作用: event_aggregate.dashboard_result を保存)
        v
   DashboardService（直近1件のDashboardResultのみ保持）
        |
        +--> GET /dashboard  (JSON)
        +--> GET /           (HTML, api/dashboard_view.py + api/templates/)
```

## 2.2 各コンポーネントの責務・入力・出力

| コンポーネント | 入力 | 出力 | 備考 |
|---|---|---|---|
| RuntimePipelineOrchestrator | raw event (dict) | PipelineOutcome (event, verification_result, trust_result, dashboard_result, event_aggregate) | VerificationRuntimeの内部状態を維持するため、単一インスタンスの再利用を想定 |
| DashboardService | DashboardResult | なし（保持のみ） | Pipeline実行への依存なし |
| `POST /aggregate` | EventAggregate (JSON) | EventAggregate (JSON, 無変更) | 副作用として `DashboardService.set_latest()` を呼ぶ |
| `GET /dashboard` | なし | DashboardResult (JSON) or 404 | |
| `GET /` | なし | HTML | api/templates/dashboard.html or dashboard_empty.html |

## 2.3 重要な前提(コードから確認できる事実)

- `EventAggregate`(`src/aggregator/event_aggregate.py`)は `dashboard_result` を直接フィールドとして持つため、`POST /aggregate` が受け取るリクエストボディには既にPipelineを経由して生成された `DashboardResult` が含まれている。DashboardServiceの更新はこれを読むだけで、Pipelineを再実行しない。
- DashboardServiceは直近1件のみを保持する。履歴・永続化・複数セッションの同時保持は行わない。プロセス再起動で消える。
- `api_server.py` はVerificationRuntime/TrustRuntime/DashboardRuntime/EventAggregatorを直接importしない（`api.dashboard_service`・`api.dashboard_view` のみをimportする）。
- 実際のCloud Run Runtime（`phantom_runtime.py`）が出すイベントを自動でPipelineに流し込む常駐コンシューマは、本機能でも実装しない（`docs/RUNBOOK_RUNTIME_VERIFICATION.md` に記載の既知のギャップのまま）。`POST /events` のような新規投入APIも追加していない — 既存の `POST /aggregate` を経路として再利用する設計。

---

# 3. Prerequisites

`docs/RUNBOOK_RUNTIME_VERIFICATION.md` §3を参照。venv構築・`pip install -r requirements.txt` の手順は同一。

---

# 4. Startup Procedure

## 4.1 FastAPI起動

`uvicorn` は `requirements.txt` に含まれていない(既知のギャップ)。

```bash
cd src
pip install uvicorn
python -m uvicorn api.api_server:app --host 127.0.0.1 --port 8080
```

---

# 5. Dashboard Verification Procedure

## Step1 まだ何も投入していない状態を確認

```bash
curl -s -w "\n%{http_code}\n" http://127.0.0.1:8080/dashboard
# → 404
curl -s http://127.0.0.1:8080/
# → "No DashboardResult yet." のHTML
```

## Step2 Pipelineを経由してEventAggregateを組み立てる

`RuntimePipelineOrchestrator` を使い、実際のRuntimeイベント形状（`phantom_runtime.py` の `_emit_event()` が出す envelope）に近い raw event を1件処理し、`EventAggregate` を得る。以下はPython REPLでの例:

```python
import sys; sys.path.insert(0, "src")
from runtime.pipeline_orchestrator import RuntimePipelineOrchestrator
from dataclasses import asdict
import json, datetime

raw_event = {
    "version": 1, "type": "transcript",
    "timestamp": "2026-07-11T12:00:00+00:00",
    "payload": {"text": "hello there", "lang": "en", "ts": 1720000000.0, "speaker": "user"},
}
outcome = RuntimePipelineOrchestrator().run(raw_event)

def _default(v):
    if isinstance(v, datetime.datetime):
        return v.isoformat()
    raise TypeError(v)

print(json.dumps(asdict(outcome.event_aggregate), default=_default))
```

## Step3 POST /aggregate へ投入

上記で得たJSONをそのまま `POST /aggregate` に送る。

```bash
curl -s -X POST http://127.0.0.1:8080/aggregate \
  -H "Content-Type: application/json" \
  -d '<Step2で出力されたJSON>'
```

## Step4 GET /dashboard・GET / で反映を確認

```bash
curl -s http://127.0.0.1:8080/dashboard
curl -s http://127.0.0.1:8080/    # ブラウザで開くと表形式で表示される
```

## Step2-3 の自動化 (scripts/post_dashboard_event.py)

上記Step2-3(`RuntimePipelineOrchestrator.run()` → `POST /aggregate`)を1コマンドで実行するスクリプト。RuntimePipelineOrchestrator / VerificationRuntime / TrustRuntime / DashboardRuntime / EventAggregator / FastAPI・API契約は変更しない。

投入する raw_event は次の優先順位で決まる。

1. `--input PATH` — 指定したJSONファイルを読み込む
2. 標準入力(stdin) — パイプでJSONが渡された場合に読み込む
3. どちらも無ければ、Runbook Step2と同じ組み込みサンプルイベントを使用する

```bash
# 組み込みサンプルイベントを使用
python scripts/post_dashboard_event.py

# ファイルからraw_eventを読み込む
python scripts/post_dashboard_event.py \
    --input sample_event.json

# 標準入力からraw_eventを読み込む
cat sample_event.json \
    | python scripts/post_dashboard_event.py

# Cloud Run等、別ホストのFastAPIに投入する場合
python scripts/post_dashboard_event.py \
    --url https://xxxxx.run.app \
    --input sample_event.json
```

不正なJSONを渡した場合は `ERROR: Invalid Runtime Event JSON` を表示して終了コード1を返す。

---

# 6. HTTP API

## 使用API一覧

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/health` | ヘルスチェック(無変更) |
| POST | `/aggregate` | EventAggregateを受け取りJSON化して返す(契約無変更)。副作用としてDashboardServiceを更新 |
| GET | `/dashboard` | 最新のDashboardResultをJSONで返す。未生成なら404 |
| GET | `/` | 最新のDashboardResultをHTMLで表示(最新1件のみ、履歴・グラフ・リアルタイム更新なし) |

## GET /dashboard 期待結果

- 未投入時: `404`
- 投入後: `200` + DashboardResultの全フィールド(`trust_score`, `trust_level`, `reliability_score`, `gap_detected`, `gap_reason`, `fallback_detected`, `human_review_required`, `review_reason`, `warnings`, `session_id`, `source_event_id`, `timestamp`, ほか)

---

# 7. Troubleshooting

## 7.1 GET /dashboard が404のまま

`POST /aggregate` がまだ一度も呼ばれていない(想定内)。Step2-3を実施する。

## 7.2 GET / が空状態のまま

同上。

## 7.3 プロセス再起動後にDashboardが空に戻る

想定内の制約。DashboardServiceは永続化しないインメモリ単一スロット。

## 7.4 WebSocketが `1011`/`409` で切断・再接続を繰り返している間、Dashboardの内容が更新されない

Dashboard（本Runbookの対象、`api.dashboard_service` / `api.dashboard_view`）と TransportGateway（`runtime.transport_gateway`、Cloud Run Compatibility ShellのWebSocket層）は独立したサブシステムであり、`POST /aggregate` を介した手動投入以外の自動連携は無い（§2.3参照）。したがって WebSocket側で `1011`/`409` が発生しても、Dashboard自体のコード（本Runbookが対象とするコンポーネント）には影響しない — 直近に投入された `DashboardResult` を保持し続ける。

WebSocket接続そのものの `1011`/`409` の原因調査・切り分けは `docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §11（TransportGateway Session Lifecycle Verification）を参照。Operator E2E実施時は、WebSocket側でreconnectが発生した前後でもDashboardの表示内容（Trust Score等）が意図せず消失・巻き戻っていないことを確認する（同Runbook §11.7のチェック項目）。

---

# 8. Acceptance Criteria

## 8.1 Hackathon提出条件

- `GET /dashboard` が最新のDashboardResultをJSONで返す、または未生成時404を返す
- `GET /` でブラウザから同じ内容を確認できる
- `POST /aggregate` の既存仕様(リクエスト/レスポンス形状・ステータスコード)が変更されていない
- VerificationRuntime / TrustRuntime / DashboardRuntime / EventAggregator のロジックが変更されていない

## 8.2 実運用条件（未対応・既知のギャップ）

- 複数セッション分のDashboardResultを同時に保持することは未対応
- 実際のCloud Run Runtimeからの自動連携（常駐コンシューマ）は未対応 — `docs/RUNBOOK_RUNTIME_VERIFICATION.md` の既存ギャップのまま

---

# 8A. Conversation Traceability

## 8A.1 目的

Dashboardの Trust Score / Gap Detected / Session ID / Event ID だけでは、「どのRuntime Conversationのどの発話に対する評価なのか」が分からない。この節は、Runtime ConversationからDashboardまでのConversation Traceabilityの追跡経路を記述する。

## 8A.2 アーキテクチャ

```
Runtime Conversation
        │
        ▼
Runtime Event
        │
        ▼
Verification Runtime
        │
        ▼
Trust Runtime
        │
        ▼
Dashboard Runtime
```

## 8A.3 Runtime Eventの責務

Conversation Traceabilityの情報は、Runtime Eventの `metadata`（`docs/H4_RUNTIME_EVENT_CONTRACT.md` の「Runtime Event Metadata」節で正式に定義）が保持する責務を持つ。`payload` の意味・内容は変更しない。

```json
{
  "version": 1,
  "type": "transcript",
  "timestamp": "...",
  "payload": { "...": "..." },
  "metadata": {
    "conversation_line": 31,
    "speaker": "YOU",
    "transcript": "現在、利用人数はどのくらいを想定されていますか？"
  }
}
```

保持する項目:

| Field | Type | Description |
|---|---|---|
| `conversation_line` | int? | Runtime Conversation の発話番号 |
| `speaker` | string? | `YOU` / `AGT` |
| `transcript` | string? | Runtime Conversation の発話内容 |

`metadata` が無い、または上記キーが無い場合は、値を推測せず `None`（JSON上は `null`）とする。

## 8A.4 伝搬経路

- `RuntimePipelineOrchestrator.run()` は `raw_event["metadata"]` を読み取り、`conversation_line` / `speaker` / `transcript` を変換・推測・検証せず、そのまま `DashboardRuntime.render()` へ渡す（Read Only / Pass Through / No Business Logic）。
- `DashboardRuntime` はロジックを持たず、受け取った3項目をそのまま `DashboardResult` にコピーする。
- `EventAggregator`/`EventAggregate` は変更しない。`EventAggregate.dashboard_result` が `DashboardResult` を参照として保持しているため、追加の契約変更なしに `POST /aggregate` レスポンス・`GET /dashboard` の両方に3項目が含まれる。
- `VerificationRuntime`/`TrustRuntime` は一切変更しない。両者はConversation情報を読み書きしない。

## 8A.5 Dashboardでの確認方法

- `GET /`（HTML）: `Conversation` / `Speaker` / `Transcript` の行が、既存の `Event ID` / `Session ID` / `Trust Score` 等とともに表示される。
- `GET /dashboard`（JSON）: レスポンスボディに `conversation_line` / `speaker` / `transcript` が含まれる（Conversation情報未投入時は `null`）。

---

# 9. Operational Checklist

- [ ] `pip install uvicorn`
- [ ] `python -m uvicorn api.api_server:app --host 127.0.0.1 --port 8080`
- [ ] `GET /dashboard` → 404であることを確認
- [ ] Step2-3の手順で `POST /aggregate` を実行
- [ ] `GET /dashboard` → 200 + 内容確認
- [ ] `GET /` をブラウザで開いて表示確認
- [ ] （Operator E2E実施時）WebSocket側でreconnectが発生しても、直近のDashboard内容が意図せず消失・巻き戻らないことを確認（§7.4、`docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §11.7）
