# Phantom Runtime Lite Runbook

**Version:** H5-1.1  
**Status:** Production (Hackathon Submission)  
**Platform:** Google Cloud Run  
**Last Updated:** 2026-07-12（OpenAI正式デモ構成・Gemini Known Issue明記、Release Candidate Review指摘の是正）

---

# 1. Purpose

本Runbookは Phantom Runtime Lite の構築・デプロイ・運用・障害切り分け手順を示します。

本Runbookのみで以下を実施できます。

- Google Cloud 環境確認
- Docker Build
- Docker Push
- Cloud Run Deploy
- API Key設定
- Runtime起動確認
- Provider Routing確認
- WebSocket接続確認
- Shutdown確認
- 障害切り分け

---

# 2. System Overview

```
Browser Client
      │
      ▼
Cloud Run
Transport Gateway
      │
Provider Router
      │
RuntimeSession
      │
Runtime Child
(phantom_runtime.py)
      │
Typed Events
```

Providerは接続毎に指定します。

```
wss://<HOST>/ws?provider=openai

または

wss://<HOST>/ws?provider=gemini
```

環境変数によるProvider切替は行いません。

**Provider構成の位置付け**: 本Hackathon提出における正式デモ構成は **OpenAI**（`provider=openai`）である。Geminiは動作するが、WebSocketの`1011`（keepalive ping timeout）→ 再接続時`409`（reconnect conflict）が再発する既知の未解決事象（Status: Open）があるため、**Known Issue**として扱う（[docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md](bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md)参照。OpenAI構成では現時点で本事象は未再現）。§11.2/§11.3・§17ではOpenAI/Geminiの接続確認手順自体は引き続き両方記載するが、Operator E2E・Demoの正式実施はOpenAI構成で行う。

---

# 3. Prerequisites

以下がインストール済みであること。

- Python 3.13+
- Docker
- Google Cloud SDK
- Git

Google Cloudへログイン済みであること。

---

# 4. Google Cloud Environment

## 4.1 Login確認

目的

Cloud RunへアクセスするGoogleアカウントを確認する。

実行

```bash
gcloud auth list
```

期待

```
ACTIVE

takeuchi@vuln-alerts.com
```

異常

ACTIVEが異なる場合

```bash
gcloud config set account takeuchi@vuln-alerts.com
```

---

## 4.2 Project確認

実行

```bash
gcloud config get-value project
```

期待

```
phantom-runtime-lite
```

異常

Projectが違う場合

```bash
gcloud config set project phantom-runtime-lite
```

---

# 5. Cloud Run

## 5.1 Service存在確認

実行

```bash
gcloud run services list \
  --region asia-northeast1
```

期待

```
phantom-runtime-lite
```

正常

Serviceが表示される。

---

## 5.2 Cloud Run URL取得

実行

```bash
gcloud run services describe phantom-runtime-lite \
  --region asia-northeast1 \
  --format="value(status.url)"
```

期待

```
https://phantom-runtime-lite-395126859945.asia-northeast1.run.app
```

以降

```
<CLOUD_RUN_URL>
```

として使用する。

WebSocket接続時は

```
https://

↓

wss://
```

へ変更して使用する。

例

```
https://phantom-runtime-lite-395126859945.asia-northeast1.run.app

↓

wss://phantom-runtime-lite-395126859945.asia-northeast1.run.app
```

---

## 5.3 Service設定確認

実行

```bash
gcloud run services describe phantom-runtime-lite \
  --region asia-northeast1
```

期待

| Item | Expected |
|------|----------|
| Ingress | all |
| Traffic | 100% Latest Revision |
| Port | 8080 |
| Memory | 512Mi |
| CPU | 1 |

---

# 6. API Keys

## 6.1 OpenAI API Key設定

```bash
gcloud run services update phantom-runtime-lite \
  --region asia-northeast1 \
  --update-env-vars OPENAI_API_KEY="<YOUR_OPENAI_API_KEY>"
```

例

```
sk-proj-xxxxxxxx
```

---

## 6.2 Gemini API Key設定

```bash
gcloud run services update phantom-runtime-lite \
  --region asia-northeast1 \
  --update-env-vars GEMINI_API_KEY="<YOUR_GEMINI_API_KEY>"
```

例

```
AIzaSyxxxxxxxx
```

または

```
AQ.Abxxxxxxxx
```

（利用するGemini APIキー形式に従う）

