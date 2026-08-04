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
