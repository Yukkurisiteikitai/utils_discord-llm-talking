# 引き継ぎ資料 (2026-08-04時点)

Discord「電話」Botプロジェクトのここまでの経緯・決定事項・技術的な発見をまとめた資料。
セットアップ手順や使い方は `README.md` を参照。ここでは**なぜ今の形になっているか**を中心に記録する。

## 目的

Discord Botからランダム/スケジュールでDMに「着信」が来て、応答するとボイスチャンネルで
リアルタイム音声会話が始まる。STT/LLM/TTSは全てMac上でローカル完結させ、体感1秒程度の
応答速度を目指す(OpenAIのGPT-Live記事のパイプライン化によるレイテンシ削減思想を参考にした)。
対象環境はMacBook Air M3・RAM 8GB。

## 現在の状態: 実際の通話で動作確認済み

STT→LLM→TTSの全パイプラインを実際のDiscordボイスチャンネルで動かし、双方向の会話が成立することを
確認済み(例: 「ドクターソーンっていいアニメを知ってるかい?」→「知っています」)。単なる理論上の
実装ではなく、実機での往復動作を確認している。

## 技術構成(現時点)

| 役割 | 使用技術 | 備考 |
|---|---|---|
| Discord接続 | discord.py 2.7.1 | py-cordではなくこちらを採用(理由は後述) |
| ボイス受信 | discord-ext-voice-recv (git main) | 非公式・実験的。重大なパッチを複数適用済み(後述) |
| VAD | Silero VAD (ONNX) | 発話区間検出 |
| STT | MLX Whisper (`whisper-small-mlx`) | 実測 約0.6秒/発話 |
| LLM | mlx-lm (`Qwen2.5-1.5B-Instruct-4bit`) | 4bit量子化済み。実測 約0.5〜1.0秒/最初の一文 |
| TTS | VOICEVOX ENGINE(ずんだもん, speaker id 3) | 実測 約0.6〜0.75秒/文。AivisSpeechより2〜3倍速い |

体感レイテンシ見積もり: VAD無音待ち(0.3s) + STT(0.6s) + LLM初手(0.5〜1.0s) + TTS(0.6〜0.75s)
≒ **1.5〜2秒前後**。当初目標の1秒には届いていないが、初期実装(AivisSpeech構成、2.5〜3秒)からは
大幅改善している。

## 意思決定の経緯

1. **py-cord ではなく discord.py + discord-ext-voice-recv を採用**: py-cordの`Sink`はファイル録音向けの
   設計で、リアルタイムのパケット単位ストリーム処理には不向きと判明(GitHub Issue調査より)。
   discord-ext-voice-recvは`write(user, data)`がパケット到着ごとに同期的に呼ばれる設計で要件に合致した。
2. **TTSエンジン: VOICEVOX → AivisSpeech → VOICEVOX と2回切り替えた**:
   - 最初はVOICEVOXを想定していたが、このMacに未インストールだった
   - 次にAivisSpeech(声の自然さ重視)に切り替え、実際にインストールして動作確認
   - しかし実機計測でAivisSpeechはCPUのみ(GPU/CoreMLアクセラレーション不可)で1文あたり1.2〜2.2秒と判明
   - LLMの暴走生成バグと合わせて応答が15〜26秒に悪化する事態が発生し、速度優先の方針でVOICEVOXに戻した
   - `pipeline/tts.py`はVOICEVOX/AivisSpeechどちらのAPIとも互換な設計にしてあるため、`config.py`の
     数行(`TTS_ENGINE_BASE_URL`/`TTS_SPEAKER_ID`/`TTS_ENGINE_BINARY_PATH`/`TTS_ENGINE_APP_NAME`)を
     差し替えるだけでどちらにも戻せる

## 実機デバッグで発見した重大な問題と対処(`bot.py`に集約)

開発中に「着信は繋がるが一切発話が認識されない」という深刻な症状が発生し、原因調査の結果、
**単一の原因ではなく4つの問題が積み重なっていた**ことが判明した。

### 1. `discord.opus.OpusNotLoaded`
macOS(Apple Silicon Homebrew)では`ctypes.util.find_library('opus')`が
`/opt/homebrew`配下のlibopusを見つけられず、Botの発話(`voice_client.play()`)自体が例外で落ちていた。
→ `bot.py`の`_ensure_opus_loaded()`でHomebrewの既知パスに直接フォールバック。

