# Fix Report: Operator E2E で `1011 keepalive ping timeout` → reconnect → `409` が発生し継続不能になる

- **Status**: Fixed / Validated
- **Reported by**: takeuchi@vuln-alerts.com
- **Fixed by**: Claude Code
- **Date**: 2026-07-12
- **対象コンポーネント**: `src/runtime/transport_gateway.py`（`TransportGateway._handler`）

---

## 1. 原因（Root Cause）

`TransportGateway` は `/healthz` と全ての `/ws` セッションを **単一の asyncio イベントループ**（専用スレッド1本、`_run()` → `_serve()`）上で処理する設計になっている。

ところが `_handler()` の `finally` 節（セッション終了処理）が、このイベントループ上で**同期ブロッキング呼び出し**を直接実行していた。

- `reader_thread.join(timeout=2.0)` — 最大2秒ブロック
- `self._session_teardown(session)` → `RuntimeSession.teardown()`（`cloud_run_shell.py`）
  - `child.send_signal(SIGINT)` → `child.wait(timeout=SHUTDOWN_GRACE_SECONDS=10)`
  - 応答なしの場合 `child.kill()` → `child.wait(timeout=_KILL_WAIT_SECONDS=2)`
  - 合計最大 **約12秒**ブロック

`async def` の中で `await` も `run_in_executor` も介さずに `subprocess.Popen.wait()` や `threading.Thread.join()` を直接呼ぶと、その呼び出し元スレッド＝イベントループ全体が完全に停止する。1セッションの終了処理だけで、このプロセスが処理している**全ての接続**に対して以下が同時に起こる。

1. `websockets` ライブラリが自動送信する Ping/Pong keepalive（既定 `ping_interval=20s` / `ping_timeout=20s`、`websockets==16.0`）を処理するタスクがループ上で一切進行できない → 相手からの Pong/Ping に応答できない → いずれかの側で `keepalive ping timeout` → close code **1011**。
2. `_process_request` / `_handler`（新規接続のハンドシェイク処理）も同じループ上でしか動けないため、その間に到着した接続要求は一切処理されない。ロックそのもの（`_active_connection`）はこのブロックの**前**に解放されるが、ループ自体が凍結しているため、その解放は誰からも観測できない。ループが再開した瞬間、たまたま先に溜まっていた（クライアント側では既に `open_timeout` 切れで見捨てられた）接続要求が空いたスロットを先に掴んでしまい、オペレーターの本当の再接続要求が **409** で弾かれる（または応答自体がタイムアウトする）。

これは `docs/bugs/BUG-2026-07-11-runtime-event-display-stops-after-reconnect.md` とは別の事象だが、同一カテゴリ（セッション終了処理が共有イベントループをブロックする設計欠陥）に起因する。また `docs/SPEECH_GATE_SERVER_VAD_VALIDATION_REPORT.md`（別タスクで作成、未コミット）にも「`keepalive ping timeout` → 再接続が `409 (fatal)` で拒否されクライアントが終了した」という、当時は原因未特定だった観測が記録されており、今回の原因究明と完全に一致する。

### 原因コード（修正前・抜粋、`src/runtime/transport_gateway.py`）

```python
finally:
    if drain_task is not None:
        drain_task.cancel()
    if reader_thread is not None:
        reader_stop.set()
        reader_thread.join(timeout=2.0)          # ← イベントループを最大2秒ブロック
    with self._active_lock:
        if self._active_connection is websocket:
            self._active_connection = None
        self._active_session = None
    self._session_teardown(session)              # ← child.wait()/kill() が
                                                   #    イベントループを最大約12秒ブロック
    _log("client disconnected")
```

---

## 2. 修正内容（設計変更理由を含む）

**方針**: イベントループをブロックしている同期処理（`reader_thread.join()` / `session_teardown()` = `child.wait()` / `child.kill()`）のみを `loop.run_in_executor()` でデフォルトの `ThreadPoolExecutor`（バックグラウンドスレッド）へ退避する。**`TransportGateway` の共有状態（`_active_connection` / `_active_session` / `_active_lock`）はこれまで通りイベントループ側のみで管理し、バックグラウンドスレッド側からは一切触れない。**

対症療法（timeout延長・retry回数変更・409の握り潰し）は一切行っていない。ブロッキングの原因そのものをイベントループから除去することでのみ解決している。

### 新しい Session Lifecycle

