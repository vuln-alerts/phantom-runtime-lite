# Implementation Plan — P5-4 Adaptive Runtime Calibration

> **入力仕様書:** `docs/designs/P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md`(Version 1.0、正式採用済み。本ドキュメントは変更しない)
> **本ドキュメントの位置づけ:** 「どう実装するか」ではなく「どの順番で、安全に実装するか」を整理する計画書。設計内容(思想・数値・状態遷移・UI文言)は一切変更・再解釈しない。
> **本フェーズでの実施内容:** 計画作成のみ。コード変更・テスト変更・設計書変更・コミットは行っていない。

---

## 1. 実装対象ファイル

仕様書の Scope(`src/runtime_client/` のみ、Server 側は変更しない)に基づき分類する。

### 1.1 新規作成

| ファイル | 役割 | 対応する設計章 |
|---|---|---|
| `src/runtime_client/calibration.py` | `NoiseFloorSampler` / `CalibrationEngine` / `CalibrationState` を格納する新規モジュール。Noise Floor 測定、Speech Gate 導出、状態機械、ドリフト監視を一元的に持つ | §6, §7 |
| `tests/test_runtime_client_calibration.py` | `calibration.py` の Unit Test(実装フェーズで作成。本計画書では対象範囲の特定のみ) | §6.1〜§6.4, §7 |

### 1.2 変更

| ファイル | 変更内容 | 対応する設計章 |
|---|---|---|
| `src/runtime_client/audio_bridge.py` | `_run_pump()` が固定 `self._silence_rms_threshold` を参照する箇所を、`CalibrationEngine` から取得する動的な Speech Gate に置き換える。判定結果(pass/reject)を `CalibrationEngine` のドリフト監視へフィードバックする経路を追加する | §5, §6.4, §10.1 |
| `src/runtime_client/keyboard_bridge.py` | 手動再キャリブレーションキーのハンドラを `CalibrationEngine` へ接続する(既存の `recording_active` の受け渡しパターンを踏襲)。実際の再キャリブレーション処理ロジックはすべてこのファイル(または `calibration.py`)側に閉じ込める | §6.4, FR-7 |
| `src/ui/keyboard.py` | 新規キー(例: `c`)のディスパッチ分岐を **1行だけ** 追加する。**詳細な要否レビューは §1.2.1 を参照** | §6.4, FR-7 |
| `src/runtime_client/main.py` | `_amain()` の起動シーケンスに、通常運転開始前の Environment Observation フェーズ(ブロッキング)を挿入する。`AudioBridge` へ `CalibrationEngine` を渡す配線を追加する | §5.1, §5.2 |
| `src/runtime_client/config.py` | `DEFAULT_SILENCE_RMS_THRESHOLD`(固定値)の扱いを、§9.1 の Fallback 値としての位置づけに再整理する。倍率(3.0)・クランプ範囲(150, 2500)・観測窓長(2.5秒)・ドリフト監視窓(10秒)などの新規定数を追加する | §6.2, §6.3, §6.4 |
| `src/runtime_client/typed_event.py` | §8 の5画面(起動時/中/完了/エラー/再実施)を表示するための表示処理を追加する。既存の `show_info`/`show_warn` トーンとの一貫性(UI-5)を維持する形で拡張する | §8 |
| `src/audio/capture.py` | 解決済みデバイス名(現状は `on_info` へのログ文字列としてのみ存在)を呼び出し元が値として取得できるようにする。完了UI(§8.3)の `Microphone: USB Audio Device` 表示に必要 | §8.3 (UI-2) |
| `tests/test_runtime_client_audio_bridge.py` | `AudioBridge` コンストラクタのシグネチャ変更(固定 `silence_rms_threshold` → `CalibrationEngine` 参照)に伴う既存テストの更新(実装フェーズで実施) | §6 Unit Test Plan |
| `tests/test_runtime_client_keyboard_bridge.py` | 手動再キャリブレーションキーに対応するテスト追加(実装フェーズで実施) | §6 Unit Test Plan |