---

## 6.3 同時設定

```bash
gcloud run services update phantom-runtime-lite \
  --region asia-northeast1 \
  --update-env-vars \
OPENAI_API_KEY="<YOUR_OPENAI_API_KEY>",\
GEMINI_API_KEY="<YOUR_GEMINI_API_KEY>"
```

---

## 6.4 設定確認

実行

```bash
gcloud run services describe phantom-runtime-lite \
  --region asia-northeast1
```

期待

```
Env vars

OPENAI_API_KEY
GEMINI_API_KEY
```

確認

```
PROVIDER
```

は存在しないこと。

---

# 7. Docker Build

実行

```bash
docker build \
  --platform linux/amd64 \
  -t asia-northeast1-docker.pkg.dev/phantom-runtime-lite/phantom-runtime-lite/phantom-runtime-lite:hackathon .
```

期待

```
Successfully built
```

## 7.1 Local Docker Run（Cloud Run代替 / gcloud不要、Phase 4で実施・検証済み）

`gcloud`認証やCloud Runへのデプロイなしで、同一Dockerfileをローカルで起動し
Runtime Clientから接続してE2E検証できる。実際に2026-07-09に実行し、
OpenAI/Gemini両Providerで実音声（STT）→LLM応答→Typed Event→Client TTSの
往復を確認済み（詳細は§16 Known Limitationsおよび
`docs/MIGRATION_MATRIX.md`の2026-07-09エントリを参照）。

実行

```bash
docker build --platform linux/amd64 -t phantom-runtime-lite:local .

docker run --rm --name phantom-local -p 8080:8080 \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  -e PORT=8080 \
  phantom-runtime-lite:local
```

別ターミナルでヘルスチェック

```bash
curl -i http://localhost:8080/healthz
```

期待

```
HTTP/1.1 200 OK
ok
```

Runtime Clientから接続（別ターミナル、`src/`から実行）

```bash
python -m runtime_client --url http://localhost:8080 --provider openai \
  --tts say --output-device "BlackHole"
```

注記: Apple Siliconでは`--platform linux/amd64`によりエミュレーション実行となるため
実機Cloud Runより起動・応答が多少遅くなる。機能検証目的では問題なし。

---

# 8. Docker Push

実行

```bash
docker push \
asia-northeast1-docker.pkg.dev/phantom-runtime-lite/phantom-runtime-lite/phantom-runtime-lite:hackathon
```

期待

```
digest:
```

が表示される。

---

# 9. Cloud Run Deploy

実行

```bash
gcloud run deploy phantom-runtime-lite \
  --image asia-northeast1-docker.pkg.dev/phantom-runtime-lite/phantom-runtime-lite/phantom-runtime-lite:hackathon \
  --region asia-northeast1 \
  --remove-env-vars=PROVIDER
```

期待

```
Creating Revision

Routing traffic

Done
```

---

# 10. Runtime Startup

## 10.1 Health Check

実行

```bash
curl -i <CLOUD_RUN_URL>/
```

期待

```
HTTP/2 200

ok
```

異常

| Status | 原因 |
|---------|------|
|404|URL誤り|
|503|Runtime起動中|

---

## 10.2 Runtime Log

実行

```bash
gcloud run services logs read phantom-runtime-lite \
  --region asia-northeast1 \
  --limit=50
```

期待

```
transport gateway listening

runtime child started

readiness = healthy
```

---

# 11. WebSocket Validation

Cloud RunはHTTPSを利用するため

```
ws://
```

ではなく

```
wss://
```

を利用する。

---

## 11.1 websocatインストール

macOS

```bash
brew install websocat
```

---

## 11.2 OpenAI接続

```bash
websocat \
"wss://phantom-runtime-lite-395126859945.asia-northeast1.run.app/ws?provider=openai"
```

期待

- 接続成功
- RuntimeSession生成
- Runtime Child生成

---

## 11.3 Gemini接続

```bash
websocat \
"wss://phantom-runtime-lite-395126859945.asia-northeast1.run.app/ws?provider=gemini"
```

期待

- 接続成功
- RuntimeSession生成

---

## 11.4 不正Provider

```
provider=claude
```

期待

```
HTTP400
```

---

## 11.5 Provider未指定

```
/ws
```

期待

```
HTTP400
```

---

## 11.6 Runtime Client E2E（ローカル、Phase 4で実施・検証済み、2026-07-09）