```
CONNECTED
  ↓ (relay loop が終了 = 切断検知)
DISCONNECTING   ← ここで _active_connection / _active_session を解放（ループスレッド上、同期）
  ↓ (loop.run_in_executor へ committed)
TEARDOWN        ← reader_thread.join() / RuntimeSession.teardown() をバックグラウンドスレッドで実行
  ↓
CLOSED
```

- `CONNECTED → DISCONNECTING → TEARDOWN → CLOSED` の4状態を `runtime_trace`（`PHANTOM_TRACE=1`）に `lifecycle_state` として出力する `Session START` / `Session DISCONNECT` / `Session TEARDOWN START` / `Session TEARDOWN END` の4イベントとして追加した。
- `_active_connection` / `_active_session` の解放は **`DISCONNECTING` の間、イベントループスレッド上で完了**し、その**後**にバックグラウンド実行が `run_in_executor` へ委譲される。したがって新しい接続がスロットの空きを観測できるタイミングは、常に「実際に空いた瞬間」と一致する（ループが凍結して観測が遅れる、という問題そのものが起きなくなる）。

### 修正後コード（抜粋）

```python
finally:
    if runtime_trace.enabled():
        runtime_trace.emit("Session DISCONNECT", session_id=_trace_session_id,
                            event_id="session-disconnect",
                            lifecycle_state=_SESSION_STATE_DISCONNECTING)
    if drain_task is not None:
        drain_task.cancel()

    # 共有状態はここ(ループスレッド)でのみ確定させる。
    with self._active_lock:
        if self._active_connection is websocket:
            self._active_connection = None
        self._active_session = None
    _log("client disconnected")

    # ブロッキング処理はバックグラウンドスレッドへ。
    loop.run_in_executor(
        None, self._finish_disconnect,
        reader_thread, reader_stop, session, _trace_session_id,
    )

def _finish_disconnect(self, reader_thread, reader_stop, session, trace_session_id) -> None:
    """default executor 上（イベントループとは別スレッド）で実行。
    TransportGateway の共有状態には一切触れない。"""
    if reader_thread is not None:
        reader_stop.set()
        reader_thread.join(timeout=2.0)
    if runtime_trace.enabled():
        runtime_trace.emit("Session TEARDOWN START", ..., lifecycle_state="TEARDOWN")
    try:
        self._session_teardown(session)
    except Exception as exc:
        _log(f"session teardown raised (session_id={trace_session_id}): {exc}")
    if runtime_trace.enabled():
        runtime_trace.emit("Session TEARDOWN END", ..., lifecycle_state="CLOSED")
```

### Thread Safety への影響

| 状態 | 書き込み元 | 検証方法 |
|---|---|---|
| `_active_connection` / `_active_session` / `_active_lock` | **イベントループスレッドのみ**（`_handler`）。`_finish_disconnect`（バックグラウンドスレッド）は読み書きしない | コードレビュー + `test_active_slot_is_free_immediately_even_while_teardown_is_still_running` |
| `RuntimeSession.teardown()` の冪等性（`_lock` + `_torn_down`フラグ） | 既存実装のまま変更なし。バックグラウンドスレッドから呼ばれても、`cloud_run_shell` の SIGTERM ハンドラから同時に呼ばれても二重実行されない | `tests/test_cloud_run_shell_session.py::test_teardown_is_idempotent`（既存、無変更） |
| `_handler()` の `try/finally` は1接続につき正確に1回しか実行されない（Python言語仕様） | → `loop.run_in_executor` も1接続につき正確に1回しかスケジュールされない | `test_teardown_called_exactly_once_never_twice` |

**CONNECTED → DISCONNECTING → TEARDOWN → CLOSED が二重実行されないことの検証**: 上記の通り、(1) 状態遷移そのものは単一コルーチンの `try/finally` の中で線形に一度だけ進む、(2) 共有スロットの解放はループスレッドのみで行われる、(3) 子プロセスの終了処理自体は既存の冪等性ガードにより二重実行されない、の3点をコードとテストの両方で確認した。

### イベントループがブロックされなくなった根拠

1. **ユニットテスト**（`tests/test_transport_gateway_session_lifecycle.py`、新規5件）: 意図的に遅い `session_teardown` スタブ（0.2秒 sleep）を注入し、
   - `_handler()` が return した直後（teardown完了を待たずに）`_active_connection`/`_active_session` が既に `None` であること
   - `session_teardown` が呼び出しスレッドとは異なるスレッドで実行されること
   - 呼び出しが正確に1回であること
   を検証。**修正前コードに対して同じテストを実行すると2件が実際に失敗する**ことを確認済み（後述 §4）。
