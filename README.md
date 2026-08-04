# discord_caller

Discord Botから「電話がかかってくる」体験を再現するBot。ランダム/スケジュールでDMに着信が来て、
応答するとボイスチャンネルで会話が始まる。音声認識(STT)・応答生成(LLM)・音声合成(TTS)は
すべてMac上でローカル完結し、体感1秒程度での応答を目指す。

対象環境: MacBook Air M3 / 8GB RAM(Apple Silicon前提。MLXを使用するためIntel Macでは動作しない)。

## アーキテクチャ

```
ユーザーの発話 (Discordボイスチャンネル)
  -> discord-ext-voice-recv でリアルタイムPCM受信
  -> Silero VAD で発話区間を検出(発話終了を検知した瞬間に次へ)
  -> MLX Whisper でSTT(常駐ロード)
  -> mlx-lm でLLM応答をストリーミング生成、文が完成するたびに即TTSへ
  -> VOICEVOX ENGINE (ローカルHTTPサーバー) でTTS
  -> カスタムAudioSourceでDiscordのボイスチャンネルへPCM再生
```

各段階を直列に待たず、LLMが後続の文を生成している間に前の文のTTS・再生を並行して進めることで
体感レイテンシを下げている(詳細は `pipeline/llm.py`, `call_flow.py` のコメント参照)。

## 前提セットアップ