### 2. discord-ext-voice-recvの受信ループが1パケットのエラーで完全停止するバグ
`PacketRouter.run()`が`_do_run()`全体を1つのtry/exceptで囲んでおり、Opusデコード中に例外が
1回でも起きると`voice_client.stop_listening()`が呼ばれてスレッドごと終了する実装だった。
つまりネットワーク由来のパケット破損が1回起きただけで、その通話は以後ずっと音声を受信できなく
なっていた(「最初の応答以降スタックする」の直接原因)。
→ `bot.py`の`_patch_voice_recv_resilience()`でパケット単位のtry/exceptに置き換え。

### 3. 【最重要】DiscordのE2EE音声プロトコル(DAVE)にdiscord-ext-voice-recvが非対応
Discordは2026年3月にボイスチャンネルのE2EE(DAVEプロトコル、MLSベースのフレーム暗号化)を
必須化した。discord.py本体は`davey`ライブラリで対応済みだが、`discord-ext-voice-recv`は
DAVEの存在を一切知らないコードのまま(ソースにdave/mlsへの参照が皆無なことを確認済み)。
そのため受信パケットは通常の伝送路暗号化だけ解かれ、DAVEのフレーム単位暗号化はそのまま残った状態で
Opusデコーダに渡され、**100%の確率で`corrupted stream`エラー**になっていた。
→ `bot.py`の`_patch_voice_recv_dave_decrypt()`で、discord.pyが保持している
`VoiceClient._connection.dave_session`(`davey.DaveSession`)の`decrypt()`メソッドを、
Opusデコード直前に割り込ませて復号するようパッチ。**これが無いと通話の音声受信機能自体が
成立しない**、最も重要な修正。

このパッチは内部APIへの深い依存であり、`discord.py`または`discord-ext-voice-recv`のバージョンが
上がると壊れる可能性が高い。将来的にどちらかのライブラリが正式にDAVEへ対応すれば不要になるはず。

### 4. エラーログ自体がCPUを圧迫し応答が数十秒に悪化
上記1〜3のエラーハンドリングで`log.exception()`(フルトレースバック出力)や、RTCPパケットの
INFOログが高頻度で出続け、それ自体がCPUを消費して応答レイテンシを直接悪化させていた
(実測で15〜26秒まで悪化する例を確認)。
→ `_quiet_voice_recv_logging()`でRTCP関連ログをWARNING以上に抑制、各種エラーログも
カウンタ表示のみの軽量なものに変更。

## LLMの暴走生成(繰り返しループ)対策

小型モデル(1.5B)が同じ単語・フレーズを延々と繰り返す壊れた生成に陥る現象を実機で確認
(「もっと」が数百回続く例)。`pipeline/llm.py`で2段構えの対策:
1. `config.LLM_REPETITION_PENALTY`(既定1.3)による生成時の抑制
2. 同じ文字列パターン(1〜12文字)が5回連続したら強制的に生成を打ち切る安全弁(単体テスト済み)

この対策により、句読点が出ない暴走生成でストリーミングTTSが機能しなくなる問題も同時に解消した。

## 未解決・今後の課題

- **体感1秒の目標には未達**(現状1.5〜2秒程度)。さらに詰めるなら: STT/LLMモデルをさらに小さくする、
  VADの無音待ちを削る、LLMの応答をさらに短くする、といった方向性がある
- **discord-ext-voice-recvへの依存自体がリスク**: 非公式・実験的ライブラリであり、DAVE復号パッチも
  内部API依存。ライブラリ更新やDiscord側の仕様変更で壊れる可能性が常にある
- **複数話者・複数人通話は非対応**: 「1対1の電話」という前提でTARGET_USER_ID固定の設計
- **自動テスト・CIは無し**: 動作確認は全て手動 + このセッション内でのユニットテスト(音声変換関数、
  繰り返し検知ロジックなど)のみ
- **Discordの「本物の通話(発信/着信)」機能はBot APIには存在しない**: 現在の着信はDM埋め込み+
  ボタンによる疑似演出。ユーザー自身が案内されたボイスチャンネルに手動で参加する必要がある

## ファイル構成

