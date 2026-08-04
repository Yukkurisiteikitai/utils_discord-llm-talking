"""着信(DM Embed + ボタン)から通話終了までの状態管理と会話パイプラインの結線。"""
from __future__ import annotations

import asyncio
import enum
import time

import discord
import numpy as np
from discord.ext import voice_recv

import config
from audio.player import QueuedPCMSource, _generate_ambient_pcm
from audio.sink import UtteranceSink, VadResult
from pipeline.llm import LanguageModel
from pipeline.metrics import TurnLogger, TurnRecord
from pipeline.stt import SpeechToText
from pipeline.tts import TextToSpeech, TextToSpeechError

GREETING = "もしもし、聞こえる?"


class CallState(enum.Enum):
    IDLE = "idle"
    RINGING = "ringing"
    AWAITING_JOIN = "awaiting_join"
    IN_CALL = "in_call"


class IncomingCallView(discord.ui.View):
    def __init__(self, manager: "CallManager") -> None:
        super().__init__(timeout=config.CALL_RING_TIMEOUT_SEC)
        self._manager = manager

    @discord.ui.button(label="応答", style=discord.ButtonStyle.success, emoji="\U0001F4DE")
    async def answer(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await self._manager.on_answer(interaction)

    @discord.ui.button(label="拒否", style=discord.ButtonStyle.danger, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await self._manager.on_decline(interaction)

    async def on_timeout(self) -> None:
        await self._manager.on_ring_timeout()


class CallManager:
    """通話ライフサイクル(着信→応答待ち→通話中)と、STT→LLM→TTSパイプラインの結線を持つ。

    モデルのロードはコンストラクタでは行わず、load_models() で
    asyncio.to_thread 経由で行う(Discordゲートウェイのハートビートを
    ブロックしないため)。bot.py の on_ready から await して使う。
    """

    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self.state = CallState.IDLE
        self.voice_client: voice_recv.VoiceRecvClient | None = None
        self.player: QueuedPCMSource | None = None
        self.history: list[dict] = []
        self.loop: asyncio.AbstractEventLoop | None = None

        self.stt: SpeechToText | None = None
        self.llm: LanguageModel | None = None
        self.tts = TextToSpeech()
        # Smart Turn(意味的終話検出)。config.SMART_TURN_ENABLED が True のときだけ
        # load_models でロードする。None のとき sink は従来の沈黙タイマー方式のまま。
        self.turn_detector = None

        self.metrics = TurnLogger(config.METRICS_LOG_PATH) if config.METRICS_ENABLED else None
        self._turn_index = 0

    async def load_models(self) -> None:
        self.loop = asyncio.get_running_loop()
        # STT/LLMのロード(重いブロッキング処理)と、TTSエンジンの起動確認/自動起動を
        # 並行して行い、Botの起動時間を余分に伸ばさないようにする。
        self.stt, self.llm, tts_ready = await asyncio.gather(
            asyncio.to_thread(SpeechToText),
            asyncio.to_thread(LanguageModel),
            self.tts.ensure_running(),
        )
        if not tts_ready:
            print("[call] TTSエンジンが未起動のままです。着信時に再度自動起動を試みます。")

        if config.SMART_TURN_ENABLED:
            try:
                from pipeline.turn_detector import SmartTurnDetector

                detector = SmartTurnDetector(threshold=config.SMART_TURN_THRESHOLD)
                await asyncio.to_thread(detector.warmup)  # 初回ロードを通話前に払う
                self.turn_detector = detector
                print("[call] Smart Turn(意味的終話検出)を有効化しました")
            except Exception as e:  # noqa: BLE001
                # 失敗しても通話自体は従来の沈黙方式で成立させる。
                print(f"[call] Smart Turnのロードに失敗、沈黙方式で続行します: {e}")
                self.turn_detector = None

    def _make_player(self) -> QueuedPCMSource:
        """再生source を作る。config.AMBIENT_ENABLED のときだけアンビエント+
        ダッキングを有効にする。無効時は従来と同じ無音ベースの連続source。"""
        if not config.AMBIENT_ENABLED:
            return QueuedPCMSource()

        pcm = b""
        if config.AMBIENT_WAV_PATH:
            try:
                from audio.player import _wav_to_discord_pcm

                with open(config.AMBIENT_WAV_PATH, "rb") as f:
                    pcm = _wav_to_discord_pcm(f.read())
            except Exception as e:  # noqa: BLE001
                print(f"[call] アンビエントWAV読込失敗、自動生成に切替: {e}")
        if not pcm:
            pcm = _generate_ambient_pcm()
        return QueuedPCMSource(
            ambient_pcm=pcm,
            idle_gain=config.AMBIENT_IDLE_GAIN,
            duck_gain=config.AMBIENT_DUCK_GAIN,
        )

    # ------------------------------------------------------------------
    # 着信
    # ------------------------------------------------------------------
    async def start_incoming_call(self) -> None:
        if self.state is not CallState.IDLE:
            print("[call] 通話中のため着信をスキップしました")
            return

        if not await self.tts.ensure_running():
            print(
                f"[call] {config.TTS_ENGINE_APP_NAME} に接続できないため着信をスキップしました。"
                " アプリを起動して初回のセキュリティ確認を済ませてから再試行してください。"
            )
            return

        user = await self.client.fetch_user(config.TARGET_USER_ID)
        channel_name = self._voice_channel_name()
        embed = discord.Embed(
            title="\U0001F4DE 着信中...",
            description=(
                f"応答すると **#{channel_name}** に参加するよう案内されます。\n"
                f"{config.CALL_RING_TIMEOUT_SEC}秒以内に応答してください。"
            ),
            color=discord.Color.green(),
        )
        self.state = CallState.RINGING
        try:
            await user.send(embed=embed, view=IncomingCallView(self))
        except discord.Forbidden:
            print("[call] DMを送信できませんでした(相手のDM設定を確認してください)")
            self.state = CallState.IDLE

    def _voice_channel_name(self) -> str:
        channel = self.client.get_channel(config.VOICE_CHANNEL_ID)
        return channel.name if channel else str(config.VOICE_CHANNEL_ID)

    async def on_answer(self, interaction: discord.Interaction) -> None:
        self.state = CallState.AWAITING_JOIN
        channel_name = self._voice_channel_name()
        await interaction.response.edit_message(
            content=f"✅ 応答しました。**#{channel_name}** に参加してください。",
            embed=None,
            view=None,
        )

        # 着信はDMで送っているため interaction.guild は常に None になる
        # (DMのInteractionにはギルドコンテキストが無いため)。
        # なので対象サーバーを client.get_guild で直接引いてメンバーの
        # 現在のボイス状態を確認する。
        guild = self.client.get_guild(config.GUILD_ID)
        member = guild.get_member(config.TARGET_USER_ID) if guild else None
        already_there = (
            member is not None
            and member.voice is not None
            and member.voice.channel is not None
            and member.voice.channel.id == config.VOICE_CHANNEL_ID
        )
        print(f"[call] 応答受理。既にボイスチャンネルにいるか: {already_there}")
        if already_there:
            await self._begin_call()

    async def on_decline(self, interaction: discord.Interaction) -> None:
        self.state = CallState.IDLE
        await interaction.response.edit_message(
            content="❌ 通話を拒否しました。", embed=None, view=None
        )

    async def on_ring_timeout(self) -> None:
        if self.state is CallState.RINGING:
            self.state = CallState.IDLE
            print("[call] 応答なし(不在着信)")

    async def on_target_user_joined_channel(self) -> None:
        print(f"[call] 対象ユーザーが通話用チャンネルに参加(現在の状態: {self.state})")
        if self.state is CallState.AWAITING_JOIN:
            await self._begin_call()

    async def on_target_user_left_channel(self) -> None:
        if self.state is CallState.IN_CALL:
            await self.end_call()

    # ------------------------------------------------------------------
    # 通話中
    # ------------------------------------------------------------------
    async def _begin_call(self) -> None:
        print("[call] _begin_call() 開始")
        channel = self.client.get_channel(config.VOICE_CHANNEL_ID)
        if channel is None:
            print("[call] ボイスチャンネルが見つかりません")
            self.state = CallState.IDLE
            return

        try:
            self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as exc:  # noqa: BLE001 - 原因を必ず可視化してIDLEに戻す
            print(f"[call] ボイスチャンネルへの接続に失敗しました: {exc!r}")
            self.state = CallState.IDLE
            return
        self.player = self._make_player()
        self.voice_client.play(self.player)

        assert self.loop is not None
        sink = UtteranceSink(
            config.TARGET_USER_ID,
            self.loop,
            self.handle_utterance,
            turn_detector=self.turn_detector,
        )
        self.voice_client.listen(sink)

        self.history = []
        self.state = CallState.IN_CALL
        print("[call] 通話開始")
        await self._speak(GREETING, time.monotonic())

    async def end_call(self) -> None:
        if self.voice_client is not None:
            self.voice_client.stop()
            await self.voice_client.disconnect(force=True)
        self.voice_client = None
        self.player = None
        self.history = []
        self.state = CallState.IDLE
        print("[call] 通話終了")

    # ------------------------------------------------------------------
    # 会話パイプライン (STT -> LLM(文単位ストリーミング) -> TTS -> 再生)
    # ------------------------------------------------------------------
    async def handle_utterance(self, vad: VadResult) -> None:
        if self.state is not CallState.IN_CALL or self.stt is None or self.llm is None:
            return
        # すべてのレイテンシは「話し終わった瞬間」(speech_end)を起点に測る。
        speech_end = vad.speech_end_mono

        record: TurnRecord | None = None
        if self.metrics is not None:
            self._turn_index += 1
            record = TurnRecord(
                turn_index=self._turn_index,
                utterance_ms=vad.utterance_ms,
                pre_roll_ms=vad.pre_roll_ms,
                ended_at_max=vad.ended_at_max,
            )
            # 発話終了→最初のAI発声のギャップは、実際の再生スレッドから受け取る。
            if self.player is not None:
                self.player.begin_turn(
                    speech_end,
                    lambda gap_ms, r=record: setattr(r, "response_gap_ms", gap_ms),
                )

        text = await asyncio.to_thread(self.stt.transcribe, vad.pcm)
        if record is not None:
            record.stt_ms = (time.monotonic() - speech_end) * 1000
            record.stt_text = text
        if config.LOG_LATENCY:
            print(f"[latency] STT: {time.monotonic() - speech_end:.2f}s -> {text!r}")
        if not text:
            # 無音/幻聴で応答しないターンも、データとして残す(再生は起きないので
            # begin_turn のマーカーを解除してから記録する)。
            if record is not None:
                if self.player is not None:
                    self.player.begin_turn(speech_end, lambda _gap: None)
                self.metrics.write(record)
            return

        self.history.append({"role": "user", "content": text})
        self.history = self.history[-config.HISTORY_TURNS * 2 :]

        assistant_parts: list[str] = []
        first_logged = False

        def _run_llm() -> None:
            nonlocal first_logged
            for sentence in self.llm.stream_sentences(self.history):
                assistant_parts.append(sentence)
                if not first_logged:
                    first_logged = True
                    elapsed = time.monotonic() - speech_end
                    if record is not None:
                        record.llm_first_sentence_ms = elapsed * 1000
                    if config.LOG_LATENCY:
                        print(f"[latency] LLM first sentence: {elapsed:.2f}s -> {sentence!r}")
                asyncio.run_coroutine_threadsafe(
                    self._speak(sentence, speech_end, record), self.loop
                )

        await asyncio.to_thread(_run_llm)
        if assistant_parts:
            self.history.append({"role": "assistant", "content": "".join(assistant_parts)})

        if record is not None:
            # 体感の肝である response_gap_ms は再生スレッドから遅れて届くため、
            # 鳴り始める(最大2s)まで待ってから記録する。
            for _ in range(200):
                if record.response_gap_ms is not None or self.player is None:
                    break
                await asyncio.sleep(0.01)
            record.n_sentences = len(assistant_parts)
            self.metrics.write(record)

    async def _speak(
        self, sentence: str, speech_end: float, record: TurnRecord | None = None
    ) -> None:
        if self.player is None:
            return
        try:
            wav_bytes = await self.tts.synthesize(sentence)
        except TextToSpeechError as exc:
            print(f"[tts] error: {exc}")
            return
        if record is not None and record.tts_first_chunk_ms is None:
            record.tts_first_chunk_ms = (time.monotonic() - speech_end) * 1000
        self.player.push_wav(wav_bytes)
        if config.LOG_LATENCY:
            print(f"[latency] TTS chunk ready: {time.monotonic() - speech_end:.2f}s -> {sentence!r}")
