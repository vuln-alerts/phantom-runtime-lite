# Fix Report: Gemini構成でのみ再発する `1011 keepalive timeout → reconnect → 409`

- **Status**: Investigated / Root Cause (reply truncation) Fixed & Validated / 1011→409 causal link **not established** (see §10)
- **Reported by**: takeuchi@vuln-alerts.com
- **Investigated by**: Claude Code
- **Date**: 2026-07-12
- **前提**: `docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md`（TransportGateway Session Teardown Block）は修正済み・20分Operator E2Eで検証済み。本調査はそれとは別に「Gemini構成でのみ」再発するという報告を対象とする。

---

## 1. Root Cause

### 1.1 確定して修正した原因: Gemini応答の「thinking token」による切り詰め

`GeminiProvider._to_gemini_request()`（`src/provider/gemini_provider.py`）および `GeminiSpeechProvider.transcribe()`（`src/provider/gemini_speech_provider.py`）が構築する `GenerateContentConfig` は `thinking_config` を設定していなかった。`gemini-2.5-flash` はこの場合、SDKの型定義（`google.genai.types.ThinkingConfig` docstring: *"The default values and allowed ranges are model dependent"*）が示す通り、モデル自身のデフォルトの動的思考（thinking）予算を使用する。この思考トークンは `max_output_tokens` と**同じ予算**から消費される。

`phantom_runtime.py` の `generate_reply()` は `max_tok = 60`（日本語）/ `120`（英語）/ `150`（発音モード）という値を、**非thinkingモデルの会話応答**を想定して設定している。thinkingが有効なままだと、この60〜150トークンの大半（実測で90%以上）が不可視の思考トークンに消費され、可視の応答テキストがほぼ生成されないまま `finish_reason=MAX_TOKENS` で打ち切られる。

### 1.2 なぜOpenAIでは発生せず、Geminiだけで発生するのか

| | OpenAI (`gpt-4o-mini`) | Gemini (`gemini-2.5-flash`) |
|---|---|---|
| 隠れた推論トークン消費 | なし | あり（デフォルトで動的thinking有効、`thinking_config`未指定時） |
| `max_tokens`/`max_output_tokens` の意味 | 可視の出力トークン数の上限 | **可視出力 + 不可視thinking** 合計の上限 |
| 60〜150トークンの応答予算での挙動 | 常に完結した応答を生成 | thinkingが予算の大半を消費し、応答が切り詰め/空になりやすい |

これはコード上の非対称性そのもの（`_to_gemini_request()`/`GeminiSpeechProvider.transcribe()`にのみ存在する未設定の`thinking_config`）であり、OpenAI側のコード（`_to_openai_kwargs()`）には対応する概念自体が存在しない。**"OpenAIでは発生せずGeminiだけ発生する"** のはこの非対称性が原因であり、推測ではなく以下の実測で確認済み。

### 1.3 実測による確認（本番と同一のSDK呼び出しを直接実行）

```python
# thinking_config 未設定（修正前と同一のコード経路）
config = GenerateContentConfig(temperature=0.15, max_output_tokens=60)
resp = client.models.generate_content(model='gemini-2.5-flash', contents=contents, config=config)
# => text='現在、' finish_reason=MAX_TOKENS thoughts_token_count=54 candidates_token_count=2
#    (60トークン予算のうち54トークンが不可視思考、可視出力はわずか2トークン)
# 3回連続で完全に同一の結果 -- 偶発的ではなく決定論的に再現する

# thinking_config=ThinkingConfig(thinking_budget=0) を設定
config = GenerateContentConfig(temperature=0.15, max_output_tokens=60,
                                thinking_config=ThinkingConfig(thinking_budget=0))
resp = client.models.generate_content(model='gemini-2.5-flash', contents=contents, config=config)
# => text='現在、利用人数はどれくらいを想定されていますか？' finish_reason=STOP thoughts_token_count=None
#    (完全な文が生成され、かつ実行時間も短縮: 0.94-1.31s -> 0.71-0.80s)
```

実際の18分間Operator E2E（後述 §7）でも、修正前は **450件中 59件が完全に空文字列、91件が5文字以下**（合計82%が劣化）だったのに対し、修正後は **450件中 0件が空/短文**（完全な文が生成された）ことを確認した。

### 1.4 原因コード（修正前・抜粋）

```python
# src/provider/gemini_provider.py  GeminiProvider._to_gemini_request()
config = GenerateContentConfig(
    system_instruction=system_instruction,
    temperature=request.temperature,
    max_output_tokens=request.max_tokens,   # thinking_config未設定
                                              # -> モデルのデフォルトthinking予算が
                                              #    このmax_output_tokensから消費される
)
```

