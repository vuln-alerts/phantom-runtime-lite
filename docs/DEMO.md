# Demo Guide

**Purpose:** DevOps × AI Agent Hackathon 2026 提出物のデモを、審査員・レビュアーが実際に再現できる手順としてまとめる。
**正式デモ構成:** OpenAI（`--provider openai`）。Geminiは動作するが、WebSocket `1011`→`409`再発のKnown Issueがあるため（[Known Limitations](../README.md#known-limitations)、[docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md](bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md)、Status: Open）、本デモでは対象外とする。
**詳細手順:** 本ガイドはハイライトのみを示す。実行可能な完全な手順・トラブルシューティングは [docs/RUNBOOK_PRODUCTION_VERIFICATION.md](RUNBOOK_PRODUCTION_VERIFICATION.md) を参照。

---

## 1. デモの目的

Phantom Runtime Liteが、ライブ会話をリアルタイムに観測し続け、発話の書き起こし・応答生成・Runtime Eventの信頼性検証を継続的に行うことを、実際のCloud Run環境・実機マイクで示す。

## 2. 前提

- Cloud Runにデプロイ済みの`phantom-runtime-lite`サービス（`docs/RUNBOOK.md` §4-§9）、またはローカルDocker代替（`docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §2.2）
- `OPENAI_API_KEY`（必須）
- 物理マイク、またはBlackHole経由の合成音声入力

## 3. デモ手順（ハイライト）

### 3.1 Runtime起動・Health確認

```bash
gcloud run services describe phantom-runtime-lite \
  --region asia-northeast1 \
  --format="value(status.url)"

curl -i <CLOUD_RUN_URL>/healthz
# 期待: HTTP/2 200 / ok
```

### 3.2 Runtime Client起動（OpenAI構成）

```bash
cd src
python -m runtime_client \
  --url <CLOUD_RUN_URL> \
  --provider openai \
  --input-device "<入力デバイス名>" \
  --production-verification
```

`=== Calibration Complete ===` の表示を確認する。

### 3.3 会話デモ

1. `r` キーでRECORDING ONにする
2. 通常の会話・質問応答を行う（例: DevOpsインシデント対応会議を想定した発話）
3. `s`: 状態表示
4. `l`: Conversation History表示
5. `g`: ミーティング分析（Manual Flush、`analysis` Typed Event）
6. `G`: サマリー生成

### 3.4 Dashboard API（任意、Runtimeとは別プロセス）

Dashboard APIはRuntimeと自動連携しない（[README.md Dashboard API](../README.md#dashboard-api)参照）。手動でイベントを投入して表示を確認する場合:

```bash
pip install uvicorn
cd src
python -m uvicorn api.api_server:app --host 127.0.0.1 --port 8081
```

```bash
python scripts/post_dashboard_event.py --url http://127.0.0.1:8081
curl -s http://127.0.0.1:8081/dashboard
```

ブラウザで `http://127.0.0.1:8081/` を開くとHTML表示を確認できる。

### 3.5 終了

- Runtime Client: `q` キー、または `Ctrl+C`
- Dashboard API: `Ctrl+C`

## 4. デモで示さないこと（Known Limitations）

- Dashboard APIはRuntimeと自動連携しない（手動/スクリプト投入が必須）
- Gemini構成はWebSocket `1011`→`409`再発のKnown Issueがあるため、本デモでは使用しない
- WebSocket再接続後、`g`キーによるRuntime Event表示が動作しなくなる既知の未解決事象がある（[docs/bugs/BUG-2026-07-11-runtime-event-display-stops-after-reconnect.md](bugs/BUG-2026-07-11-runtime-event-display-stops-after-reconnect.md)）。デモ中に意図せず切断・再接続が発生した場合はこの制約に該当する可能性がある。

詳細は [README.md Known Limitations](../README.md#known-limitations) を参照。

## 5. 参考

- 台本（発話・操作の時系列）: [demo/demo_script.md](../demo/demo_script.md)
- 提出ストーリー: [docs/SUBMISSION_STORY.md](SUBMISSION_STORY.md)
- 詳細な検証手順・合格条件: [docs/RUNBOOK_PRODUCTION_VERIFICATION.md](RUNBOOK_PRODUCTION_VERIFICATION.md)
