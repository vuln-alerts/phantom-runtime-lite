# Adaptive Runtime Calibration Design Review

> **Status:** Design-only review. No source code has been written or modified as part of this document(制約: コード変更禁止/ロードマップ変更禁止/新機能提案禁止 を遵守)。
> **Scope:** `src/runtime_client/calibration.py`, `src/runtime_client/audio_bridge.py`, `src/runtime_client/main.py` の Speech Gate / Noise Floor 導出ロジックのみ。Server (`phantom_runtime.py`, Cloud Run) は変更対象外。
> **Origin:** Hackathon提出前の Production Verification で観測された実機データ(§0)を根拠に、`P5_4_ADAPTIVE_RUNTIME_CALIBRATION.md`(以下「設計書」)の §6.2/6.3/6.4 を再検証する。
> **Preserved:** Dynamic Calibration / Adaptive Threshold / Production Runtimeという設計書の思想は本レビューでも維持する。撤廃・置き換えの対象は「観測に基づかない固定値」ではなく、「観測に基づいて導出された式そのものの係数」である。

---

## 0. Production Verification 実測データ(本レビューの出発点)

| 項目 | MacBook Pro 内蔵マイク | 外部マイク(USB) |
|---|---|---|
| Noise Floor (p90) | ≒180 | ≒985 |
| Speech Gate (`clamp(floor×3.0, 150, 2500)`) | ≒540 | ≒2500 (**上限クランプに張り付き**) |
| 通常会話 RMS 実測レンジ | 120〜325 | 400〜1700 |
| 会話 RMS / Noise Floor 比 | 最大 1.8x (325/180) | 最大 1.73x (1700/985) |
| Speech START | 発生せず(325 < 540 で常に下回る) | 一瞬のみ(blocks sent = 1) |
| Transcript | なし | なし |

この2台は「マイクの種類が違う」以上に共通点を持つ: **どちらも、実際の会話音量とNoise Floorの比は最大でも1.7〜1.8倍であり、設計書 §6.3 が発話の意思とみなす閾値である「3.0倍」に一度も到達していない。** これは偶然の2サンプルではなく、口語の通常会話音量(マイクに向かって叫ぶのではなく、Hackathonデモで話す程度の声量)におけるRMSダイナミックレンジの構造的な性質であり、以降の分析全体の起点となる事実である。

---

## ① 現状設計の問題点

### 1.1 Noise Floor

`NoiseFloorSampler.noise_floor()`(`calibration.py:262-269`)は観測ウィンドウの p90 を返す。問題は値そのものの算出方法ではなく、**この値が「発話に対して確保できる残りのダイナミックレンジ」をどれだけ消費するか**にある。

- 内蔵マイクでは Noise Floor (180) が既に会話RMSレンジ (120-325) の下限を上回っている。つまりこのマイクは「静寂」と「小声の会話」をほぼ同じRMS帯で表現しており、Noise Floorという単一のスカラー値で「静寂の上限」を代表させること自体が、そもそも会話の下限側と重なってしまっている。
- 外部マイクでは Noise Floor (985) が会話RMSレンジの下限 (400) を大きく超えている。この場合 Noise Floor は「暗騒音」ではなく「マイク自体のセルフノイズ/ライン入力ゲイン」を測定してしまっている可能性が高い。

いずれのケースも、Noise Floor 自体の測定(p90という統計量選択)は妥当だが、**その後段(§6.3の乗算式)が「Noise FloorとSpeech RMSの間に3倍の分離幅がある」という前提に依存しており、この前提が実機で成立しないマイクが2/2で観測された。**

### 1.2 Speech Gate

`CalibrationEngine._derive_speech_gate()`(`calibration.py:429-430`)は `clamp(noise_floor × 3.0, 150, 2500)` のみで導出される単一の相対ルール。設計書 §10.3 はこれを「発話の意思とみなす」ための保守的な安全マージンとして採用しているが、Production Verification の実測は次を示す。

- 内蔵マイク: Gate 540 に対し会話最大値 325 → **常時 Gate 未達**。Speech START が一度も発生しない。
- 外部マイク: Gate が 2500(クランプ上限)に張り付き、会話最大値 1700 → **理論上も到達不能な閾値**。この状態は設計書 §9.3 が「測定できたが環境が非常に騒がしい」として容認している状態と表面上は同じ形(clampの上限張り付き)だが、実態は「騒がしい」のではなく「乗算係数が高すぎて通常会話では届かない」ことによる誤検知であり、§9.3のフォールバック的な扱い(そのまま採用)は本ケースには適合しない。