### 1.2.1 レビュー: `ui/keyboard.py` を変更せずに実現できないか

このモジュールは `keyboard_bridge.py` のモジュールdocstringで「reused verbatim, unmodified」と明記されている共有コードである。変更を前提とする前に、以下3つの代替案を検討した。

**検討した代替案**

| # | 代替案 | 実現可能性 | 却下理由 |
|---|---|---|---|
| (a) | 既存キー(例: `s`)に再キャリブレーション機能を相乗りさせ、新規キーを増やさない | 技術的には可能 | 既存キーの意味を変更・多重化することになり、UX上の曖昧さを生む。設計書 FR-7 は「手動再キャリブレーション」という独立した操作を要求しており、既存機能への相乗りはその要求を歪める(設計の再解釈にあたるため不採用) |
| (b) | FR-7(手動トリガー)を実装せず、FR-6(自動ドリフト検知)のみに頼る | 実装は簡略化できる | 設計書 FR-7 は明確に手動トリガーを要件として定義しており、これを落とすことは Implementation Plan の裁量を超えた設計変更にあたる。本レビューは「設計変更は禁止」という制約下にあるため不採用 |
| (c) | `ui/keyboard.py` の `input()` ループとは別に、新しい入力チャネル(別スレッドでの生キー入力・シグナルハンドラ等)を Runtime Client 側だけで新設する | 技術的には可能 | 既存の全キー('r'/'g'/'G'/'s' 等)は同一の行ベース `input()` ループで処理されており、'c' キーだけを別チャネルにすると、①ユーザー体感として同じキーボード操作なのに挙動の一貫性がなくなる、②二重の入力読み取り機構(標準入力の奪い合い)による新規の不具合リスクを持ち込む。既存アーキテクチャとの整合(設計書 NFR-4 相当の精神)よりリスクが大きいと判断し不採用 |

**結論**

`ui/keyboard.py` の変更は不可避と判断する。理由は、この3案がいずれも「設計要件(FR-7)を満たさない」「設計を暗黙に変更する」「既存アーキテクチャより複雑でリスクが高い」のいずれかに該当し、"変更しない"という制約を守るコストが、それによって生まれる新たなリスクを上回るためである。

ただし、変更範囲は最小化できる。既存の `r` キーの実装を確認すると、`ui/keyboard.py` 側は `ctx.recording_active.set()/clear()` の呼び出しと `ctx.show_recording_status_fn()` の呼び出しのみを行い、実際のロジック(Control Event 送信、`NotifyingEvent` のコールバック等)はすべて `keyboard_bridge.py` 側に閉じている。'c' キーもこれと同型に実装することで、**`ui/keyboard.py` への変更を「新しい `elif cmd_lower == "c":` 分岐1行が、`keyboard_bridge.py` 側が用意した1個のコールバック(`ctx.recalibrate_fn()` 相当)を呼ぶだけ」という最小限に抑えられる**。再キャリブレーションの実処理・状態管理・UI表示判断は、すべて `src/runtime_client/` 側(`keyboard_bridge.py` / `calibration.py`)に責務を閉じたまま実装できる。

「Runtime Client 側だけで責務を閉じられないか」という問いへの回答は、**ロジックの責務は閉じられるが、キー入力のディスパッチ起点そのものは `ui/keyboard.py` の外に置けない**、というものである。

### 1.3 変更不要

| ファイル | 変更不要である理由 |
|---|---|
| `src/runtime_client/websocket_client.py` | WebSocket 転送層はブロックの中身を判断しない。Speech Gate の導出方法が変わっても、送信インターフェース(`_send_audio`)自体には影響しない |
| `src/runtime_client/output_device.py`, `src/runtime_client/tts.py` | 出力(TTS再生)側のモジュールであり、入力側のキャリブレーションとは独立している。設計書 §8 の UI モックもテキスト表示のみで、音声読み上げは要件に含まれていない |
| `src/audio/vad.py`, `src/audio/vad_buffering.py`, `src/phantom_runtime.py`, `src/runtime/*` | 仕様書 Scope・NFR-3 により Server 側は明示的に対象外 |
| `Dockerfile` | Server デプロイ設定のみであり、Runtime Client の変更はコンテナ定義に影響しない |
| `src/runtime_client/config.py` の `build_ws_url`, `parse_args` の URL/provider 関連ロジック | キャリブレーションと無関係な既存ロジックであり、本設計の影響範囲外 |