```python
# src/provider/gemini_speech_provider.py  GeminiSpeechProvider.transcribe()
config = GenerateContentConfig(temperature=0.0)   # thinking_config未設定
```

---

## 2. 修正内容

`GenerateContentConfig` に `thinking_config=ThinkingConfig(thinking_budget=0)` を追加し、thinkingを明示的に無効化した。対象は以下の2箇所（Gemini LLM経路・Gemini STT経路の両方 — STT側もモデルは同じ `generate_content()` APIを使うため、同一の潜在的な思考トークン消費リスクを持つ）。

- `GeminiProvider._to_gemini_request()`（`generate()` / `generate_stream()` 両方が使用する共通のconfig構築箇所）
- `GeminiSpeechProvider.transcribe()`

いずれも会話応答・逐語書き起こしという単純タスクであり、多段階推論（thinking）を必要としない。`max_output_tokens` を引き上げる対症療法（トークン予算を増やすだけ）は行っていない — thinking予算は動的であるため、予算を増やしても可視出力が保証されないためである。timeout延長・retry追加・sleep追加は一切行っていない。

---

## 3. 修正ファイル一覧

| ファイル | 変更 |
|---|---|
| `src/provider/gemini_provider.py` | `_to_gemini_request()` に `thinking_config=ThinkingConfig(thinking_budget=0)` を追加（+29/-1） |
| `src/provider/gemini_speech_provider.py` | `transcribe()` に同様の `thinking_config` を追加 |
| `tests/test_gemini_thinking_disabled.py` | 新規 — `GeminiProvider`/`GeminiSpeechProvider` が常に `thinking_budget=0` をSDKへ渡すことを、モック経由でネットワーク接続なしに検証する回帰テスト3件 |

---

## 4. OpenAI / Gemini 比較表

| 項目 | OpenAI | Gemini（修正前） | Gemini（修正後） |
|---|---|---|---|
| 応答の完全性（450件中） | 空/極短文 0件 | 空59件・極短文91件（計150件, 82%） | 空/極短文 **0件** |
| `finish_reason` | `stop`（常に自然終了） | `MAX_TOKENS`（トークン切れによる強制終了）が支配的 | `STOP`（自然終了） |
| GPT/LLM呼び出し時間 (18分E2E, `gpt_ms`) | avg 999ms / max 1930ms | avg 1339ms / max 1907ms | **avg 838ms**（高速化）/ max 6572ms |
| STT呼び出し時間 (`stt_ms`) | avg 1403ms / max 3573ms | avg 2757ms / max **11601ms** | avg 2562ms / max **7170ms**（改善したが依然OpenAIより高め、後述§10） |
| transcript_queue drop（stale破棄） | 0件 | 4件（`pressure=MED`を複数回観測） | **0件** |
| `/healthz` 最大応答時間 | ~12ms | ~12ms | ~11ms（両者とも常に正常） |
| `1011` | 0件 | 0件（本調査の実行時間内では自然発生せず） | 0件 |
| `409` | 0件 | 0件（同上） | 0件 |
| Reconnect（強制切断注入時） | — | — | 全試行が1回目の再試行で即成功、fatalなし |

---

## 5. Runtime Trace 比較

`Speech START` / `Speech END`（`reason=silence`/`reason=force`）は本調査で使用した合成音声（発話3-8秒＋無音3秒の明確な区切り）では両プロバイダとも一貫して `reason=silence` で確定しており、VAD確定理由に provider間の差異は観測されなかった。

`transcript_queue enqueue` の `drained` フィールド（`_enqueue_latest()` が保持したまま古いチャンクを破棄した数）は、Geminiのみ0以外の値（1）を複数回記録した（OpenAIは常に0）。これは §4 の transcript_queue drop 件数と整合する — Gemini側のSTT/LLM合計処理時間がOpenAIより長いため、`transcript_queue`（maxsize=4、"~6秒のスパイクまで吸収できる"設計コメント通り）がより頻繁に飽和した。

`Session START` / `Session DISCONNECT` / `Session TEARDOWN START` / `Session TEARDOWN END`（TransportGateway側）は、Gemini構成で強制切断を注入した再接続確認（§7.3）において全て過不足なく対になっており、二重実行・取りこぼしは無かった。

---

## 6. Performance 比較（実測サマリ）