2. **決定論的な実機再現**（後述 §4）: 実際に子プロセスへ `SIGSTOP` を送って「SIGINTに応答できない子プロセス」を現実の OS プロセスとして作り出し、`/healthz` の応答性と reconnect の成否を直接観測。
3. **20分間の実機 Operator E2E**（後述 §4）: 実 Gemini STT/LLM を使った継続運用中、4分おきに子プロセスを強制終了させて切断/再接続を4回注入し、その間 `/healthz` が一度も遅延・失敗しないことを継続観測。

---

## 3. 修正ファイル一覧

| ファイル | 変更 | 概要 |
|---|---|---|
| `src/runtime/transport_gateway.py` | +167 / -9 | `_handler` の `finally` を再構成し、ブロッキング処理を `loop.run_in_executor` へ退避。`_finish_disconnect` を新設。Session Lifecycle trace（`Session START`/`DISCONNECT`/`TEARDOWN START`/`TEARDOWN END`）を追加 |
| `tests/test_transport_gateway_session_lifecycle.py` | 新規 +263行 | 上記の非ブロッキング性・スレッド分離・呼び出し回数・trace出力を検証する回帰テスト5件 |

他のファイル（`src/audio/vad.py` 等、作業開始時点で既に変更されていたもの）は本修正の対象外であり、一切変更していない。

---

## 4. Validation 結果

### 4.1 静的検証

| 項目 | 結果 |
|---|---|
| `py_compile`（変更・追加した全 `.py`） | ✅ PASS |
| 既存ユニットテスト | ✅ **480 tests PASS**（修正前ベースラインと同数、リグレッションなし） |
| 新規ユニットテスト（`test_transport_gateway_session_lifecycle.py`） | ✅ **5 tests PASS** |
| 合計 | ✅ **485 tests PASS**（skipped=2、修正前から変化なし） |
| 新規テストを修正前コードに対して実行 | ❌ **2/5 が実際に失敗**（本当にバグを検出するテストであることを確認済み） |

### 4.2 決定論的な実機再現（SIGSTOPによる「子プロセス無応答」の直接シミュレーション）

実際に `cloud_run_shell.py` をローカルで起動し（`python -m runtime.cloud_run_shell -- --profile default --mode light --no-color --audio-source fd`）、実 WebSocket クライアントで実発話（macOS `say` で合成した音声、実 Gemini STT/LLM 経由）を1件送信してセッションを確立した後、そのセッションの実子プロセス（実 PID）に **実際に `SIGSTOP`** を送って「SIGINTに応答できない子プロセス」を意図的に再現し、WebSocket を切断、`/healthz` 応答性と reconnect 結果を直接観測した。

| | **修正前（`git show HEAD`）** | **修正後** |
|---|---|---|
| `/healthz` 最大応答時間（teardown中） | **2003.8 ms**（5秒timeoutで2/3失敗） | **10.3 ms**（全て成功） |
| 単発 reconnect | `timed out during opening handshake`（open_timeout=8s超過で失敗） | **`connected` — t+0.00秒で即成功** |
| 実運用の指数バックオフを模した3連続reconnect（t+0.2s/1.5s/3.5s） | **3/3 が全てタイムアウト** | （修正後は単発で即成功するため実施不要と判断） |
| trace上の `Session TEARDOWN START` | 観測不可（ループ自体が凍結） | 旧セッションの `TEARDOWN START` **より前**に新セッションの `Session START` が記録される＝スロット解放が即座に他接続から観測可能であることを直接確認 |

修正前コードでの再現ログ（`repro_result.json`、修正前）:
```json
{
  "reconnect_result": {"outcome": "error: timed out during opening handshake", "completed_at": 8.00},
  "healthz_log": [
    {"healthz_latency_ms": -1.0},
    {"healthz_latency_ms": -1.0},
    {"healthz_latency_ms": 2003.8237501867115}
  ]
}
```