---

## 2. 実装順序

Phase 単位で、依存関係が下流(Phase 5)に向かって収束する形で整理する。設計思想(§ Runtime Philosophy)における「測定 → 導出 → 表示 → 適応 → 統合」という順序をそのまま実装順序に反映している。

```
Phase 1: Environment Observation
   (Noise Floor 測定ロジック — 単体で完結、既存コードへの依存最小)
        │
        ▼
Phase 2: Calibration Engine
   (Speech Gate 導出 + 状態機械 — Phase 1 の出力を消費)
        │
        ▼
Phase 3: Runtime UI
   (§8 の5画面描画 — Phase 2 が持つ状態/数値を表示するだけで、既存ホットパスには未接続)
        │
        ▼
Phase 4: Re-calibration
   (ドリフト検知 + 手動キー 'c' — Phase 2/3 の状態機械とUIが先に存在することが前提)
        │
        ▼
Phase 5: Integration
   (audio_bridge.py の固定閾値を置き換え、main.py の起動シーケンスに接続 —
    既存の Recording Gate / Silence Gate という「実際に音声を送信するかどうかを
    決めているホットパス」に触れる、最もリスクの高いフェーズ。
    Phase 1〜4 が単体で検証済みであることを前提に最後に実施する)
```

| Phase | 内容 | 主な対象ファイル | 前提Phase |
|---|---|---|---|
| Phase 1 | Environment Observation | `calibration.py`(`NoiseFloorSampler`) | なし |
| Phase 2 | Calibration Engine | `calibration.py`(`CalibrationEngine`, `CalibrationState`) | Phase 1 |
| Phase 3 | Runtime UI | `typed_event.py`, `audio/capture.py`(デバイス名公開) | Phase 2 |
| Phase 4 | Re-calibration | `calibration.py`(ドリフト監視)、`ui/keyboard.py`(§1.2.1 の最小分岐)、`keyboard_bridge.py` | Phase 2, Phase 3 |
| Phase 5 | Integration | `audio_bridge.py`、`main.py`、`config.py` | Phase 1〜4 すべて |

この順序を採用する理由は、既存の送信ホットパス(`AudioBridge._run_pump()`)への変更を可能な限り遅らせ、Phase 1〜4 を単体・結合テストで先に固めてから最後に一箇所だけ既存コードへ接続する、という変更の局所化にある(§7 実装リスク参照)。

---

## 3. クラス構成

### 3.1 新規クラス

| クラス | 責務 | 依存関係 |
|---|---|---|
| `NoiseFloorSampler` | 2.5秒間のブロック単位RMSサンプリング、汚染(発話混入)検出、90パーセンタイル算出(§6.2) | 既存の `block_rms()`(`audio_bridge.py`)を再利用 |
| `CalibrationEngine` | `NoiseFloorSampler` の結果から Speech Gate を導出(§6.3)、状態機械(§7.1)の保持・遷移、ドリフト監視(§6.4)の集計、Fallback 判定(§9) | `NoiseFloorSampler` に依存 |
| `CalibrationState` (Enum) | §7.1 の8状態(`INIT`/`CALIBRATING`/`CALIBRATED`/`CALIBRATION_FAILED`/`FALLBACK`/`RUNNING`/`DRIFT_DETECTED`/`RECALIBRATING`)を表現する列挙型 | なし |

UI 表示用の新規クラスは設けない。設計書 UI-5(既存トーンとの一貫性)を踏まえ、既存の `show_info`/`show_warn` 関数群(`typed_event.py`)を拡張する方針とする。

### 3.2 既存クラス(責務変更箇所)