| 指標 | OpenAI | Gemini（修正後） |
|---|---|---|
| STT (`stt_ms`) | avg 1403ms, max 3573ms | avg 2562ms, max 7170ms |
| LLM (`gpt_ms`) | avg 999ms, max 1930ms | avg 838ms, max 6572ms |
| TOTAL（STT+LLM、サーバーログ実測例） | 1736-4294ms | 2850-4979ms |
| Queue overflow (`transcript_full_count`/`audio_full_count`) | 0 | 0（修正後） |
| `/healthz` | 正常（異常0件） | 正常（異常0件） |

Geminiは修正後もSTTが平均・最大ともにOpenAIより高め（サーバー側処理時間・Gemini API自体の特性によるものと考えられ、本コードベース内に原因コードは確認できなかった — §10参照）。ただし両者とも `--whisper/gemini` それぞれの30秒STTタイムアウト、20秒ping_interval+20秒ping_timeoutのkeepalive予算に対して十分小さく、本調査の実行時間内でトランスポート層の異常には至らなかった。

---

## 7. Validation結果

### 7.1 静的検証

| 項目 | 結果 |
|---|---|
| py_compile（変更・追加した全 `.py`） | ✅ PASS |
| 既存テスト | ✅ 485 tests PASS（前回セッションからの回帰なし） |
| 新規テスト（`test_gemini_thinking_disabled.py`） | ✅ 3 tests PASS（モック経由、ネットワーク接続なし） |
| 合計 | ✅ **488 tests PASS**（skipped=2） |
| `tests/test_h4_gemini_validation.py`（実Gemini APIを呼ぶ既存テスト） | ✅ 11 tests PASS（本修正後も実際のライブ呼び出しで問題なし） |

### 7.2 Operator E2E（実Gemini STT/LLM・実OpenAI STT/LLM、各18分間、並行実行）

修正前後で同一条件（同一の合成発話7フレーズをループ再生、`--audio-source fd`、実API）のOpenAI/Gemini並行比較を実施した。

**修正前比較（1080秒×2、通常運用相当・強制切断なし）**:
- 1011=0、409=0、`/healthz`異常=0（両プロバイダとも）
- Gemini: 応答182件中 空59件+極短91件（82%劣化）、STT最大11601ms、transcript_queue drop 4件
- OpenAI: 応答450件中 空/極短0件、STT最大3573ms、drop 0件

**修正後最終検証（1080秒×2、強制切断3回×2プロバイダ含む）**:
- 1011=0、409=0（`grep -wE "1011|409"` で全ログを走査し確認、単語境界一致でSTT/TOTAL ms表記との誤マッチを排除）
- Queue overflow=0（両プロバイダとも `transcript_full_count`/`audio_full_count` 相当のdrop 0件）
- `/healthz` 最大11-12ms、異常0件（両プロバイダとも）
- Gemini応答450件中 空/極短 **0件**
- Dashboard: `GET /dashboard` 未投入時404 → `POST /aggregate` 後200・内容確認、`GET /` HTML表示確認（回帰なし）
- Conversation Traceability: `conversation_line`/`speaker`/`transcript` がJSON/HTML双方に反映されることを確認（回帰なし）
- Verification Runtime / Trust Runtime: `DashboardResult` に `gap_detected`/`trust_score`/`trust_level` が含まれることを確認

### 7.3 Reconnect確認（Gemini、7分間、150秒毎に強制切断×2回）

初回の並行検証では、テストハーネス自身の `pgrep -f phantom_runtime.py` が2つの並行サーバー（Gemini/OpenAI）の子プロセスを区別できず、意図しないプロセスを強制終了していたことが判明した（本番コードの不具合ではなくテストツール側の不具合。`REPRO_SERVER_PID` による親プロセス限定の `pgrep -P` に修正）。修正後、単一のGeminiサーバーに対して再実施:

```
[websocket_client] connected: ws://127.0.0.1:9101/ws?provider=gemini
[e2e] injecting forced disconnect: killing child pid=14643
[websocket_client] disconnected by server; reconnecting
[websocket_client] reconnect attempt 1/50 in 1.0s
[websocket_client] connected: ws://127.0.0.1:9101/ws?provider=gemini
[e2e] injecting forced disconnect: killing child pid=14973
[websocket_client] disconnected by server; reconnecting
[websocket_client] reconnect attempt 1/50 in 1.0s
[websocket_client] connected: ws://127.0.0.1:9101/ws?provider=gemini
```

サーバー側 Session Lifecycle trace:
```
[Session DISCONNECT]      srv-15267  DISCONNECTING
[Session TEARDOWN START]  srv-15267  TEARDOWN
[Session TEARDOWN END]    srv-15267  CLOSED
```