```
bot.py           - エントリポイント。libopus/DAVE等の起動時パッチもここに集約
call_flow.py      - 着信〜通話状態管理、STT→LLM→TTSパイプラインの結線
config.py         - 全設定値(モデル選択、TTS設定、VADしきい値、着信スケジュール等)
audio/sink.py     - リアルタイム音声受信 + Silero VAD + pre-roll(語頭欠落防止)
audio/player.py   - TTS音声のキュー再生 + 発話終了→初回発声ギャップ計測
pipeline/stt.py   - MLX Whisperラッパー
pipeline/llm.py   - mlx-lmラッパー(文単位ストリーミング + 繰り返し検知)
pipeline/tts.py   - VOICEVOX互換TTSクライアント(自動起動ロジック含む)
pipeline/metrics.py - ターン単位のレイテンシ計測ロガー(JSONL)
config.py         - 全設定値
ARCHITECTURE.md   - 現在のアーキテクチャ解説(構成・スレッドモデル・レイテンシ予算)
README.md         - セットアップ手順・使い方・チューニング指針
```

---

## 追記 (2026-08-04): ターンテイキングの方針と計測基盤の導入

### 決めた方針
会話の「快適さ」を、次の優先順位で追う:

1. **速さ**(応答レイテンシ)
2. **間の自然さ**(発話終了→AI発声のギャップ、語頭欠落、途中で切らない)
3. **検知の精緻化**は後回し — 処理が軽くなって必要になった段階で強化する

### コードから読み取った現状の仕組み(「話している / 話していない」の判定)
- `audio/sink.py` は Silero VAD を 32ms 単位で回し、**無音が `VAD_MIN_SILENCE_MS`(300ms)続いたら
  発話終了**とみなす「沈黙タイマー方式」。推論(STT→LLM→TTS)は**発話が終わってから初めて**走る。
- 出力側は既に文単位ストリーム化されている(`pipeline/llm.py` が句点ごとに yield → 即 TTS)。
- したがって体感遅延の主因は「発話終了検知の無音待ち + 発話後の直列パイプライン」。
  次に効くのは**入力側の投機化**(発話中のストリーミング STT → 世代 ID で破棄・修正 = toy-a の
  「動的並行」構想。barge-in の世代 ID 破棄モデルを入力側へ流用する)。

### このセッションで実装したもの(方針①②の起点 = まず測れるようにする + 軽量な自然さ改善)
1. **計測基盤**: `pipeline/metrics.py`(新規)+ `call_flow.py`。1ターン=1行の JSONL
   (`logs/turns.jsonl`)に、**話し終わった瞬間(speech_end)を起点**とした各段レイテンシを記録。
   - `stt_ms` / `llm_first_sentence_ms` / `tts_first_chunk_ms`
   - `response_gap_ms`(発話終了→AI音声が実際に鳴り始めるまで = 体感/間の直接指標)
   - `utterance_ms` / `pre_roll_ms` / `ended_at_max` / `n_sentences` / `stt_text`
   - 無音・幻聴で応答しないターンも記録(検知チューニングの材料)
   - `config.METRICS_ENABLED` / `METRICS_LOG_PATH` で制御
2. **間の自然さ(pre-roll)**: `audio/sink.py`。発話確定直前の音を `VAD_PRE_ROLL_MS`(300ms)ぶん
   リングバッファに保持し、発話先頭へ連結。VAD 検知の遅れによる**語頭欠落**(「…んにちは」)を防ぐ。
   `on_utterance` は `VadResult`(PCM + VAD時刻メタ)を渡す形に拡張。
3. **ギャップ計測フック**: `audio/player.py`。`begin_turn()` で発話終了時刻をセットし、応答の
   **最初の実フレームを再生した瞬間**に経過(ms)を1回だけ通知 → `response_gap_ms` を正確に採取。

### 検証(この環境でできた範囲)
- 全変更ファイルの `py_compile` OK、`call_flow.py` の実 MLX/whisper スタック込み import OK。
- 機能ユニットテスト 12/12 PASS(metrics の JSONL 出力 / player のギャップ1回発火 /
  pre-roll の容量トリムと発話先頭連結)。
- **未実施**: 実 Discord 通話での端から端の駆動(トークン・ボイスチャンネル・常駐モデルが必要で
  この環境では不可)。実機で1通話するだけで `logs/turns.jsonl` が埋まる。

