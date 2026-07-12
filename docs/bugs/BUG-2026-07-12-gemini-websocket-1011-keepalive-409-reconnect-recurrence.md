# Bug Report: Gemini構成でWebSocket 1011 keepalive timeout → 409 reconnect conflictが再発

- **Status**: Open（未解決）
- **Reported by**: takeuchi@vuln-alerts.com
- **Reported date**: 2026-07-12
- **Found during**: Operator E2E（Gemini構成）

---

## 概要

Gemini構成のみで、Operator E2E実施中に以下の事象が再発した。

1. WebSocketが `1011 (internal error) keepalive ping timeout` で切断される
2. 自動reconnectが試行される
3. reconnect時に `409 (handshake rejected)` でfatalとなる

OpenAI構成では現時点で本事象は未再現。

本事象は、以下の修正が反映済みの状態（コミット `3392b38` "feat(runtime): improve Gemini speech pipeline and transport stability"）でも発生した。

- Gemini STT
- Gemini LLM
- `thinking_budget=0`
- Speech Provider抽象化
- TransportGateway teardown修正

---

## 再現条件

- Provider構成: Gemini（STT / LLM ともにGemini）
- 実施フロー: Operator E2E
- 前提コード状態: 上記修正（Speech Provider抽象化 / thinking_budget=0 / TransportGateway teardown修正）反映済み

OpenAI構成では同条件下での再現は確認できていない。

---

## 発生ログ

```text
runtime_client
[websocket_client] connected: ws://localhost:8080/ws?provider=gemini

...
STT=2094ms TOTAL=2900ms
STT=2295ms TOTAL=3009ms
STT=2888ms TOTAL=3697ms
STT=3510ms TOTAL=4229ms

↓

connection closed
(sent 1011 (internal error) keepalive ping timeout)

↓

reconnect attempt

↓

fatal: handshake rejected (409)
```

---

## 現時点で確定している事実

- Gemini STTは正常に動作している
- Gemini LLMは正常に動作している
- 応答生成は正常に行われている
- thinking token問題（コミット `3392b38` で対応済みの事象）は解消されている
- 今回のログにおいてQueue overflowは確認できていない
- TransportGateway teardown修正後の状態でも本事象は再発している
- OpenAI構成では本事象は未再現（ただし再現試行の網羅性は未確認）

---

## 未確認事項

以下はいずれも未証明であり、原因として断定・推測しない。

- TransportGatewayが原因であるか
- Gemini SDKが原因であるか
- WebSocketライブラリが原因であるか
- runtime_clientが原因であるか
- reply_workerが原因であるか

---

## 今後の調査方針

次回調査では WebSocket Trace を追加し、以下の項目を最低限記録する。

- Ping送信
- Pong受信
- Last Send
- Last Receive
- Close Code
- Close Reason
- Handshake開始
- Handshake終了
- Reconnect開始
- Reconnect終了
- Session ID
- Connection ID
- Trace ID

これらのTraceを取得した上で、次回調査にて原因範囲を絞り込む。

---

## Git Diff Summary

今回はソースコード変更なし（Documentation Only）。

```text
$ git status --short
?? docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md
```

参考: 直前に反映済みの関連修正コミット

```text
3392b38 feat(runtime): improve Gemini speech pipeline and transport stability
```