| クラス | 現在の責務 | 変更後の責務差分 | 新規の依存関係 |
|---|---|---|---|
| `AudioBridge`(`audio_bridge.py`) | 固定 `silence_rms_threshold` と `block_rms()` の比較で送信可否を判定 | `_run_pump()` が `CalibrationEngine` から動的な Speech Gate を取得して判定するよう変更。判定結果を `CalibrationEngine` へフィードバック | `AudioBridge` → `CalibrationEngine` |
| `RuntimeContext`(`ui/keyboard.py`、`keyboard_bridge.py` で構築) | `recording_active` 等のコールバックを保持 | 手動再キャリブレーション要求コールバックを新規に保持(既存の `recording_active` と同型のパターン、§1.2.1) | `RuntimeContext` → `CalibrationEngine` への再キャリブレーション要求コールバック |
| `AudioCapture`(`audio/capture.py`) | 解決済みデバイス名を `on_info` へのログ文字列としてのみ通知 | 解決済みデバイス名を呼び出し元が値として取得できるように公開範囲を広げる(軽微な変更) | なし(公開範囲の変更のみ) |

### 3.3 将来分離可能な責務(今回は分離しない)

現在の `CalibrationEngine` は Observation の結果を受け取った後の「Threshold 導出」「State 管理」「Drift 監視」「Fallback 判定」の4つを1クラスに集約している。Hackathon 版の「最小限の自動キャリブレーション」というスコープ(設計書 §3.2 非目標)においては、この集約は妥当と判断する——分離してもクラス数が増えるだけで、今回の実装・テスト量に見合う恩恵がない(設計書 §10.2 のトレードオフ判断で機械学習ベースの動的しきい値推定を「オーバーエンジニアリング」として不採用としたのと同じ理由による)。

一方で、将来的にこれらの責務が肥大化した場合に備え、分離の余地を整理しておく。

```
CalibrationEngine (今回はこのまま単一クラス)
    │
    ├─ 将来分離候補: SpeechGateCalculator
    │     noise_floor から speech_gate を導出する処理(§6.3の計算式)のみを担う。
    │     入力と出力が数値のみで完結する純粋関数的な処理であり、
    │     状態を持たないため独立性が高い。
    │
    ├─ 将来分離候補: CalibrationStateMachine
    │     §7.1 の状態遷移(INIT〜FALLBACK)の保持・遷移判定のみを担う。
    │     「何が遷移条件を満たすか」の判定ロジックと、
    │     「遷移そのものの管理」を切り離せる。
    │
    ├─ 将来分離候補: DriftDetector
    │     直近10秒の pass/reject 移動窓を監視し、
    │     急変を検知したら通知するだけのコンポーネント。
    │     音声固有の概念(RMS等)を一切知らなくても成立するため、
    │     最も独立性が高く、他の「Runtime が環境を観測する」用途
    │     (Runtime Philosophy §11.1 で言及されている将来拡張)にも
    │     転用しやすい分離候補。
    │
    └─ 将来分離候補: FallbackPolicy
          キャリブレーション失敗時に「どの保守的推定値を採用するか」を
          決定するポリシーのみを担う。判定ロジックを差し替え可能にする
          余地を残す。
```

**分離を今回行わない理由:** 各分離候補はいずれも `CalibrationEngine` の内部状態(現在の Noise Floor 推定値、直近サンプル等)を参照するため、分離すると必然的にそれらの受け渡しインターフェースを新たに設計する必要が生じる。今回のスコープ(Hackathon 版・最小限の自動キャリブレーション)ではこのインターフェース設計コストに見合う利益がない。**分離が正当化される契機**は、設計書 §11(Future Extensions)にある「Runtime 全体への思想の拡張」——例えば `DriftDetector` を音声以外の観測(ネットワーク遅延の変化など)にも転用する具体的な必要性が生じた時点、あるいは `CalibrationEngine` 自体のテストケースが責務混在によって書きにくくなった時点である。

---

## 4. 状態遷移(実装レベル)

設計書 §7.1 の Mermaid 状態遷移図を変更せず、実装に必要な State / Event / Transition の対応表として再整理する。