### 次にやること
1. 実機で `LOG_LATENCY=True` のまま数ターン会話 → `logs/turns.jsonl` に実測を溜める。
2. `response_gap_ms` と各段の内訳で**どこが体感を食っているか**を確定。
3. 実データで `VAD_MIN_SILENCE_MS`(速さ↔分断)と `VAD_PRE_ROLL_MS` を詰める。
4. 十分軽ければ入力側の投機(ストリーミング STT → 世代 ID 破棄)へ着手。

---

## 追記 (2026-08-05): 類似OSS調査と、そこからの4機能取り込み + WARP/SSL修正

### 経緯
類似OSSを調べ(結果は別リポジトリ `../oss_ref/COMPARISON.md`)、現行botへ「本当に必要な条件」を
取り込んだ。**優先5本**を `../oss_ref/` に clone して比較:
hermes-agent(MIT) / openclaw-voice(ライセンス無し) / speech-to-speech(Apache-2) /
pipecat(BSD-2) / livekit-agents(Apache-2)。

**調査の結論(ランキングの読み替え)**:
- 現行botの最大リスク(DAVE復号 + opusパスの手パッチ)の“参照実装”は **hermes-agent の
  `plugins/platforms/discord/adapter.py`**(voice-recv非依存の自前RTP受信でDAVE `dave_session.decrypt`まで)。MIT。
- 「いつ喋るか(Smart Turn/発言可否)」は **openclaw-voice** が実装しているが**ライセンス不在=概念参照のみ**。
  Smart Turnモデルの本家は **pipecat(BSD-2)** なのでモデル/実装はそこから取った。
- 5本とも「AI発の発信(疑似着信スケジューラ)」は無し = 現行botの独自価値。

### 実装した4機能(取り込み優先度 #2→#3→#1→#4)。**すべて config で切替・既定OFF=現行動作を維持**
実機で往復動作している資産を壊さないため、リスクのある変更は opt-in にし、OFF経路は現行と同一。

1. **#2 起動前診断 `scripts/voice_doctor.py`(新規)**: libopus(Homebrew既知パス)/davey=DAVE/
   VOICEVOX応答/Bot権限を一括チェック。「繋がるのに聞こえない」系の切り分け用。
   hermesの `discord-voice-doctor.py` を本プロジェクトの config/.env に合わせて再実装(MIT参照)。
   実行OK(実機で davey/opus/権限Adminを検出、VOICEVOX未起動はwarn)。
2. **#3 Smart Turn v3.2 による意味的終話検出(既定OFF)**:
   - `pipeline/turn_detector.py`(新規) + `pipeline/_whisper_features.py`(pipecat=BSD-2 を vendor)。
   - モデル `models/smart-turn-v3.2-cpu.onnx`(8.7MB)は **.gitignore除外**、入手手順は `models/README.md`。
   - `audio/sink.py` に opt-in 統合: 沈黙で「終了」判定が出た瞬間に発話全体をSmart Turnへかけ、
     未完了なら区切らず継続(`awaiting_continuation`)。誤判定でハングしないよう `SMART_TURN_MAX_MS` の安全弁。
   - config: `SMART_TURN_ENABLED/THRESHOLD/MAX_MS/MODEL_PATH`。実ONNX推論 + sink4経路テストPASS。
3. **#1 DAVE受信の堅牢化(既定OFF)**: `bot.py` の復号を純関数 `_dave_decrypt_or_passthrough` に抽出し、
   hermes準拠の **passthrough(非暗号化フレーム)復帰**を追加。現行は例外時に全破棄していたが、
   `"Unencrypted"`系の例外はDAVE前データをそのままOpusへ通す。config: `DAVE_PASSTHROUGH_RECOVERY`。全分岐テストPASS。
4. **#4 出力の質感 ambient+ducking(既定OFF)**: 現行 `QueuedPCMSource` は既に連続sourceで
   `is_playing()`レースは無かったため、hermes `voice_mixer` 由来の **常時アンビエント + 発話中ダッキング**
   だけを `audio/player.py` に追加。config: `AMBIENT_ENABLED/WAV_PATH/IDLE_GAIN/DUCK_GAIN`。
   OFFは byte-identical(ギャップ計測も維持)、ONの混合/duck/loop/clipテストPASS。

commit: `8a5fed5`(4機能)。ライセンス帰属は各ファイル冒頭に保持(pipecat=BSD-2 / hermes=MIT)。