`websocat`は生WebSocketの疎通のみ確認するため、実際のRuntime Client（音声
キャプチャ・Keyboard UX・TTS再生を含む）でのE2Eには使えない。マイクを使わず
実音声を流し込むため、Phase 3で実装したBlackHoleルーティングとTTS
Providerを利用し、`say`で合成音声をBlackHoleへ再生 → Runtime Clientが
BlackHoleを入力デバイスとしてキャプチャ、という手順で実施した。

実行（§7.1のローカルコンテナ起動後、`src/`から）

```bash
python -m runtime_client --url http://localhost:8080 --provider openai \
  --input-device BlackHole --tts say --output-device "MacBook Proのスピーカー" &

# 別プロセスで、Phase 3のSayTTSProviderを使いBlackHoleへ合成音声を再生
python3 -c "
import sys; sys.path.insert(0, '.')
from runtime_client.output_device import resolve_output_device_id
from runtime_client.tts import SayTTSProvider
tts = SayTTSProvider(device_id=resolve_output_device_id('BlackHole'))
tts.speak('Today we discussed the project timeline. Sarah will own the deployment task.')
"
```

結果（OpenAI / Gemini 両方で実施）

- Mic(BlackHole) → Runtime Client → WebSocket → ローカルCloud Run → Whisper STT → 実応答
- `g`（ミーティング分析）: OpenAI/Gemini双方で構造化された分析結果（`analysis` Typed Event）が
  Runtime Clientに届き、コンソールに正しくレンダリングされることを確認
- `s`（状態表示 / TTS停止）: `state=idle mode=OBSERVER tts=say` + 録音状態が
  Keyboard UX仕様どおりに表示されることを確認（既存UX変更なし）
- Client側TTS: `reply` Typed Event受信後、実際に音声再生（`say`経由）が
  行われることを確認

既知の問題（§16参照）: `G`（インタビューまとめ生成）はサーバー側コンソールには
正しく出力されるが、**Typed Eventとして送出されておらずRuntime Clientには届かない**。
Gemini応答が一部のターンで単語単位に短く途切れる事象を観測（プロンプト/ストリーミング
パース側の挙動と推測、Client側の問題ではない）。

---

# 12. Runtime Session

期待

```
WebSocket

↓

Provider Router

↓

RuntimeSession

↓

Runtime Child

↓

Typed Events

↓

Disconnect

↓

Runtime Child Exit

↓

FD Close
```

正常

- Childリークなし
- FDリークなし

---

# 13. Single Runtime Policy

接続中

↓

2つ目接続

期待

```
409 Conflict
```

切断後

↓

再接続成功

---

# 14. Shutdown

SIGTERM

期待

```
Ready=false

↓

503

↓

Session teardown

↓

Runtime Child終了

↓

FD Close

↓

Cloud Run終了
```

---

# 15. Troubleshooting

## HTTP400

原因

- Provider未指定
- Provider不正

---

## HTTP409

原因

Single Runtime Policy

---

## HTTP503

原因

Runtime起動中

Shutdown中

---

## OpenAI Error

確認

```
OPENAI_API_KEY
```

---

## Gemini Error

確認

```
GEMINI_API_KEY
```

---

## Runtime Child起動失敗

Cloud Run Logs

```
SessionSpawnError
```

を確認する。

---

## WebSocket接続失敗

Cloud Runでは

```
wss://
```

を利用すること。

```
ws://
```

は使用しない。

---

# 16. Known Limitations

- H4 ExtensionはFuture Work
- Whisper(STT)はOpenAIを利用
- Runtimeは単一モジュール
- Provider RoutingはSession単位
- ~~[Phase 4で確認, 2026-07-09] Summary（`G`キー/`generate_summary`）はTyped Eventとして
  送出されない。~~ **[Phase 4-1で修正, 2026-07-09]** `phantom_runtime.py`の
  `generate_summary()`に`_emit_event("analysis", text=summary)`を追加（`generate_meeting_analysis()`
  と同じイベントタイプ・同じ`text`キー）。ローカルDocker E2Eで`G`キー押下後に
  Runtime Client側へ`[分析結果]`ブロックとしてSummary内容が実際に届くことを再検証済み
  （§11.6参照）。`docs/MIGRATION_MATRIX.md`の2026-07-09 Phase 4-1エントリに詳細。
