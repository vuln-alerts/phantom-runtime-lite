# Demo Script — Phantom Runtime Lite

**用途:** DevOps × AI Agent Hackathon 2026 提出デモの発表台本（発話・操作のタイムライン）。
**正式デモ構成:** OpenAI（`--provider openai`）。技術的なセットアップ手順は [docs/DEMO.md](../docs/DEMO.md) と [docs/RUNBOOK_PRODUCTION_VERIFICATION.md](../docs/RUNBOOK_PRODUCTION_VERIFICATION.md) を参照。本スクリプトはその手順を前提とした「発表の進行台本」であり、コマンドの再掲は最小限に留める。
**想定シナリオ:** DevOpsインシデント対応会議を模した会話に対し、Phantom Runtime Liteがリアルタイムで書き起こし・応答生成・信頼性検証を行う様子を示す。
**想定時間:** 約5分。

---

## 0. 事前準備（発表前に完了させておく）

- Cloud Run Runtimeが起動済みで `GET /healthz` が200を返すことを確認済み（[docs/DEMO.md](../docs/DEMO.md) §3.1）
- `OPENAI_API_KEY` 設定済み
- 入力デバイス（マイクまたはBlackHole）を `--list-devices` で確認済み

## 1. 導入（0:00–0:30）

**発話例:**
> "Phantom Runtime Liteは、単発のプロンプト応答ではなく、ライブ会話をリアルタイムに観測し続けるConversational AI Agent Runtimeです。DevOpsのインシデント対応会議のような、継続的な会話に対して人間の意思決定を支援します。"

**操作:** Runtime Clientを起動する（[docs/DEMO.md](../docs/DEMO.md) §3.2）。`=== Calibration Complete ===` が表示されるまで待つ。

## 2. Recording開始・会話デモ（0:30–3:30）

**操作:** `r` キーでRECORDING ONにする。

**発話例（インシデント対応会議を想定した発話を実際に行う）:**
> "本番環境でレイテンシが悪化しています。直近のデプロイが原因の可能性があります。"
> "ロールバックを実行する前に、まずダッシュボードのメトリクスを確認しましょう。"

**確認ポイント:**
- Runtime Client画面に書き起こし（transcript）と応答（reply）がリアルタイムに表示されることを示す
- `s` キーで状態表示（`state=... mode=... tts=...`）を示す
- `l` キーでConversation History表示を示す

## 3. Meeting Analysis（3:30–4:00）

**操作:** `g` キーでミーティング分析（Manual Flush）を実行する。

**発話例:**
> "gキーを押すと、ここまでの会話からリスク・推奨アクション・確認事実を構造化して抽出します。"

**確認ポイント:** `analysis` Typed Eventとして構造化された分析結果がRuntime Client画面に表示される。

## 4. Dashboard API（任意、4:00–4:30）

Dashboard APIはRuntimeと自動連携しないため、時間に余裕がある場合のみ実施する（[docs/DEMO.md](../docs/DEMO.md) §3.4）。

**発話例:**
> "検証済みのイベントは、Verification RuntimeとTrust Runtimeを経由してDashboard APIで確認できます。これは現時点ではRuntimeとは独立したプロセスとして手動で連携させています。"

**操作:** 別ターミナルでDashboard API（`--port 8081`）を起動し、`scripts/post_dashboard_event.py` でサンプルイベントを投入、ブラウザで `http://127.0.0.1:8081/` を表示する。

## 5. まとめ（4:30–5:00）

**発話例:**
> "Phantom Runtime Liteは、Prompt→Responseではなく、継続的な会話理解によって人間の意思決定を支援するRuntimeです。今回のOpenAI構成に加えてGemini構成にも対応していますが、Gemini構成では WebSocket再接続に関する未解決のKnown Issueがあるため、本デモではOpenAI構成を正式構成としています。詳細はREADMEのKnown Limitationsに記載しています。"

**操作:** `q` キーでRuntime Clientを終了する。

---

## Known Limitations（発表中に質問された場合の回答用）

- Gemini構成: WebSocket `1011`（keepalive timeout）→ 再接続時`409`が再発する既知の未解決事象がある（Status: Open）。詳細: [docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md](../docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md)
- WebSocket再接続後、`g`キー（Runtime Event表示）が動作しなくなる既知の未解決事象がある（Status: Open）。詳細: [docs/bugs/BUG-2026-07-11-runtime-event-display-stops-after-reconnect.md](../docs/bugs/BUG-2026-07-11-runtime-event-display-stops-after-reconnect.md)
- Dashboard APIはRuntimeと自動連携しない（手動/スクリプト投入が必須）

完全な一覧は [README.md Known Limitations](../README.md#known-limitations) を参照。
