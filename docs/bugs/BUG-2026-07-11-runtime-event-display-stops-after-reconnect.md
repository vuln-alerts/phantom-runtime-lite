# Bug Report: WebSocket reconnect後、gキーによるRuntime Event表示が停止する

- **Status**: Open
- **Reported by**: takeuchi@vuln-alerts.com
- **Reported date**: 2026-07-11
- **Found during**: Production Verification（Runtime Conversation実施中）

---

## Summary

Production Verification中にRuntime Conversationを実施していたところ、WebSocket切断後の自動Reconnectは成功したものの、`g`キーによるRuntime Event表示が動作しなくなる事象を確認した。Conversation自体は継続しており、`l`キーによるConversation History表示は正常に動作していた。

---

## Severity / Priority

- **Severity**: Medium（Conversation継続自体には影響しないが、Runtime観測・デバッグ手段が失われる）
- **Priority**: High（Production Verification / Dashboard確認の運用フローに支障があるため）

---

## Environment

- 対象エンドポイント: `wss://phantom-runtime-lite-mcoseigxna-an.a.run.app/ws?provider=openai`
- 実施フロー: Runtime Conversation（Production Verification手順内）

---

## Observed Behavior

以下のログを確認した。

```text
[websocket_client] connection closed (no close frame received or sent); reconnecting
[websocket_client] reconnect attempt 1/3 in 1.0s
[websocket_client] connected: wss://phantom-runtime-lite-mcoseigxna-an.a.run.app/ws?provider=openai
```

Reconnect完了後、`g` を押下してもRuntime Eventは何も表示されない。

一方で `l` は正常に動作し、Conversation Historyは継続して増加している。実際のログ上でもReconnect後にConversationは継続していることを確認している。

確認できている事実は以下の通り。

- WebSocket reconnectは成功している（ログ上 `connected` が出力される）
- Runtime Conversationは継続している
- Conversation History取得（`l`キー）は正常に動作する
- Runtime Event表示（`g`キー）のみ、reconnect後に動作しなくなる

---

## Expected Behavior

WebSocket reconnect後も、`g` キーを押下すると、reconnect前と同様にRuntime Eventが表示されること。

---

## Reproduction Steps

1. Runtime Conversationを開始する
2. `g` キーでRuntime Event表示が動作することを確認する
3. WebSocket切断を発生させる
4. 自動Reconnect完了を待つ（ログに `connected:` が出力されることを確認）
5. `g` キーを押下する

### Actual

Runtime Eventが表示されない。

### Expected

Reconnect前と同様にRuntime Eventが表示される。

---

## Impact

現在のProduction Verificationでは、以下の場面でRuntime Event表示が利用できなくなり、運用性が低下する。

- Runtime Event確認
- Verification Runtime確認
- Dashboard確認

Conversation自体は継続するため致命的ではないが、Runtime観測・デバッグに支障がある。

---

## Investigation Items

Root Causeは未特定。以下を調査対象とする。

- WebSocket reconnect処理
- Runtime Event購読状態
- Runtime Event Queue
- Runtime Event Buffer
- `g` キー表示処理
- reconnect後のイベントハンドラ再登録
- Runtime Client内部状態
- reconnect後の状態遷移

---

## Investigation Checklist

- [ ] WebSocket reconnect処理のコードパスを確認する
- [ ] Runtime Event購読状態がreconnect後に維持されているか確認する
- [ ] Runtime Event Queueの状態（reconnect前後でのデータ有無）を確認する
- [ ] Runtime Event Bufferの状態（reconnect前後でのデータ有無）を確認する
- [ ] `g` キー表示処理の呼び出し有無・エラー有無を確認する
- [ ] reconnect後にイベントハンドラが再登録されているか確認する
- [ ] Runtime Client内部状態（reconnect前後の差分）を確認する
- [ ] reconnect後の状態遷移（接続状態・購読状態・表示状態）を確認する
- [ ] `l`（Conversation History表示）との処理経路の違いを確認する

---

## Attachments / Evidence

```text
[websocket_client] connection closed (no close frame received or sent); reconnecting
[websocket_client] reconnect attempt 1/3 in 1.0s
[websocket_client] connected: wss://phantom-runtime-lite-mcoseigxna-an.a.run.app/ws?provider=openai
```
