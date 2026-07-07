# Phantom Runtime Lite Runbook

**Version:** H5-1  
**Status:** Production (Hackathon Submission)  
**Platform:** Google Cloud Run  
**Last Updated:** 2026-07-07

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
