# Hackathon Submission Notes

> **Status:** ドキュメントのみ。本ファイルの追加にあたりコード・テスト・README・ROADMAPへの変更は一切行っていない。
> **Purpose:** Hackathon提出版の現状(実装済み範囲)、既知の制約・課題、および提出後の改善計画・設計方針を明示的に記録する。

---

## 1. Current Status

Hackathon提出版として実装済みの主な構成要素。

- **Runtime (Client)** — `src/runtime_client/`。マイク入力 → Recording Gate → Speech Gate → WebSocket送信までを担う、Cloud Run Serverと対をなすローカルRuntime。
- **Cloud Run** — `Dockerfile` / `docs/RUNBOOK.md`によりコンテナ化され、Cloud Run上にデプロイ可能。`runtime/cloud_run_shell.py`がマイクを持たないCloud Run環境向けに`phantom_runtime.py`を子プロセスとして起動し、リモートClientからの音声をfd経由で受け渡す。
- **Streaming** — Runtime Client(`AudioBridge`)からWebSocket経由でCloud Run Serverへ、100msブロック単位のPCM16LE音声をストリーミング送信する経路。
- **FastAPI** — `src/api/api_server.py`。EventAggregateを読み取り専用・ステートレスに公開するHTTPプレゼンテーション層(`GET /health`, `POST /aggregate`)。
- **Verification Runtime** — `src/verification/verification_runtime.py`。Typed Event(RuntimeEvent)を入力に、Gap検出・Fallback検出等を行いVerificationResultを生成する、読み取り専用の下流Runtime。
- **Trust Layer** — `src/trust/trust_runtime.py`。VerificationResultを入力に、Trust Policy(重み付けルールセット)を通してtrust_score/trust_level/human_review_requiredを算出する、独立した読み取り専用Runtime。
- **Dashboard / Event Aggregator** — `src/dashboard/dashboard_runtime.py` / `src/aggregator/event_aggregator.py`。VerificationResult・TrustResultを読み取り専用に集約・可視化する下流レイヤー。
- **Typed Events** — `docs/H4_RUNTIME_EVENT_CONTRACT.md`で定義されたイベント契約。`src/runtime_client/typed_event.py`が受信・描画を担当し、Verification/Trust/Dashboard/Aggregator/FastAPIの各層はこのTyped Eventを唯一の入力とする。
- **Production Verification** — `docs/RUNBOOK_PRODUCTION_VERIFICATION.md`。実機マイク・実Cloud Run環境でStartup Calibration・WebSocket接続・Transcript生成・応答までを検証する手順とチェックリスト。`--production-verification`フラグおよびデバッグログ計装(`PHANTOM_CALIBRATION_DEBUG`等)を含む。
- **Dynamic Calibration** — `src/runtime_client/calibration.py`。起動時に静寂区間のRMSを実測し、Noise Floor(p90パーセンタイル)を導出する仕組み(`NoiseFloorSampler` / `EnvironmentObserver`)。固定値を用いない設計。
- **Adaptive Speech Gate** — `CalibrationEngine`がNoise Floorから`clamp(noise_floor × multiplier, min, max)`でSpeech Gateを導出し、`AudioBridge`の送信判定に利用する。Production Verificationの実測データ(マイク別のNoise Floor/会話RMS比)に基づき、倍率(`DEFAULT_SPEECH_GATE_MULTIPLIER`)を複数回にわたり再較正している。
- **Meeting Analysis** — `generate_meeting_analysis()`(`src/phantom_runtime.py`)。トランスクリプトの増分をLLMに渡し、サマリー・リスク・質問・推奨アクション・確認事実を構造化出力する機能。Memory Layer(Rolling Summary / Question / Decision / Subject / Fact)との連携を含む。

---

## 2. Known Limitations

現在のコード・実測から確認できている制約。推測は含まない。

- **Speech Gateの倍率は限られた実測データに基づく再較正であり、汎用的な保証はない。** Production Verificationで確認できたのは2機種のマイク(MacBook Pro内蔵マイク、外部USBマイク)の実測データのみであり、現行の`DEFAULT_SPEECH_GATE_MULTIPLIER`はこの2点の観測比率(1.7〜1.8倍)を根拠に設定されている。異なるマイク・環境での再検証は未実施。
- **Calibration時とStreaming時で、物理的に異なる`InputStream`セッションを使用している。** `_perform_startup_calibration()`は専用の短命な`AudioCapture`でストリームを開き、Calibration完了後に明示的にクローズする。その後`AudioBridge`が別の`AudioCapture`インスタンスで新たにストリームを開く。設定パラメータ(samplerate/channels/blocksize/device_id)は完全に一致することをコードで確認済みだが、2つの独立したストリームセッション間でハードウェア/OS側のゲイン状態が保証された形で引き継がれる仕組みはコード上存在しない。
- **Meeting Analysisは、セッション・時間範囲でスコープされないMemoryストアを参照する。** `memory_build_context()`は`rolling_summary.json` / `question_memory.json` / `decision_memory.json` / `subject_registry.json` / `fact_memory.json`を、セッションID・時刻によるフィルタなしに(直近N件、または全件)読み込み、今回のTranscriptより前にプロンプトへ結合する。同一のCloud Runコンテナインスタンスが複数セッションを跨いで稼働する場合、過去セッションの内容が後続セッションの分析入力に含まれ得る。
- **Meeting AnalysisのMemoryストアは、LLM出力を再帰的に取り込む構造になっている。** `_memory_extract_and_save()`は、Meeting AnalysisのLLM出力テキストからセクションを抽出してMemoryへ保存する(Transcript原文そのものは保存しない)。保存された内容は次回以降の`memory_build_context()`呼び出しで再度プロンプトに含まれる。
- **Speaker識別は音声(声紋)ベースではなく、言語・会話状態に基づくヒューリスティック(`_infer_speaker()`)である。** 複数話者が同一言語で発話するケースの区別は行われない。
- **Transcript品質は環境ノイズ・マイク特性に依存する。** Speech Gateの導出がNoise Floorの実測値に基づく以上、マイクの自己ノイズや部屋の暗騒音レベルが結果に直接影響する。

