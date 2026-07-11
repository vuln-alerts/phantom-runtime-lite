# Runbook — Dashboard閲覧機能 (RuntimePipelineOrchestrator / DashboardService / GET /dashboard / GET /)

**Version:** 1.0
**Target Feature:** Hackathon提出物 — Runtime Pipeline Orchestrator (`src/runtime/pipeline_orchestrator.py`), DashboardService (`src/api/dashboard_service.py`), Dashboard View (`src/api/dashboard_view.py`, `src/api/templates/`), FastAPI拡張 (`GET /dashboard`, `GET /`)
**Related:** `docs/RUNBOOK_RUNTIME_VERIFICATION.md`（Verification/Trust/Dashboard Runtime・FastAPI起動手順の詳細）, `docs/H4_RUNTIME_EVENT_CONTRACT.md`（Frozen）
**Last Updated:** 2026-07-11

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

# 9. Operational Checklist

- [ ] `pip install uvicorn`
- [ ] `python -m uvicorn api.api_server:app --host 127.0.0.1 --port 8080`
- [ ] `GET /dashboard` → 404であることを確認
- [ ] Step2-3の手順で `POST /aggregate` を実行
- [ ] `GET /dashboard` → 200 + 内容確認
- [ ] `GET /` をブラウザで開いて表示確認