### 重大な実機バグ修正: Cloudflare WARP のTLS検査でログインが落ちる
`python3 bot.py` が `SSLCertVerificationError: self-signed certificate in certificate chain` で
起動直後に落ちた。原因は**このMacで動く Cloudflare WARP/Zero Trust のTLS検査**で、discord.comの証明書を
`Gateway CA - Cloudflare Managed` 発行のものに差し替えていたこと。ブラウザ/curlは macOSキーチェーンで
このCAを信頼するので動くが、Python(uvのcpython + certifi)は certifi バンドルしか見ないため落ちる。
→ `bot.py` 冒頭で **`truststore.inject_into_ssl()`** を呼び、SSL検証をmacOSキーチェーンに切替。
WARPのON/OFFに関係なく繋がる。truststoreが無い環境ではcertifiにフォールバック。
`requirements.txt` に `truststore>=0.9` 追加。注入後にaiohttpでdiscord `/users/@me` がHTTP 200を確認。
commit: `1c7d24c`。**再発時のチェック順**: `curl https://discord.com`(=システムは200か)→ `pgrep -fi warp`
→ Pythonだけ落ちるならこのパターン。

### 実機で機能を有効化する順番(全部 config、既定は全OFF=現行動作)
1. `.venv/bin/python scripts/voice_doctor.py` で前提確認。
2. まず全OFFで `logs/turns.jsonl` に実測を溜める。
3. 早切りが気になれば `SMART_TURN_ENABLED=True`(`SMART_TURN_THRESHOLD`で間の長さ調整)。
4. 音が欠ける兆候があれば `DAVE_PASSTHROUGH_RECOVERY=True`。
5. 通話の"間"を生かしたければ `AMBIENT_ENABLED=True`(`IDLE/DUCK_GAIN`微調整)。

### 追加・変更ファイル(この追記時点)
- 新規: `scripts/voice_doctor.py`, `pipeline/turn_detector.py`, `pipeline/_whisper_features.py`,
  `models/README.md`, `IMPL_PROGRESS.md`(作業チェックリスト)。