| State | 契機となる Event | 遷移先 | 備考 |
|---|---|---|---|
| `INIT` | `SessionEstablished`(WebSocket接続成功) | `CALIBRATING` | §7.2 |
| `CALIBRATING` | `SampleWindowValid`(汚染なしでサンプル取得完了) | `CALIBRATED` | §6.2, §7.2 |
| `CALIBRATING` | `SampleWindowContaminated`(発話混入検出、リトライ余地あり) | `CALIBRATING`(自己遷移) | 最大3回、§6.2 |
| `CALIBRATING` | `RetryLimitReached`(3回失敗 or デバイスエラー) | `CALIBRATION_FAILED` | §9.4 タイムアウトも同一Event経路 |
| `CALIBRATION_FAILED` | `FallbackAdopted`(保守的推定値を明示採用) | `FALLBACK` | §9.1 |
| `CALIBRATED` | `NormalOperationStarted` | `RUNNING` | §7.2 |
| `RUNNING` | `DriftSuspected`(直近10秒棄却率の急変) | `DRIFT_DETECTED` | §6.4, FR-6 |
| `RUNNING` | `ManualRecalibrationRequested`('c'キー押下) | `RECALIBRATING` | §6.4, FR-7, §1.2.1 |
| `DRIFT_DETECTED` | `BackgroundRecalibrationStarted` | `RECALIBRATING` | 即座に遷移(§7.2) |
| `RECALIBRATING` | `RecalibrationSucceeded`(Gate更新) | `RUNNING` | §6.4 |
| `RECALIBRATING` | `RecalibrationFailed` | `CALIBRATION_FAILED` | §7.1 |
| `FALLBACK` | `ManualRecalibrationRequested` / `SilenceWindowDetected` | `RECALIBRATING` | §7.2 |
| `RUNNING` / `FALLBACK` | `SessionEnded` | `[*]` | §7.1 |

Recording ON/OFF(既存 P5-4-2 トグル)は本状態機械と独立した直交軸のままとする(設計書 §7.2 の記述を変更せず踏襲)。実装上は `CalibrationEngine` の状態と `recording_active` の状態を、それぞれ独立した Event ソースとして扱う。

---

## 5. UI実装順序

指定された順序(起動 → Calibration → Running → Environment Changed → Fallback → Manual Calibration)に沿って、対応する設計章・実装Phaseを整理する。

| 順序 | 画面 | 対応する設計章 | 実装Phase | 実装上の前提 |
|---|---|---|---|---|
| 1 | 起動時UI(サンプル取得中: 0/25 blocks) | §8.1 | Phase 3 | `CalibrationEngine` が `CALIBRATING` 状態に入った直後の初期表示。数値は0からのカウントアップのみで、ロジック依存は最小 |
| 2 | キャリブレーション中UI(進捗バー + 暫定Noise Floor) | §8.2 | Phase 3 | `NoiseFloorSampler` が保持する暫定サンプル数・暫定RMSを毎ブロックごとに参照する必要があるため、Phase 1 の内部状態を UI から読み取れる形にしておく |
| 3 | 完了UI(Noise Floor / Speech Gate / Microphone名) | §8.3 | Phase 3 | `CALIBRATED` 状態への遷移完了後に一度だけ表示。マイク名表示は `audio/capture.py` の変更(§1.2)が前提 |
| 4 | 再実施UI(Environment Changed) | §8.5 | Phase 4 | `DRIFT_DETECTED`/`RECALIBRATING` 状態が実装されていることが前提(Phase 2, Phase 4) |
| 5 | エラーUI(Calibration Incomplete) | §8.4 | Phase 2, Phase 3 | `CALIBRATION_FAILED`/`FALLBACK` 状態遷移(Phase 2)と表示(Phase 3)の両方が前提 |
| 6 | 手動キャリブレーション(`c` キー) | §6.4, FR-7 | Phase 4 | `ui/keyboard.py` の最小分岐追加(§1.2.1)が前提 |

※ ユーザー指定の順序と、§2 の実装Phase順序は完全に一致しない(UI要件としての提示順序と、依存関係に基づく実装順序は別軸であるため)。上表はその対応関係を明示するためのものであり、両者に矛盾はない。