### 1.3 Calibration方式(2層構造: baseline → dynamic contamination threshold → 本観測)

直近の修正(`cfb6d71 fix(runtime): resolve Production Startup Calibration blocker with adaptive threshold`, `main.py:309-398`)により、Startup Calibrationの汚染検出閾値(`contamination_threshold`)自体も動的化された。具体的には:

1. `_run_baseline_observation()` が汚染判定なし(閾値 = +inf)で10ブロックをサンプリングし、暫定 Noise Floor を得る。
2. `_derive_dynamic_contamination_threshold()`(`main.py:354-398`)が **その暫定Noise FloorにCalibrationEngineの同じ式 `clamp(floor×3.0, 150, 2500)` を再適用**し、これを本観測の contamination threshold として使う。
3. 本観測(`EnvironmentObserver`, 25ブロック)がこの動的閾値で汚染検出しながら Noise Floor を確定する。

この設計は「Startup CalibrationがProductionで常に失敗する」という別の不具合(固定150という低すぎる contamination threshold が、セルフノイズの高い環境音そのものを"発話混入"と誤検出していた問題)を解消した点では有効だった。しかし、**Speech Gate導出式をcontamination threshold導出にも使い回したことで、両者が同じ値に収束する自己参照的なループ**が生まれている。

外部マイクのケースで具体的に辿ると:
- baseline noise floor ≒985 → dynamic contamination threshold = clamp(985×3, 150, 2500) = 2500
- 本観測は「RMS < 2500 は汚染ではない」とみなして25ブロックをサンプル → 結果として測定される Noise Floor も ≒985(セルフノイズ込みでそのまま通る)
- Speech Gate = clamp(985×3, 150, 2500) = 2500 → 到達不能

**contamination thresholdが実質2500まで緩んでいるということは、「静寂を測るはずの観測ウィンドウ中に本当は小声の発話が混ざっていても、それを"汚染"として検出する能力そのものが失われている」ことを意味する。** これは設計書 §6.2 の汚染検出("人が観測中に話し始めてしまうケースを吸収する")の意図を、セルフノイズの高いマイクにおいて実質的に無効化してしまっている。

### 1.4 Percentile

p90という統計量選択自体(設計書 §10.2 の理由: 平均は暗騒音を過小評価、最大値は単発ノイズに弱い)は妥当性を保っている。ただし1.3で述べた通り、**現行フローはp90の測定を2回連続で行う**(10ブロックのbaseline → 25ブロックの本観測)。どちらも同じ「高い方に寄る」統計量であるため、後段の乗算係数の問題を悪化させる方向にのみ作用する。Percentile自体の選択ミスではなく、**測定パス数が増えたことで方式全体の感度が変わった**点が、当初の単層設計からの意図しない副作用である。

### 1.5 Contamination

閾値の動的化は妥当な方向性だが、1.3で述べた通り「Speech Gate導出式の使い回し」が、Noise Floorが高いマイクほど汚染検出が緩む、という**環境が悪いほど安全装置が弱くなる逆相関**を生んでいる。これは設計書のどの節にも意図として書かれていない(§6.4は「本観測に対して」再キャリブレーション用の固定閾値を維持する設計であり、Startup Calibrationのcontamination thresholdまでSpeech Gate式で動的化するのはこの修正で新たに追加された挙動)。

### 1.6 Clamp

`min=150 / max=2500`(設計書§6.3, `calibration.py:178-184`)は「無響室」「異常に騒がしい環境」という**稀な外れ値に対する安全弁**として設計されている(設計書§10.3: 「環境ごとの正解の音量を決め打ちするものではない」)。しかし外部マイクの実測は、この上限が「稀な外れ値」ではなく**通常のUSBマイク+通常の会話音量という一般的な条件下で恒常的に張り付く値**であることを示した。安全弁が常用境界値になっている時点で、クランプ境界自体が実環境の分布を反映していないと判断すべきである。

### 1.7 RMS評価

`AudioBridge._run_pump()`(`audio_bridge.py:210-227`)は100msブロック単位の瞬時RMSを、平滑化・ヒステリシス・最小継続時間なしで単純比較している(`rms < gate` で即座に continue)。