---

## 3. Known Bugs

現在、コードレベルで不具合として確定的に再現・特定できているもの。

- 該当なし(本ドキュメント作成時点で、再現条件・原因箇所ともにコード上確定できている不具合は確認されていない)。

### Observed Behavior(バグと断定できないもの)

- **Meeting Analysisの出力に、今回のTranscriptに含まれない内容が現れるケースが実機で観測された。** 原因調査の結果、`memory_build_context()`がセッションスコープなしにMemoryストアを結合するコードパスの存在は確認できたが、実際にCloud Run本番インスタンス上で使用されていたMemoryストアの内容そのものは確認できておらず、当該出力がMemory由来であると断定するには至っていない。
- **同一マイク・同一環境でも、Calibration時に測定したNoise Floorと、Streaming時に観測される実際の会話RMSの関係が、想定より低い比率で観測されたケースがある。** RMS計算式の不一致はコード上否定されているが、Root Causeは現時点では特定されておらず、追加調査が必要である。

---

## 4. Planned Improvements

### Priority High

- Transcript fidelity improvement(マイク・環境差に対するSpeech Gate導出のさらなる実機検証・再較正)
- Meeting Analysis入力の透明性向上(Memory由来部分とTranscript由来部分をログ上・出力上で明確に分離する仕組み)
- Prompt最適化(Meeting Analysisのsystem/user content構成の見直し)

### Priority Medium

- Speaker diarization(音声特徴に基づく話者識別への置き換え検討)
- Context filtering(Meeting AnalysisのMemory Context取得に、時間範囲・セッション単位のフィルタを導入する設計検討)
- Memory optimization(長時間MeetingにおけるMemoryストアの取得件数・結合ロジックの見直し)

### Priority Low

- UI improvements(Runtime Client端末出力・Dashboard表示の改善)
- Analytics enhancements(Verification/Trust/Dashboardレイヤーの可視化拡張)

---

## 5. Design Principles

Hackathon提出版から一貫して維持している設計原則。

- **Typed Events** — Runtime・Verification・Trust・Dashboard・FastAPIの各層は、明示的に定義されたTyped Event / VerificationResult / TrustResult / DashboardResultのみを介して連携し、内部状態への直接アクセスを行わない。
- **Verification First** — 各下流レイヤー(Verification / Trust / Dashboard / Aggregator)は読み取り専用であり、Runtimeロジック・Provider呼び出し・Whisper呼び出しを一切行わない。
- **Backward Compatibility** — 既存の呼び出し元・既存テストへの影響を最小化する変更(定数値の再較正、デフォルト引数の追加等)を優先し、既存APIシグネチャの破壊的変更を避ける。
- **Incremental Evolution** — 固定値による判定よりもRuntimeによる実測・観測を優先し(Dynamic Calibration)、問題が実機検証で確認されるたびに最小差分で段階的に改善する。
- **Cloud Run Compatibility** — Server側は`runtime/cloud_run_shell.py`を介してマイク非搭載のCloud Run環境と互換性を保ち、音声入力はリモートClientからのfd経由ストリームとして扱う。

---

## 6. Out of Scope

Hackathon提出版では対象外とする範囲。

- Multi-language optimization(日本語以外の言語に対するMeeting Analysis出力の最適化)
- Enterprise Authentication(組織向け認証・認可基盤)
- Distributed Runtime(複数Runtimeインスタンスの協調動作)
- Multi-node execution(複数ノードにまたがる実行基盤)
- Horizontal scaling optimization(Cloud Runの水平スケーリングに特化したチューニング)

---

## 7. Future Vision

Phantom Runtimeは、固定値に依存せず実行環境を観測してから振る舞いを決める、という思想(Dynamic Calibration / Adaptive Speech Gate)を出発点としている。今後はこの思想を、Speech Gateという単一機能に閉じず、Meeting AnalysisのMemory参照範囲や、Verification/Trust層の判定ロジックなど、Runtime全体の「環境・文脈に応じて適応する」設計へと段階的に広げていく。あわせて、Verification Runtime・Trust Runtime・Dashboardという読み取り専用の下流レイヤー群を軸に、Runtime本体を変更せずに信頼性・説明可能性を継続的に強化できる構成を維持していく。