- **[Phase 4で確認, 2026-07-09] Geminiの応答が一部ターンで単語単位に短く途切れる**
  （例: "はい、" "今日" "サラ" "立ち"）。OpenAIでは同一入力に対しフル文の応答が返る。
  ローカルDocker E2E（§11.6）で再現。原因未特定（プロンプト/ストリーミングパース側の
  挙動と推測）。Client側の実装に起因するものではない（同一のTyped Event処理コードで
  OpenAIは正常動作）。
- **[Phase 4で確認, 2026-07-09] TTS再生中に稀に`PaMacCore (AUHAL) err=-50`がstderrに出る。**
  連続した`reply` Typed Eventに対しClient側TTSが短時間に連続して`speak()`を呼ぶ際、
  PortAudio側のストリーム再オープンが競合して起きる非致命的なエラー（例外は発生せず、
  該当発話の再生のみスキップされ、後続の発話は正常に再生される）。ローカルE2Eで2回観測。
- **[2026-07-12確認、Status: Open] Gemini構成で、WebSocketの`1011`（keepalive ping timeout）→
  再接続時`409`（reconnect conflict）が再発する既知の未解決事象がある。** OpenAI構成では
  現時点で未再現。TransportGateway teardown修正（`docs/bugs/FIX-2026-07-12-transport-gateway-session-teardown-blocks-event-loop.md`）
  適用後も再発しており、原因（TransportGateway/Gemini SDK/WebSocketライブラリ/runtime_client/
  reply_workerのいずれか）は未特定。この未解決事象があるため、**本Hackathon提出の正式デモ構成は
  OpenAIとし、Geminiは Known Issue として扱う**。詳細は
  [docs/bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md](bugs/BUG-2026-07-12-gemini-websocket-1011-keepalive-409-reconnect-recurrence.md)を参照。

---

# 17. Acceptance Criteria

以下を満たすこと。

- Google Cloud Login正常
- Project正常
- Docker Build成功
- Docker Push成功
- Cloud Run Deploy成功
- OPENAI_API_KEY設定済み
- GEMINI_API_KEY設定済み
- Cloud Run URL取得可能
- HTTP200応答
- Runtime正常起動
- OpenAI接続成功
- Gemini接続成功
- Invalid ProviderがHTTP400
- Single Runtime PolicyがHTTP409
- Runtime Child正常終了
- READMEと整合
- Unit Test PASS
- Scenario Test PASS
- Acceptance Test PASS
- Final Validation PASS

## 17.1 Phase 4 実施状況（2026-07-09）

| 項目 | 状態 | 備考 |
|---|---|---|
| ローカルCloud Run（Docker）接続確認 | 完了 | §7.1・§11.6。gcloud不要 |
| Cloud Run本番接続確認 | **未実施** | `gcloud auth`の再認証がインタラクティブ入力必須のため非対話環境では実行不能。`gcloud auth login`の実行が必要 |
| Zoom/BlackHole動作確認 | **未実施** | 実際にZoomアプリを開き人間が音声を聞いて確認する必要があるため、エージェント単体では検証不能。BlackHoleへのルーティング自体はPhase 3で実装・単体検証済み、本PhaseのローカルE2Eでも同一経路（BlackHole入力キャプチャ）を実際に使用し疎通確認済み |
| OpenAI接続・応答 | 完了 | §11.6。実音声→STT→GPT応答を確認 |
| Gemini接続・応答 | 完了 | §11.6。応答が短く途切れる事象を観測（Known Limitations参照） |
| Recording | 完了 | `r`キー相当のVADBuffer既定ON状態を`s`キーで確認、Control Event疎通はPhase 3で単体検証済み |
| Meeting Analysis | 完了 | OpenAI/Gemini双方でTyped Event経由の受信・表示を確認 |
| Summary | **完了（Phase 4-1で修正）** | Phase 4で発見した不備（Typed Event未送出）をPhase 4-1で修正し、`G`キー押下後にRuntime Client側で`[分析結果]`として受信・表示されることをローカルDocker E2Eで再検証済み |
| Keyboard UX完全一致確認 | 完了 | 実サーバー接続下で`s`/`l`/`g`/`G`/`q`の表示・挙動が既存仕様と一致することを確認 |
| Cloud Run Deploy | **未実施（要承認）** | `gcloud auth`再認証に加え、本番インフラへの変更は実行前にユーザー確認が必須 |
| Live Validation（本番） | **未実施** | Deploy未実施のため実施不能 |