これが1.3までの問題と独立に効いてくるのが「外部マイクで blocks sent = 1」という実測である。仮にSpeech Gateが正しい値に補正されたとしても、会話音声は無声子音・単語間ポーズにより100ms粒度で激しく上下する。**単発ブロックがGateを一瞬超えても、次のブロックで即座に下回れば "Speech START" 相当の1ブロックだけが送信されて終わる。** さらに Server 側 VAD(`phantom_runtime.py`, `args.max_sec=8.0`/`args.silence_sec` 由来の `SILENCE_BLOCKS`)は「8.0秒連続音声」または「約200ms以上の無音区間」のいずれかを検出して初めて Whisper を呼び出す設計(design doc §1.2 bullet 3 で確認済み)。**1ブロック(100ms)だけの送信は、Server側のどちらの確定条件も満たせない。** つまりGateの値をいくら正しく調整しても、単発ブロック判定のままでは「Gateを超える瞬間はあるがTranscriptは生成されない」という今回とほぼ同型の症状が再発しうる。

ただし本レビューの推奨(§④)は、まずGateの値(①1.2/1.6)そのものが実測会話音量に対して構造的に高すぎるという一次要因を解消することを優先する。この評価方式(1.7)の問題は、値の再較正後になお残るかどうかを実測で確認してから対処すべき次点の論点として扱う。

---

## ② 改善案

いずれも「Runtimeが実行環境を観測し、そこから相対的にパラメータを導出する」という設計書の思想(Runtime Philosophy, §Runtime Philosophy 冒頭)を維持したまま、①で特定した具体的な数値的事実に対応する。

### 案1: Adaptive倍率変更(乗算係数の再較正)

実測(会話RMS/Noise Floor比が2機種とも1.7〜1.8倍が上限)に基づき、`DEFAULT_SPEECH_GATE_MULTIPLIER` を 3.0 から実測レンジの安全側(例: 1.5〜1.8程度、要追加検証)に引き下げる。Contamination threshold導出(`main.py:_derive_dynamic_contamination_threshold`)にはこの新係数を使わず、汚染検出専用の別係数(または固定値)に分離し、①1.3で述べた自己参照ループを断つ。

### 案2: 動的Speech Margin(乗算+加算ハイブリッド)

`speech_gate = clamp(noise_floor + max(margin_min, noise_floor × (k-1)), gate_min, gate_max)` のように、**Noise Floorが低いマイクには加算マージンが下駄として効き、Noise Floorが高いマイクには乗算成分がスケールする**ハイブリッド式に変更する。設計書§10.3は加算方式を「相対的な余裕度の説明力が落ちる」として不採用にしたが、それは実測データがない段階の判断であり、今回の実測(低floor機で加算的な余裕が、高floor機で相対的な余裕が、それぞれ支配的に効くべきことが判明)を踏まえると再検討の余地がある。

### 案3: ヒステリシス + 最小継続ブロック数(Debounce)

Gateの**値**ではなく**評価方式**を変更する案。単一ブロックの `rms >= gate` 判定を、(a) 立ち上がり/立ち下がりで異なる閾値を使う二段階ヒステリシス、(b) N連続ブロック(例: 2〜3ブロック=200〜300ms)がGateを超えて初めて "Speech START" とみなす最小継続時間確認、の少なくとも一方に置き換える。①1.7で述べた「blocks sent=1」問題は、Gateの値の問題ではなく評価方式そのものの脆弱性であるため、案1とは独立した論点である。

### 案4: Percentile変更(baseline/本観測の役割分離)

`_run_baseline_observation`(10ブロック, p90)と本観測(25ブロック, p90)の統計量を分離する。baselineはcontamination threshold導出専用であることを踏まえ、より保守的な低めの統計量(例: p75、または中央値)に変更し、①1.4で述べた「p90を2回連続適用して感度が悪化する」副作用を緩和する。

### 案5: Conversation中のAdaptive Update(棄却率ベースの自動再キャリブレーション)

設計書 FR-6(§6.4 自動トリガー、§10.6)は「直近10秒の棄却率が急変した場合」の再キャリブレーションを要件化しているが、具体的な閾値・アルゴリズムは意図的に未定義(`calibration.py`モジュールdocstring §37-48 で明記: "no threshold not already in the design doc")。本症状(棄却率がほぼ100%に貼り付いたまま推移しない)はFR-6の対象そのものだが、その自動化には未定義の閾値設計が新たに必要になる。