---

## 6. Unit Test Plan

Acceptance Criteria(AC-1〜AC-10、設計書末尾)それぞれについて、テスト種別を整理する。実マイクハードウェア・実Cloud Runへの依存有無で Unit Test / Integration Test / Production E2E を分類する(既存の `docs/MIGRATION_MATRIX.md` が採用している分類方針——ハードウェア依存はハードウェアテストとして明示的に別枠にする——を踏襲)。

| AC | 内容 | Unit Test | Integration Test | Production E2E |
|---|---|---|---|---|
| AC-1 | USBマイクで正常動作 | — | — | ✅ 必須(実ハードウェア依存) |
| AC-2 | 内蔵マイクで正常動作 | — | — | ✅ 必須(実ハードウェア依存) |
| AC-3 | Bluetoothマイクで正常動作(利用可能な場合) | — | — | ✅ 該当機材がある場合のみ |
| AC-4 | 静かな環境で正常動作(過敏反応しない) | ✅ 合成RMS(低振幅)データで `NoiseFloorSampler`/`CalibrationEngine` を直接検証 | ✅ `AudioBridge._run_pump()` に合成データを直接投入(既存 `test_runtime_client_audio_bridge.py` の手法を踏襲) | ✅ 静音環境での確認 |
| AC-5 | 環境ノイズ変化時の再キャリブレーション | ✅ ドリフト監視ロジックを合成pass/rejectシーケンスで検証 | ✅ `CalibrationEngine` の状態遷移(`RUNNING`→`DRIFT_DETECTED`→`RECALIBRATING`→`RUNNING`)を直接駆動して検証 | ✅ マイク切替を伴う実演環境 |
| AC-6 | Recording Gate と競合しない | ✅ 両ゲートを同一 `_run_pump()` 呼び出し内でモック値により独立検証(既存 `TestRecordingGate`/`TestSilenceGate` と同型) | ✅ `recording_active` と `CalibrationEngine` の状態を同時に変化させた組み合わせテスト | ✅ 既存の Production 相当 E2E 手順(本調査で確立済み)を再実施 |
| AC-7 | Production Cloud Run E2E PASS | — | — | ✅ 必須(実マイク・実WebSocket・実Cloud Run) |
| AC-8 | UI が仕様どおり表示される | ✅ 各表示関数の出力文字列をスナップショット的に検証 | ✅ 状態遷移に応じて正しい画面が呼び出されることを検証 | ✅ 目視確認 |
| AC-9 | 手動再キャリブレーションがいつでも実行できる | ✅ `RuntimeContext` 相当のモックで 'c' キー入力をシミュレートし `CalibrationEngine` への到達を検証(既存 `test_runtime_client_keyboard_bridge.py` の `_run_keyboard_and_collect` 方式を踏襲) | ✅ `RUNNING`/`FALLBACK` それぞれの状態から実行できることを検証 | ✅ 実キーボード操作での確認 |
| AC-10 | キャリブレーション失敗時、推定値であることが明示される | ✅ `CALIBRATION_FAILED`→`FALLBACK` 遷移時の表示関数呼び出しを検証 | — | ✅ 意図的に発話を継続させて汚染を発生させた実演確認 |

---

## 7. 実装リスク

### 7.1 想定されるリスク

| リスク | 内容 | 影響度 |
|---|---|---|
| 共有コード(`ui/keyboard.py`)への変更 | §1.2.1 のレビューにより変更は不可避と判断したが、変更範囲は新規分岐1行に限定できる。それでも「reused verbatim, unmodified」という既存方針からの逸脱であることに変わりはなく、他機能への意図しない影響がないか個別確認が必要 | 中 |
| 既存ホットパス(`_run_pump()`)への変更 | 本調査(P5-4フォレンジック)で実測・検証済みの Recording Gate(P5-4-2)/Silence Gate(P5-4-1)のロジックに直接手を入れる。両ゲートの独立性(AC-6)を壊さないことが必須 | 高 |
| 起動時ブロッキング処理の追加 | `_amain()` に 2.5秒(最大 7.5秒、リトライ込み)のブロッキング待機が新規に入る。NFR-1(3秒以内)を超過するリトライケースの扱いを明確にする必要がある | 中 |
| `AudioBridge` コンストラクタのシグネチャ変更 | 固定 `silence_rms_threshold: int` パラメータの扱いが変わるため、既存の呼び出し元(`main.py`)と既存テスト(`tests/test_runtime_client_audio_bridge.py`)の両方に影響する | 中 |