修正後コードでの再現ログ（trace抜粋）:
```
[event:Session DISCONNECT] srv-1368  lifecycle_state=DISCONNECTING
[event:Session START]      srv-1394  lifecycle_state=CONNECTED   ← 再接続が即成功
[event:Session DISCONNECT] srv-1394  lifecycle_state=DISCONNECTING
[event:Session TEARDOWN START] srv-1368  lifecycle_state=TEARDOWN  ← 旧セッションの
                                                                     teardownはここでようやく開始
[event:Session TEARDOWN START] srv-1394  lifecycle_state=TEARDOWN
```
verdict: `max /healthz latency during teardown window: 10.3 ms` / `failed/timed-out /healthz polls: 0 / 15` / `reconnect outcome: connected (at t+0.00s after close)`

### 4.3 実機 Operator E2E（実 Gemini STT / 実 Gemini LLM、20分間、継続稼働）

- 対象: 修正後コードで起動した実 `cloud_run_shell.py`（実 `.env` の `GEMINI_API_KEY` 使用）
- クライアント: **実の、無改造の** `runtime_client.websocket_client.RuntimeWebSocketClient`
- 音声: マイク実機の代わりに macOS `say` で合成した実音声（7フレーズ、16kHz/mono/PCM16、100msブロックで実タイミング送信）をループ再生
- 4分おきに実子プロセスを `SIGKILL` して切断を注入し、実クライアントの reconnect ロジックを実際に4回駆動
- `/healthz` を1秒間隔で並行ポーリング

| 項目 | 結果 |
|---|---|
| 実施時間 | **1200.02秒（20分00秒）** |
| Gemini STT | ✅ 正常 — 実transcript **248件**取得 |
| Gemini LLM | ✅ 正常 — 実reply **209件**取得、latencyイベント244件（STT ≈2.4〜3.1秒、LLM ≈1.0〜1.6秒、典型値） |
| **`keepalive ping timeout`（close 1011）** | **0件**（クライアント/サーバー双方の全ログを走査して確認） |
| **`409`** | **0件** |
| `/healthz` 応答時間 | 1196サンプル中 最大 **10.77ms** / 最小 0.52ms / 平均 2.22ms、異常（失敗 or >200ms）**0件** |
| Session Lifecycle | `Session START`×5、`Session DISCONNECT`×5、`Session TEARDOWN START`×5、`Session TEARDOWN END`×5 — **全て対になっており欠落・二重実行なし** |
| Reconnect | 4回発生、**全て1回の試行(`attempt 1/50`)で即座に成功**。`close_reason` はいずれも `disconnected_by_server`（正常系の切断分類） |
| クライアント側 errors | 0件 |
| Dashboard（`GET /dashboard`, `GET /`） | ✅ 回帰なし — 未投入時404、`POST /aggregate` 後200・HTML表示とも正常 |
| Conversation Traceability（`conversation_line`/`speaker`/`transcript`） | ✅ 回帰なし — `GET /dashboard` JSON・`GET /` HTML の両方に正しく反映されることを確認 |

Session Lifecycle trace（実ログ、5サイクル全件）:
```
2026-07-12T05:34:00 [Session START]           srv-2587  CONNECTED
2026-07-12T05:38:01 [Session DISCONNECT]      srv-2587  DISCONNECTING
2026-07-12T05:38:01 [Session TEARDOWN START]  srv-2587  TEARDOWN
2026-07-12T05:38:01 [Session TEARDOWN END]    srv-2587  CLOSED
2026-07-12T05:38:02 [Session START]           srv-3148  CONNECTED
2026-07-12T05:42:01 [Session DISCONNECT]      srv-3148  DISCONNECTING
2026-07-12T05:42:01 [Session TEARDOWN START]  srv-3148  TEARDOWN
2026-07-12T05:42:01 [Session TEARDOWN END]    srv-3148  CLOSED
2026-07-12T05:42:02 [Session START]           srv-3654  CONNECTED
2026-07-12T05:46:01 [Session DISCONNECT]      srv-3654  DISCONNECTING
2026-07-12T05:46:01 [Session TEARDOWN START]  srv-3654  TEARDOWN
2026-07-12T05:46:01 [Session TEARDOWN END]    srv-3654  CLOSED
2026-07-12T05:46:02 [Session START]           srv-4155  CONNECTED
2026-07-12T05:50:01 [Session DISCONNECT]      srv-4155  DISCONNECTING
2026-07-12T05:50:01 [Session TEARDOWN START]  srv-4155  TEARDOWN
2026-07-12T05:50:01 [Session TEARDOWN END]    srv-4155  CLOSED
2026-07-12T05:50:02 [Session START]           srv-4646  CONNECTED
2026-07-12T05:54:00 [Session DISCONNECT]      srv-4646  DISCONNECTING
2026-07-12T05:54:02 [Session TEARDOWN START]  srv-4646  TEARDOWN
2026-07-12T05:54:08 [Session TEARDOWN END]    srv-4646  CLOSED   (実行終了時の自然なclose)
```