---

## ③ 比較

| 案 | メリット | デメリット | Production安定性 | 誤認識リスク | Hackathon提出適合性 |
|---|---|---|---|---|---|
| **案1: 倍率変更** | 実測データに直接対応する最小差分。式の形(相対倍率+clamp)を変えないため設計書の思想・既存テストの構造をほぼ維持できる | 係数の「正しい値」は2サンプルのみからの推定であり、他マイクでの再検証が必要。低すぎるとNoise Floorの揺らぎ自体がGateを超え、誤って"発話あり"と判定するリスクが上がる | 高(式構造は不変、定数のみ変更) | 中(倍率を下げるほど上昇するが、①の実測に基づけば許容範囲) | 高(変更範囲が最小、既存の単体テストへの影響が定数値の変更に限定される) |
| **案2: 動的Speech Margin** | 低floor機・高floor機の両方に理論的に説明のつく式になる | 式の構造自体を変更するため、設計書§10.3が一度不採用にした代替案の再導入であり、既存の説明("倍率の意味")との整合を取り直す必要がある。パラメータが2つ(乗算係数+加算下駄)に増え、調整対象が増える | 中(式変更のため既存テストの前提を作り直す必要がある) | 中〜低(下駄の設計次第で低floor環境の過敏化を抑えられる) | 中(式構造の変更は案1よりレビューコストが高く、提出前の検証時間が限られる中ではリスク) |
| **案3: ヒステリシス+Debounce** | Gateの値に依存せず「blocks sent=1」型の症状に直接対応。Server側VADの確定条件(継続音声/継続無音)と評価粒度を整合させられる | Gate値自体(①1.2/1.6の問題)は解消しないため、単独では「内蔵マイクでGateに一度も到達しない」ケースを救えない。評価方式(状態機械)への変更を伴う | 高(既存の単一比較をN回連続比較に変えるだけで、状態機械やAPIの追加は不要) | 低(誤ってノイズ単発ブロックを発話と誤認するケースを逆に減らす方向に働く) | 中(実装自体は小さいが、案1で問題が解消するかを実機再検証してから要否を判断すべき次点の変更) |
| **案4: Percentile変更** | 実装コストが最小(定数1つの変更) | 単独では乗算係数3.0という根本要因を変えないため、①の実測が示す「会話RMSが常にGate未達」という主症状は解消しない | 高 | 低(むしろ保守的な方向) | 中(単独では効果が限定的なため、他案との併用前提) |
| **案5: 棄却率ベース自動再キャリブレーション(FR-6実装)** | 設計書が元々想定していた自己修復機構であり、思想的に最も"Adaptive"を体現する | 具体的な閾値・アルゴリズムが設計書に一切定義されておらず(calibration.py docstringが明記する通り意図的に未実装)、提出直前に新規アルゴリズムを設計・検証する時間的リスクが最も高い。「新機能提案禁止」の制約にも抵触しやすい(未実装のFR-6を新規実装することは事実上の新機能追加) | 低(未検証のアルゴリズムをHackathon直前に投入するリスク) | 不明(閾値設計次第で大きく変動、検証時間が確保できない) | 低(制約「新機能提案禁止」「Hackathon成功率最優先」と正面から衝突する) |

---

## ④ 推奨案

**案1(Adaptive倍率変更)を単独で推奨する。**

理由:

1. Production Verificationの実測データが示す一次的な事実は「乗算係数3.0が実際の会話RMS/Noise Floor比(最大1.7〜1.8倍)を超えている」ことである。この事実に直接対応するのは案1であり、式の構造(相対倍率+clamp)を変えないため設計書の思想・既存の`CalibrationEngine`のAPI・既存テストへの影響が最小である。
2. 案3(ヒステリシス+最小継続ブロック数)はGateの"値"ではなく"評価方式(状態機械)"そのものを変更する提案であり、①1.7で述べた「blocks sent=1」という症状が案1適用後もなお残るかどうかは、係数再較正後に実機で再検証しなければ判断できない。**評価方式の変更は状態管理の複雑化とテスト範囲の拡大を伴うため、案1単独の効果を実測で確認した上で、なお症状が解消しない場合にのみ導入を検討する次点の候補**と位置づける。Hackathon提出前は、まず最小スコープ・最小リスクの変更(案1)で主要因を解消し、それでも残る症状があれば案3を追加する、という段階的な適用が適切である。
3. 案2・案4は式構造の変更を伴い案1より検証コストが高いため見送る。案5は「新機能提案禁止」という制約と衝突するため除外する。