1. **Discord Bot作成**: [Discord Developer Portal](https://discord.com/developers/applications) でアプリケーション/Botを作成し、トークンを取得。
   - Bot設定で **SERVER MEMBERS INTENT** を有効にする(必須)
   - OAuth2 URL Generatorで `bot`, `applications.commands` スコープと、`Connect`/`Speak`/`Send Messages`/`View Channels` 権限を付与してサーバーに招待する
2. **VOICEVOX**: https://voicevox.hiroshiba.jp/ (または [GitHub Releases](https://github.com/VOICEVOX/voicevox/releases))からダウンロードし、`/Applications` にインストールしておく(Apple Silicon版=`arm64`推奨)。既定では `http://127.0.0.1:50021` で待ち受け、話者は既定で「ずんだもん」(speaker id 3)。
   - **初回だけ手動で一度起動して承認することを推奨**: `curl`等でダウンロードした場合はGatekeeperの隔離属性(quarantine)が付かず無審査で起動できることを確認済みだが、Finder/ブラウザ経由でダウンロードした場合は初回起動時に「開発元を確認できないため使用がブロックされました」と止められることがある。その場合は「システム設定→プライバシーとセキュリティ→このまま開く」で一度手動承認しておく。
   - Botは起動時および着信直前に、VOICEVOXが起動していなければ自動で起動を試みる(`pipeline/tts.py` の `ensure_running()`)。実測では、GUIアプリを `open -a <アプリ名>` で起動するとElectron自体の初期化で数分かかることがあった(AivisSpeechでは実に3分40秒)ため、既定では `config.TTS_ENGINE_BINARY_PATH`(`/Applications/VOICEVOX.app/Contents/Resources/vv-engine/run`)を直接ヘッドレスで起動する方式にしている。実行ファイルが見つからない場合のみ `open -a` にフォールバックする。AivisSpeechに戻したい場合は `config.py` の `TTS_ENGINE_BASE_URL`(`http://127.0.0.1:10101`)・`TTS_SPEAKER_ID`・`TTS_ENGINE_BINARY_PATH`/`TTS_ENGINE_APP_NAME` を書き換えれば同じ仕組みで動く。
3. **.env の設定**: `.env.example` を参考に以下を設定する(`DISCORD_TOKEN` は設定済み)。
   - `TARGET_USER_ID`: 電話をかける相手(自分)のDiscordユーザーID。Discordの「開発者モード」を有効にして自分のプロフィールを右クリック→「IDをコピー」
   - `GUILD_ID`: Botと自分が両方参加しているサーバーのID
   - `VOICE_CHANNEL_ID`: 通話に使うボイスチャンネルのID

## セットアップ

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

## 起動

```bash
source .venv/bin/activate
python3 bot.py
```

起動時にWhisper/LLMモデルをダウンロード・常駐ロードする(初回は数分かかることがある)。
「モデルのロード完了」のログが出たら準備完了。

## 使い方

- `/call-me` — スケジュール待ちせず即座に着信をテストする
- 着信DMの「応答」ボタンを押し、案内された `VOICE_CHANNEL_ID` のボイスチャンネルに参加すると通話が始まる
- 通話中は話しかけると自動で応答が返る
- `/hangup` — 通話を終了する(ボイスチャンネルから退出しても自動終了する)

## チューニング

`config.py` にモデル選択・VADしきい値・システムプロンプト・着信間隔などをまとめている。
`config.LOG_LATENCY = True`(既定)にしておくと、発話ごとに以下のログが出るのでボトルネックを特定できる:

```
[latency] STT: 0.4s -> '今日どうだった?'
[latency] LLM first sentence: 0.7s -> 'まあまあかな。'
[latency] TTS chunk ready: 0.9s -> 'まあまあかな。'
```

### 実測値(このMac, M3 MacBook Air 8GB, whisper-small-mlx + Qwen2.5-1.5B-Instruct-4bit)

VOICEVOXを除くSTT/LLM単体をこの環境で実測したところ、モデル常駐後(=通話が始まってから2回目以降)は
以下の通りだった:

- STT(4.5秒の発話を書き起こし): **約0.58秒**
- LLM(最初の一文が生成されるまで): **約0.5〜1.0秒**

どちらも起動直後の**1回目の呼び出しだけMLXのMetalカーネルJITコンパイルで数秒〜10秒近くかかる**ため、
`pipeline/stt.py` と `pipeline/llm.py` はどちらもコンストラクタ内でダミー入力を1回実行して
このコストを起動時(モデルロード中)に前払いする設計にしてある。ここが抜けていると、
実際の通話の最初の一言だけ極端に遅くなるので要注意。

**VOICEVOX実機実測(このMac)**:

- TTS合成(短い一文): **約0.6〜0.75秒**(AivisSpeechの1.2〜2.2秒より2〜3倍速い)

VAD無音待ち(300ms)+STT(~0.6s)+LLM初手(~0.5〜1.0s)+TTS(~0.6〜0.75s)を合計すると、
体感レイテンシはおおむね**1.5〜2秒前後**まで縮まる。それでも当初目標の「1秒」ちょうどには届かない
場合があるが、AivisSpeech構成(2.5〜3秒)よりは大幅に改善している。より自然な声を優先したい場合は
AivisSpeechに戻すこともできる(上記セットアップ節参照)。

話者(声質)を変えたい場合は、VOICEVOXが起動している状態で `curl http://127.0.0.1:50021/speakers`
にアクセスすると、インストール済みの話者一覧とそれぞれの `style_id` が確認できる。取得した
`style_id` を `config.TTS_SPEAKER_ID` に設定する。

体感速度が足りない場合は、まず `WHISPER_MODEL_REPO` を `whisper-base-mlx` に、`LLM_MODEL_REPO` を
より小さいモデル(`Qwen2.5-0.5B-Instruct-4bit`等)に変更してみること。逆に品質を上げたい場合は
`whisper-medium-mlx` や `Qwen2.5-3B-Instruct-4bit` などに変更できる(その分RAM消費とレイテンシが増える)。

## bot.py起動時に適用している重要なパッチ

実機デバッグで判明した、Discordのボイス受信まわりの深刻な問題への対処が `bot.py` 冒頭にまとまっている。
いずれも起動時に自動適用されるので普段は意識する必要はないが、動作原理を変更する場合は要注意:

1. **`_ensure_opus_loaded()`**: macOS(特にApple Silicon/Homebrew)では `ctypes.util.find_library('opus')`
   が `/opt/homebrew` 配下のlibopusを見つけられず、`discord.opus.OpusNotLoaded` で
   `voice_client.play()` が失敗することがある。Homebrewの既知パスに直接フォールバックする。
2. **`_patch_voice_recv_resilience()`**: `discord-ext-voice-recv` は、Opusデコード中に1回でも例外
   (`corrupted stream` 等)が起きると受信スレッド全体が停止し、以後ずっと音声を受信できなくなる
   重大なバグを持つ。パケット単位でエラーを握りつぶしてループを継続するようパッチしている。
3. **`_patch_voice_recv_dave_decrypt()`**: **最も重要な修正。** Discordは2026年3月にボイスチャンネルの
   E2EE(DAVEプロトコル)を必須化した。discord.py本体はDAVEに対応済み(`davey`ライブラリ)だが、
   `discord-ext-voice-recv` はDAVEの存在を一切知らないため、受信した音声が常にDAVEで暗号化されたまま
   Opusデコーダに渡され100%の確率で失敗していた。discord.pyが保持している`davey.DaveSession`を使い、
   Opusデコード直前にDAVE復号を追加で行うことで対処している。このパッチが無いと**通話は開始できても
   一切の発話を認識できない**(このBotの中核機能が動かない)。
4. **`_quiet_voice_recv_logging()`**: 上記のエラー処理中に出る大量のログ(特に`log.exception`による
   フルトレースバック出力)自体がCPUを消費し、応答レイテンシが数秒〜数十秒に悪化する現象を確認した。
   ログを軽量なカウンタ表示に絞っている。

これらは全て discord.py / discord-ext-voice-recv の内部実装へのモンキーパッチであり、
両ライブラリのバージョンが変わると動作しなくなる可能性がある。

## LLMの繰り返し生成対策

小型モデル(1.5B)は同じ単語・フレーズを延々と繰り返す壊れた生成に陥ることがある(実機で「もっと」が
数百回続く例を確認済み)。`pipeline/llm.py` で2段構えの対策をしている:

1. `config.LLM_REPETITION_PENALTY`(既定1.3)による生成時の抑制
2. それでも発生した場合に備え、同じ文字列パターン(1〜12文字)が5回連続したら強制的に生成を打ち切る安全弁

## 既知の制約・注意点

- **8GB RAM環境向けの構成**: Whisper+LLM+TTSエンジンを同時に動かすため、通話中はブラウザなど他の重いアプリを
  閉じておくことを推奨する。スワップが発生するとレイテンシが大きく悪化する。
- **TTSエンジンの自動起動はmacOS専用かつ初回手動承認が前提**: `open -a` によるBotからの
  自動起動は、一度でも手動でアプリを開いてGatekeeperの確認を済ませていないと失敗する
  (`[tts] ...秒待ちましたがエンジンが応答しません` というログが出た場合はこのケースを疑うこと)。
- **discord-ext-voice-recv は実験的なライブラリ**: Discord公式にはBotの音声受信APIは無く、
  コミュニティ製の非公式拡張に依存している。上記のDAVEパッチも含め、Discord側の仕様変更や
  ライブラリの更新で動かなくなる可能性がある。
- **Discordの「通話(発信/着信)」機能はBot APIには存在しない**: 本Botの着信はDM埋め込み+ボタンによる
  疑似演出であり、ユーザー自身が案内されたボイスチャンネルに手動で参加する必要がある。