2回とも `attempt 1/50` で即座に再接続成功、`fatal`/`1011`/`409` は一切発生しなかった。

---

## 8. Git Diff Summary

```
 src/provider/gemini_provider.py         | 29 ++++++++++++++++++++++++++++- (+29/-1, tracked file)
 src/provider/gemini_speech_provider.py  | thinking_config 追加 (このセッション開始時点で未コミットの新規ファイルへの追加変更)
 tests/test_gemini_thinking_disabled.py  | 新規 +115行、3テスト
```

主な変更点:
- `GeminiProvider._to_gemini_request()`: `GenerateContentConfig` に `thinking_config=ThinkingConfig(thinking_budget=0)` を追加
- `GeminiSpeechProvider.transcribe()`: 同様に `thinking_config` を追加
- `ThinkingConfig` のimportを両ファイルに追加
- タイムアウト値・リトライ回数・sleep・Queueサイズ・keepalive設定は一切変更していない

---

## 9. 再発防止策

1. **Provider横断のレビュー観点**: 新しいLLM Provider（Gemini/OpenAI以外を含む将来の追加）を実装する際、そのSDKが「隠れたトークン消費（reasoning/thinkingなど）」を持つかどうかを必ず確認し、`max_tokens`相当の設定が可視出力のみを制御するのか、不可視の内部処理と共有される予算なのかを明示的に確認する。共有される場合は、リアルタイム会話用途では明示的に無効化する。
2. **回帰テスト**: `tests/test_gemini_thinking_disabled.py` が、将来 `thinking_config` の設定が誤って削除された場合に検出する。
3. **品質モニタリング**: 空文字列/極短文の `reply` Typed Eventの発生率は、プロバイダ設定の劣化を示す先行指標になりうる。本調査で使用した集計手法（`replies`のうち空文字列・5文字以下の割合）を、将来のProduction Verification Runbook（`docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §11.7）のGemini STT/LLMチェック項目に加えることを推奨する。

---

## 10. 1011/409 との因果関係について（重要・誠実な報告）

**本調査では、上記の「thinking tokenによる応答切り詰め」を含むあらゆるコード経路について、Gemini構成での `1011 keepalive timeout → reconnect → 409` を、実際のGemini API・実際のOpenAI API双方を用いた長時間実行（修正前比較18分×2、修正後最終検証18分×2、再接続確認7分、合計約61分の実API実行）の中で、意図的な誘発（子プロセスの強制終了によるreconnect注入を含む）を行ってもなお、直接には再現できなかった。**

調査した範囲と結論:

- **TransportGatewayのイベントループブロック**: `/healthz` は全実行を通じて最大12ms、異常0件。イベントループのブロックは一切観測されなかった（前回修正が正しく機能している）。
- **reply_workerの処理待ち**: Gemini/OpenAIとも `gpt_ms`/`stt_ms` は全実行を通じてSTT 30秒・LLM 45秒のタイムアウト、keepalive予算（ping_interval 20秒+ping_timeout 20秒=40秒）に対して十分小さく、reply_workerが子プロセスのSIGINT応答を長時間妨げる状況は観測されなかった。
- **Queueの詰まり**: Gemini側でtranscript_queueのdrop（4件、修正前）を確認したが、これはグレースフルな古いチャンク破棄であり、ハングやブロックではない。修正後は0件。
- **thinking tokenによる応答切り詰め**: 定量的に確認・修正した（§1-§7）。ただしこれ自体は「応答が空/短くなる」品質劣化であり、WebSocket接続やイベントループを直接ブロックする経路をコード上確認できなかった。

以上より、本調査で確定的に特定・修正できたのは「Gemini構成でのみ発生する重大な応答品質劣化（thinking token浪費）」であり、これは高い確度で**"Geminiのときだけ様子がおかしい"という運用上の実感の一因**であった可能性が高いが、報告されている `1011→409` という特定のWebSocketレベルの事象そのものへの直接的な因果関係は、本調査の実行時間内では確定できなかった。

**推奨事項**: 本修正の適用後も `1011→409` が再現する場合は、発生時に `PHANTOM_TRACE=1` / `PHANTOM_TRACE_FILE` を有効化した状態のトレースログ（`docs/RUNBOOK_PRODUCTION_VERIFICATION.md` §11.2/§11.5 記載の `Session START`/`DISCONNECT`/`TEARDOWN START`/`TEARDOWN END`、`Speech START`/`END`、`reason=silence`/`force` を含む）を実際の発生時刻とともに提供いただきたい。実発生時のトレースがあれば、本調査のような長時間の確率的再現待ちではなく、該当箇所を機械的に特定できる。