不採用/保留理由の要約:
- 案2: 式構造変更によるレビューコスト増
- 案3: Gate値の問題(①1.2/1.6)ではなく評価方式の問題(①1.7)に対する修正であり、案1単独の効果を実機再検証してから導入要否を判断すべきフォールバック
- 案4: 単独では主症状を解消しない補助的措置
- 案5: 制約(新機能提案禁止)と時間的リスクの両面で不適

---

## ⑤ 実装影響(推奨案: 案1)

> 以下は案1に着手する場合の影響整理であり、本レビュー自体はコード変更を行わない。案3は、案1適用後の実機再検証で「blocks sent=1」等の症状が解消しない場合に検討する次点の追加施策であり、本レビューの推奨スコープには含めない(参考として影響範囲のみ末尾に併記する)。

### 変更対象ファイル(案1)

| ファイル | 変更内容(想定) |
|---|---|
| `src/runtime_client/calibration.py` | `DEFAULT_SPEECH_GATE_MULTIPLIER`(現行3.0)の再較正。`_derive_dynamic_contamination_threshold`相当のロジックが同じ式を再利用している構造(①1.3)を踏まえ、Speech Gate用係数とContamination Threshold用係数を分離できるよう定数を追加(既存の`DEFAULT_NOISE_FLOOR_SAFETY_FLOOR`とは別軸) |
| `src/runtime_client/main.py` | `_derive_dynamic_contamination_threshold()`が呼び出す係数を、Speech Gate導出用の係数と分離したものに差し替え |
| `tests/test_runtime_client_calibration.py`, `tests/test_runtime_client_main.py` | 定数値に依存する既存アサーションの更新 |

### 影響範囲

- `CalibrationEngine`/`EnvironmentObserver`/`RecalibrationController`の公開APIシグネチャは変更不要(定数のデフォルト値変更のみで完結する設計)。
- `AudioBridge`側の変更は不要(案1は`calibration.py`/`main.py`の定数・導出ロジックに閉じる)。

### Backward Compatibility

- `calibration_controller=None`のケース(pre-Phase-5の固定`silence_rms_threshold`経路)は無変更のため影響なし。
- 係数変更は`CalibrationResult`のフィールド構造を変えないため、これを消費する`main.py`側の表示ロジック(`show_calibration_complete`等)は無変更で動作する。

### Production Runtimeへの影響

- Server側(`phantom_runtime.py`, Cloud Run)は無変更(設計書§3.2の非目標を維持)。
- 係数変更は「観測から導出する」という構造を保持したままの再較正であり、Dynamic Calibration / Adaptive Threshold という設計思想の後退にはあたらない。

### (参考)案3を追加適用する場合の影響範囲

案1適用後の実機再検証で症状が残る場合にのみ検討する。その場合は`src/runtime_client/audio_bridge.py`の`_run_pump()`にGate判定の状態追跡(継続ブロック数カウンタ等)を追加することになり、`tests/test_runtime_client_audio_bridge.py`のアサーション更新が追加で必要になる。`calibration_controller`とのインターフェース(`active_result.speech_gate`を読むだけ)自体は不変。

---

## Appendix: 未解決として残る論点

- 案1の具体的な係数値(1.5〜1.8のいずれか)は、本レビュー時点で入手している2機種の実測データのみから決定するには samples が少ない。Hackathon会場での追加実機検証(可能であれば審査当日に近い環境・マイクでの再測定)を推奨する。
- 案1適用後、なお「blocks sent=1」等の単発クロス症状が残る場合に備え、案3(ヒステリシス+最小継続ブロック数)を次点候補として温存する。導入する場合はServer側VADの`args.silence_sec`由来の`SILENCE_BLOCKS`と整合させる値にすることが望ましいが、Server側の具体的な現在値は本レビューのスコープ外(`phantom_runtime.py`の起動引数依存)として別途確認が必要。
