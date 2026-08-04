# OSS知見の取り込み 実装進捗

`oss_ref/COMPARISON.md` の推奨を現行botへ実装する作業。順番: **#2 → #3 → #1 → #4**。
方針: 実機駆動不可の環境なので、リスクの高い変更は **config フラグで opt-in・既定は現状維持**、
`py_compile` + ユニットテストで検証。実機ON/OFFはユーザが切替。

## チャンク
- [x] #2 voice doctor(起動前診断) … 新規 `scripts/voice_doctor.py`。実行OK(davey/opus/VOICEVOX/権限を検出)
- [x] #3 Smart Turn v3 終話検知 … `pipeline/turn_detector.py`+`_whisper_features.py`+model vendor(BSD-2)。
      `audio/sink.py`にopt-in統合(既定OFF)。実ONNX推論+sink4経路テストPASS。config: SMART_TURN_*
- [x] #1 受信パス堅牢化 … Hermes(MIT)準拠の passthrough 復帰を `bot.py` の純関数
      `_dave_decrypt_or_passthrough` に抽出。config: DAVE_PASSTHROUGH_RECOVERY(既定False=現行維持)。全分岐テストPASS
- [x] #4 連続ミキサ+ducking … 現行sourceは既に連続sourceだったので、Hermes voice_mixer由来の
      ambient bed + ducking を `audio/player.py` に opt-in 追加。config: AMBIENT_*。OFFは byte-identical。テストPASS

## 状態: #2/#3/#1/#4 すべて実装・検証済み(2026-08-05)

### 検証範囲
- 全ファイル py_compile OK
- voice_doctor: 実行し davey/opus(既知パス)/VOICEVOX未起動/Bot権限を正しく検出
- Smart Turn: 実ONNX推論 + sink 4経路(OFF同一/継続/complete/安全弁) PASS
- DAVE helper: decrypted/passthrough(ON)/skip(OFF)/hardfail 全分岐 PASS
- player: OFF byte-identical(ギャップ計測維持)/ON idle・speech・clip・loop PASS
- call_flow wiring: _make_player OFF/ON, turn_detector既定None を確認
- **未実施**: 実Discord通話での端から端の駆動(トークン/VC/常駐モデルが要るためこの環境では不可)

### 実機で有効化する順番(全部 config で切替。既定は全部OFF=現行動作)
1. `.venv/bin/python scripts/voice_doctor.py` で前提を確認
2. まず現状(全OFF)で `logs/turns.jsonl` に実測を溜める
3. 早切りが気になれば `SMART_TURN_ENABLED=True`(SMART_TURN_THRESHOLD で間の長さ調整)
4. passthroughで音が欠ける兆候があれば `DAVE_PASSTHROUGH_RECOVERY=True`
5. 通話の"間"を生かしたければ `AMBIENT_ENABLED=True`(IDLE/DUCK_GAIN 微調整)

### 新規/変更ファイル
- 新規: `scripts/voice_doctor.py`, `pipeline/turn_detector.py`, `pipeline/_whisper_features.py`(BSD-2 vendor),
  `models/smart-turn-v3.2-cpu.onnx`(BSD-2, 8.7MB)
- 変更: `config.py`(4機能ぶんの設定, 全既定OFF), `bot.py`(DAVE純関数化), `audio/sink.py`(Smart Turn統合),
  `audio/player.py`(ambient+ducking), `call_flow.py`(detector/player配線)

### ライセンス
- Smart Turnモデル + `_whisper_features.py`: pipecat(BSD-2, Daily)由来。帰属はファイル冒頭に保持。
- DAVE passthrough / doctor / voice_mixer 発想: Hermes Agent(MIT)由来。

## ログ
- git 未コミット(ユーザ指示待ち)。models/ の 8.7MB onnx を含むため .gitignore 方針は要判断。
