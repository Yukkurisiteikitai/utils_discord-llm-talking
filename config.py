"""Bot全体の設定。全て.envと定数から読み込む。"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"環境変数 {name} が.envに設定されていません。.env.exampleを参考に設定してください。"
        )
    return value


def _require_env_int(name: str) -> int:
    return int(_require_env(name))


DISCORD_TOKEN = _require_env("DISCORD_TOKEN")
TARGET_USER_ID = _require_env_int("TARGET_USER_ID")
GUILD_ID = _require_env_int("GUILD_ID")
VOICE_CHANNEL_ID = _require_env_int("VOICE_CHANNEL_ID")

# ---------------------------------------------------------------------------
# STT (MLX Whisper)
# ---------------------------------------------------------------------------
# 8GBのM3 MacBook Airを想定した既定値。精度を上げたい場合は
# "mlx-community/whisper-medium-mlx" 等に変更(その分RAM消費とレイテンシが増える)。
WHISPER_MODEL_REPO = "mlx-community/whisper-small-mlx"
WHISPER_LANGUAGE = "ja"

# ---------------------------------------------------------------------------
# LLM (mlx-lm)
# ---------------------------------------------------------------------------
# 1.5B・4bit量子化。8GB RAMでの常駐運用と1秒応答を優先した選択。
# 応答品質を上げたい場合は "mlx-community/Qwen2.5-3B-Instruct-4bit" 等に変更可能。
LLM_MODEL_REPO = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"
LLM_MAX_TOKENS = 120
LLM_TEMPERATURE = 0.7
# 小型モデルは同じ単語を繰り返す壊れた生成に陥りやすいため、繰り返しに
# ペナルティをかける(1.0で無効、大きいほど強く抑制。1.3程度が実用的)。
LLM_REPETITION_PENALTY = 1.3

# 保持する直近の会話ターン数(多いほどプロンプトが長くなり応答が遅くなる)。
HISTORY_TURNS = 6

SYSTEM_PROMPT = (
    "あなたは電話で話している親しい相手です。実際の電話の会話のように、"
    "短く、自然な話し言葉で返答してください。"
    "1回の発言は1〜2文まで。長い説明や箇条書きは絶対にしないでください。"
    "「うんうん」「なるほど」のような短い相槌も自然に使ってください。"
    "敬語ではなくフランクな口調で話してください。"
)

# ---------------------------------------------------------------------------
# TTS (VOICEVOX ENGINE, ローカルHTTPサーバー)
# ---------------------------------------------------------------------------
# 実測でAivisSpeechはこのMac(CPUのみ、GPU/CoreMLアクセラレーション不可)だと
# 1文あたり1.2〜2.2秒かかり、速度優先の方針に切り替えたためVOICEVOXを既定にした。
# AivisSpeechに戻したい場合はbase_url/speaker/binary_path/app_nameを
# AivisSpeech用の値に戻すだけで、pipeline/tts.pyのロジックはそのまま動く
# (両エンジンともHTTP APIが /audio_query -> /synthesis で互換のため)。
TTS_ENGINE_BASE_URL = "http://127.0.0.1:50021"
# 話者ID。既定は「ずんだもん」ノーマル。`GET /speakers` で一覧取得可能。
TTS_SPEAKER_ID = 3
TTS_ENGINE_TIMEOUT_SEC = 15

# エンジンが起動していない場合にBot側から自動起動するための設定(macOS専用)。
#
# 実測で判明した重要な点: `open -a <アプリ名>` でGUIアプリ経由で起動すると
# Electron本体の起動オーバーヘッドで数分かかることがある(AivisSpeechでは
# 実に3分40秒、RAMも余分に食う)。一方、GUIアプリに同梱されているENGINE
# 実行ファイルを直接ヘッドレスで叩くと数十秒で起動する。そのため既定では
# 直接起動を使う。
#
# GUIアプリを一度もインストール/初回起動していない場合、Gatekeeperの
# セキュリティ確認がまだ済んでいない可能性が高いので、その場合だけ
# 手動でGUIアプリを一度起動して承認する必要がある(README参照)。
TTS_ENGINE_BINARY_PATH = "/Applications/VOICEVOX.app/Contents/Resources/vv-engine/run"
TTS_ENGINE_APP_NAME = "VOICEVOX"  # 直接起動できない場合のGUIフォールバック用
TTS_ENGINE_LAUNCH_TIMEOUT_SEC = 60

# ---------------------------------------------------------------------------
# VAD (Silero VAD)
# ---------------------------------------------------------------------------
VAD_SAMPLE_RATE = 16000
# Silero VADが要求する固定チャンクサイズ(16kHzの場合512サンプル=32ms)。
VAD_CHUNK_SAMPLES = 512
VAD_THRESHOLD = 0.5
# 実測ではSTT(~0.6s)+LLM初手(~0.5-1.0s)だけで1秒予算の大半を使うため、
# ここでの無音待ちは短めにして体感速度を優先する(短すぎると発話中の
# 息継ぎで誤って区切ってしまうので、300ms程度が実用上のバランス)。
VAD_MIN_SILENCE_MS = 300
# これより短い発話区間は誤検出とみなして無視する。
VAD_MIN_SPEECH_MS = 250
# 発話がこれより長く続いたら強制的に区切ってSTTに回す(無限に溜め込まない安全弁)。
VAD_MAX_UTTERANCE_MS = 15000
# 発話確定の直前(閾値を超える前)の音を、この長さぶんだけ発話先頭に付け足す。
# VADが発話開始を検知するのは既に音が立ち上がった後なので、この「先読み」が
# 無いと語頭の子音が欠けて「…んにちは」のように不自然になる(間の自然さ)。
# 0にするとpre-rollを無効化する。
VAD_PRE_ROLL_MS = 300

# ---------------------------------------------------------------------------
# アンビエント + ダッキング(出力の質感) ※既定OFF
# ---------------------------------------------------------------------------
# 現行の再生sourceは既に「一度play()して無音を返し続ける連続source」で、
# is_playing()レースは無い。ここで足すのは Hermes(MIT)の voice_mixer 由来の
# 「常時鳴る低音のアンビエント + 発話中のダッキング」で、無音の"間"を死なせず
# 通話が生きている感じ(Grokのボイスモード的な質感)を作る。True で有効化。
# 音声ファイルを指定しない場合は空調のようなソフトノイズを自動生成する。
AMBIENT_ENABLED = False
AMBIENT_WAV_PATH = ""            # 任意: ループ再生したいWAVのパス。空なら自動生成
AMBIENT_IDLE_GAIN = 0.06         # 発話していないときの音量(0〜1、小さめ推奨)
AMBIENT_DUCK_GAIN = 0.02         # 発話中に絞ったときの音量(idleより小さく)

# ---------------------------------------------------------------------------
# DAVE(E2EE音声)受信の堅牢化 ※既定OFF・実機で検証してからONにする
# ---------------------------------------------------------------------------
# 現行の DAVE 復号パッチ(bot.py)は session.decrypt() が例外を投げたパケットを
# すべて破棄している。Hermes Agent(MIT)の受信実装は、例外が「Unencrypted」系
# =DAVEで暗号化されていない passthrough フレームの場合は、DAVE前(NaCl復号済み)の
# データをそのまま Opus に通して音声を拾う。True にすると、この passthrough 復帰を
# 有効化し、本来拾えるはずの音声を捨てずに済む。判定を誤ると壊れたデータを
# デコーダに渡す可能性があるため、まず現行の全skip(False)で実機確認してから。
DAVE_PASSTHROUGH_RECOVERY = False

# ---------------------------------------------------------------------------
# Smart Turn v3(意味的な終話検出) ※既定OFF・実機で検証してからONにする
# ---------------------------------------------------------------------------
# 沈黙タイマー(VAD_MIN_SILENCE_MS)だけだと文中の息継ぎで早切りすることがある。
# Silero VADが「無音で発話終了」と判定した瞬間に、発話音声全体を Smart Turn へ
# かけて「本当に喋り終わったか」を確率で確認する。未完了と判定されたら発話を
# 区切らずに継続し、より自然な間を待つ。追加の推論コスト(CPUで数十ms/終話点)が
# 乗るので、まず False のまま `logs/turns.jsonl` で沈黙方式の実測を取り、
# 早切りが問題になってから True にするのが安全。
SMART_TURN_ENABLED = False
SMART_TURN_MODEL_PATH = "models/smart-turn-v3.2-cpu.onnx"
# prob > この値で「発話完了」。上げるほど「まだ続く」と判断されやすく(=待つ)なる。
SMART_TURN_THRESHOLD = 0.5
# Smart Turnが「未完了」と言い続けても、発話全体がこの長さを超えたら強制的に
# 区切る安全弁(誤判定でターンが延々と閉じないのを防ぐ)。VAD_MAX_UTTERANCE_MSの
# 内側で効く短めの上限。
SMART_TURN_MAX_MS = 8000

# ---------------------------------------------------------------------------
# 計測(ターンテイキング / レイテンシのデータ収集)
# ---------------------------------------------------------------------------
# 速さ(応答レイテンシ)と間の自然さ(発話終了→AI発声開始のギャップ)を
# 実会話から測るための最小の計測基盤。1ターン=1行のJSONLで追記する。
# これが無いと「速く・自然に」のチューニングが勘になるため、まずここを起点にする。
METRICS_ENABLED = True
METRICS_LOG_PATH = "logs/turns.jsonl"

# ---------------------------------------------------------------------------
# Discordの音声フォーマット定数
# ---------------------------------------------------------------------------
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2  # 16-bit PCM

# ---------------------------------------------------------------------------
# 着信スケジュール
# ---------------------------------------------------------------------------
# ランダム着信の間隔(秒)。この範囲でランダムに次の着信時刻を決める。
CALL_INTERVAL_MIN_SEC = 60 * 60 * 2   # 2時間
CALL_INTERVAL_MAX_SEC = 60 * 60 * 6   # 6時間
# 着信ボタンのタイムアウト(秒)。応答がなければ「不在着信」として終了する。
CALL_RING_TIMEOUT_SEC = 30

# レイテンシ計測ログを出すかどうか(チューニング用)。
LOG_LATENCY = True