クライアント側ログ（切断→再接続、4回とも同一パターン）:
```
[websocket_client] connected: ws://127.0.0.1:8092/ws?provider=gemini
[websocket_client] disconnected by server; reconnecting
[websocket_client] reconnect attempt 1/50 in 1.0s
[websocket_client] connected: ws://127.0.0.1:8092/ws?provider=gemini
```

**注**: 実マイク・実Cloud Runデプロイへのアクセスがこの検証環境には無いため、マイク入力は macOS `say` による合成音声（同一のWebSocketバイナリフレーム経路）で代替し、Cloud Run本番デプロイの代わりにローカルで実プロセス（`cloud_run_shell.py`/`transport_gateway.py`/`phantom_runtime.py`、実Gemini API）を起動して検証した。トランスポート/セッションライフサイクル層のバグであり、この代替は妥当と判断したが、**Cloud Run実環境での最終確認は別途推奨する**。

---

## 5. Git Diff Summary

```
 src/runtime/transport_gateway.py               | 176 +++++++++++++++++++++++++++++++++++++--
 tests/test_transport_gateway_session_lifecycle.py | 263 ++++++++++++++++++++++++++++++++++++++++++++ (new file)
 2 files changed, 167 insertions(+), 9 deletions(-)  [transport_gateway.py]
                                                     + 263 insertions (new test file)
```

主な変更点:
- `_handler()`: `loop = asyncio.get_running_loop()` をコルーチン先頭に移動（`finally` から常時参照可能にするため）
- `_handler()` の `finally`: ブロッキング処理（`reader_thread.join()` / `session_teardown()`）を `loop.run_in_executor(None, self._finish_disconnect, ...)` へ委譲
- `_finish_disconnect()`（新設メソッド）: バックグラウンドスレッドで実行される teardown 本体。例外を握り潰さず全てログ出力しつつ、`run_in_executor` の Future が決して例外を投げないようにする
- `_SESSION_STATE_CONNECTED` / `_DISCONNECTING` / `_TEARDOWN` / `_CLOSED` の4定数を追加し、`runtime_trace` へ `lifecycle_state` として出力
- `Session START` / `Session DISCONNECT` / `Session TEARDOWN START` / `Session TEARDOWN END` の4trace呼び出しを追加

---

## 6. 再発防止策

1. **設計原則の明文化（今回のコード内コメントとして反映済み）**: `TransportGateway` は全接続を1つの asyncio イベントループで処理する。`async def` の中で `subprocess.wait()` / `Thread.join()` / その他の同期ブロッキング呼び出しを直接行うことを禁止し、必ず `loop.run_in_executor()` 経由にする。この制約は `_handler()` および `_finish_disconnect()` のdocstring/コメントに明記した。
2. **Session Lifecycle trace の常設化**: `Session START` / `DISCONNECT` / `TEARDOWN START` / `TEARDOWN END` を `PHANTOM_TRACE=1` で有効化できる形で追加した。次回同種の問題（どこかの処理がイベントループを止めている）が発生した場合、`TEARDOWN START` は出ているが `TEARDOWN END` が長時間出ない、あるいは次の `Session START` が異常に遅れる、といった形でトレースログから一目で「どこで止まっているか」を特定できる。
3. **回帰テストの追加**: `tests/test_transport_gateway_session_lifecycle.py` により、将来 `_handler()` の `finally` に再びブロッキング呼び出しが混入した場合、`test_active_slot_is_free_immediately_even_while_teardown_is_still_running` および `test_teardown_runs_on_a_different_thread_than_the_event_loop` が確実に検出する。
4. **今回のようなブロッキング混入を疑うべきシグナル**: `/healthz` の応答が単発でも200msを超える、または reconnect が `open_timeout` で丸ごとタイムアウトする（`409`にすら到達しない）場合は、真っ先に `TransportGateway` の同一イベントループ上で長時間実行されている同期処理が無いかを疑うこと。今回の実機再現で確認した通り、`409` はこの障害の**一形態**に過ぎず、タイミング次第では reconnect が単に無応答でタイムアウトする形でも現れる。