- 変更: `bot.py`(truststore注入 + DAVE純関数化), `config.py`(4機能の設定, 全既定OFF),
  `audio/sink.py`(Smart Turn統合), `audio/player.py`(ambient+ducking), `call_flow.py`(detector/player配線),
  `README.md`, `requirements.txt`, `.gitignore`(models/*.onnx除外)。
- 参照専用(このリポジトリ外): `../oss_ref/`(clone 5本 + `COMPARISON.md` + `PROGRESS.md`)。

### 未実施・次の候補
- **実Discord通話での端から端の駆動は未検証**(この作業環境ではトークン/VC/常駐モデル駆動が不可)。
  → まず実機で1通話し、各機能をONにした時の体感/ログを確認する。
- Smart Turn の `SMART_TURN_THRESHOLD` を実データ(`logs/turns.jsonl`)で調整。
- (大改修・任意)受信パスを hermes 方式(voice-recv非依存の自前RTP受信)へ本格移行すれば、
  現行の内部API手パッチ依存リスクを根本的に減らせる。ブループリントは `../oss_ref/hermes-agent/.../adapter.py`。

---

## 追記 (2026-08-05 その2): 初の実通話・計装追加・コールドスタート起因の遅延を特定/修正

`NEXT_THREAD_PROMPT.md` の指示(実通話でSmartTurn閾値を実データ調整)に沿って、**初めて実Discord通話を駆動**した。
Smart Turn の閾値チューニング自体は**未完**だが、その前提となる計装追加と、通話品質を著しく損ねていた
コールドスタート遅延の**根本原因特定と修正**まで到達した。

### 1. 計装: Smart Turn の判定値を turns.jsonl に記録(閾値チューニングの土台)
`pipeline/metrics.py` は speech_end 起点のレイテンシしか記録しておらず、Smart Turn の確率や継続回数が
残らず「閾値調整が勘になる」状態だった。以下2フィールドを sink→call_flow→metrics に通した:
- `smart_turn_prob`: ターン確定時の最後の完了確率(OFF/未実行なら null)
- `continuation_count`: 「まだ続く」で発話を延長した回数(OFFなら0)
変更: `pipeline/metrics.py`(TurnRecord), `audio/sink.py`(`VadResult`/`_UserState`/`_should_continue`/
`_finish_utterance` で状態退避・リセット), `call_flow.py`(TurnRecord構築で受け渡し)。
OFF経路は音声挙動 byte-identical、JSONLに2列増えるだけ。単体スクリプトで状態遷移PASS。

### 2. 実通話で判明した最大の問題: 「遅れて似た応答が来る」= コールドスタート起因
実通話で「AIの応答が順番前後で遅れて再生される(似た内容が後から鳴る)」現象が出た。切り分けた結果:
- **原因はコールドスタート**。ロード時warmup(既存)は効くが、**bot起動〜/call-me着信までのアイドル中に
  8GB機ではMLXの重み/Metal状態が退避**され、初回発話のSTTが7〜14秒に膨れる(2ターン目以降は0.3〜1.4s)。
  この初回だけ極端に遅い応答が、後続ターンの処理を追い越して**順番前後で遅れて再生**されていた。
- **実測で確定**(scratchの実験): アイドル 30s→0.47s / 60s→1.28s / 90s→2.21s と単調悪化。
  一方 warmup を撃った直後の transcribe は 0.26s に回復。→ 「ロード時warmupの強化」ではなく
  **「通話開始時に温め直す」**のが正しい対処と判明(無音warmupでもデコード経路は温まる=STT warmup強化は不要)。
- **Smart Turn は無罪**。ON時の prob は実測で全ターン 0.97〜0.99、`continuation_count`=0(=一度も延長せず、
  +40msの推論のみ)。遅延はSmart Turnと無関係だった。

### 3. 修正: 通話開始時にモデルを温め直す(挨拶TTSと並行)
`SpeechToText._warm_up`/`LanguageModel._warm_up` を公開 `warm_up()` にリネーム。
`call_flow.py` の `_begin_call` で、挨拶(GREETING)の合成・再生と**並行して** `_warm_up_models()` を
`asyncio.gather` で撃つ(STT/LLM/SmartTurnを空撃ち、例外は握って通話継続)。挨拶の裏でコールドコスト
(~3s)を吸収し、最初のユーザー発話を温かい状態で迎える。実測で warmup後の初回 transcribe は 0.26s。
変更: `pipeline/stt.py`, `pipeline/llm.py`, `call_flow.py`。

### 未実施(次スレッドの主タスク)
- **Smart Turn 閾値(`SMART_TURN_THRESHOLD`)の実データ調整は未完**。理由: これまでの実通話では prob が
  常に 0.97+ で `continuation_count`=0 のため、閾値0.5では早切り防止が発火していない。**息継ぎを挟んだ
  途中終話(prob が中間値になるケース)のデータが必要**。再現には、途中で **0.5〜1秒のはっきりした間**を空ける
  (`VAD_MIN_SILENCE_MS=300` 未満だとVADが終了判定せずSmart Turnの出番が来ない)。詳細は `NEXT_THREAD_PROMPT.md`。
- **LLMのターンまたぎ同一応答**: 小型1.5Bがゴミ入力(早切り/幻聴STT)に対し汎用の逃げ応答
  「わかりました。お手伝いできることが…」を返し、ターンをまたいで同一化する(繰り返しペナルティ1.3は
  1生成内にしか効かない)。閾値チューニングとは別枠の課題。対策候補: 逃げ応答の抑制/直近応答との
  重複チェック/SYSTEM_PROMPT調整/モデル差し替え。
- **バージイン(割り込み)未実装**: 新発話開始時に前ターンの生成/再生キューを破棄する仕組みが無く、
  今回はコールドスタートで露呈した。warmup修正で大半は緩和されるが、ユーザがAIに被せて話す場合に備え
  将来的にはキャンセル機構が要る(player/call_flow 改修・大きめ)。

### この追記時点の変更ファイル
- 変更: `pipeline/metrics.py`, `audio/sink.py`, `call_flow.py`(計装 + 通話開始warmup),
  `pipeline/stt.py`, `pipeline/llm.py`(`_warm_up`→公開`warm_up`)。
- `config.py` の `SMART_TURN_ENABLED` は検証中に一時 True にしたが、**クリーンなONラウンド未完のため
  既定OFF(False)に戻してコミット**(方針「既定OFF・検証後ON」を維持)。
- `logs/turns.jsonl` は .gitignore 対象(実験データ、コミットしない)。実通話のbaseline/ON実測が入っている。