### 7.2 既存コードへの影響

- `AudioBridge.__init__` のシグネチャ変更は、既存テスト(`tests/test_runtime_client_audio_bridge.py` の `_make_bridge()` ヘルパー含む)全体の更新を要する。設計書 NFR-4(既存の Recording Gate / Silence Gate の構造・責務分離を壊さない)を満たすことをこの更新で担保する。
- `keyboard_bridge.py` の `build_keyboard_thread()` は現在 `(thread, recording_active)` のタプルを返す(P5-4-2 で確立済みの契約)。手動再キャリブレーションのコールバックをこの戻り値契約にどう追加するか(タプル要素追加 or 別関数分離)は実装フェーズでの詳細設計事項とする。

### 7.3 Backward Compatibility

- `config.py` の `DEFAULT_SILENCE_RMS_THRESHOLD` は、Fallback 値(§9.1)としての役割に転用されるため、定数自体は残置する。CLI 引数の追加・削除は設計書に明記がないため、本計画では対象外とする。
- 既存の Recording Gate(P5-4-2)の Control Event(`toggle_recording`)・Server 側インターフェースには一切変更が及ばないことを Phase 5 の受け入れ基準とする(NFR-3 と整合)。

### 7.4 Performance

- ドリフト監視(§6.4)は、既存の Speech Gate 判定(pass/reject)結果を移動窓に加算するだけであり、追加の統計計算は軽量(NFR-5 に整合)。
- 初期キャリブレーション(2.5秒)は、既存の音声送信を一時的に止める(通常運転前のフェーズであるため送信対象自体がまだ存在しない)ことによる副作用がないか、Phase 5 で `main.py` の実際のシーケンスを確認する必要がある。

---

## 8. Completion Criteria

既存の `docs/MIGRATION_MATRIX.md` が採用している3段階のステータス表現(Completed / Unit Tested / Production Verified)に整合させる。

| 段階 | 条件 |
|---|---|
| **Implementation Completed** | Phase 1〜5 のすべての新規・変更ファイル(§1.1, §1.2)の実装が完了し、`python3 -m py_compile` が全対象ファイルでクリーンであること |
| **Unit Tested** | §6 Unit Test Plan の Unit Test / Integration Test 列に該当する AC(AC-4, AC-5, AC-6, AC-8, AC-9, AC-10)がすべて自動テストで検証され、既存の Unit Test スイート(現状 362 件 PASS)に対してリグレッションがないこと |
| **Production Verified** | §6 Unit Test Plan の Production E2E 列に該当する AC(AC-1, AC-2, AC-3, AC-4, AC-5, AC-6, AC-7, AC-8, AC-9, AC-10)が、実マイク・実WebSocket・実Cloud Run環境で確認され、本調査で確立した Production 相当 E2E 手順(実 `kb_thread`・音声通知付きシナリオ)により再現性をもって示されること |

---

## 要約

- 対象ファイル数: **13**
- 新規ファイル数: **2**(`src/runtime_client/calibration.py`, `tests/test_runtime_client_calibration.py`)
- 変更ファイル数: **9**(`audio_bridge.py`, `keyboard_bridge.py`, `ui/keyboard.py`, `main.py`, `config.py`, `typed_event.py`, `audio/capture.py`, `tests/test_runtime_client_audio_bridge.py`, `tests/test_runtime_client_keyboard_bridge.py`)
- 実装Phase数: **5**(Environment Observation → Calibration Engine → Runtime UI → Re-calibration → Integration)
