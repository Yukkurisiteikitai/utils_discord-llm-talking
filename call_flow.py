"""着信(DM Embed + ボタン)から通話終了までの状態管理と会話パイプラインの結線。"""
from __future__ import annotations

import asyncio
import enum
import re
import time

import discord
import numpy as np
from discord.ext import voice_recv

import config
from audio.player import QueuedPCMSource, _generate_ambient_pcm
from audio.sink import UtteranceSink, VadResult
from debug_log import DebugChannelLogger
from pipeline.llm import LanguageModel
from pipeline.metrics import TurnLogger, TurnRecord
from pipeline.stt import SpeechToText, looks_like_hallucination
from pipeline.tts import TextToSpeech, TextToSpeechError
from turn.barge_in import BargeInController
from turn.cancellation import CancellationToken, next_generation_id, next_turn_id

GREETING = "もしもし、聞こえる?"

# 応答ループ検知用: 記号・空白を落として本文だけで同一性を見る。
_NORM_RE = re.compile(r"[\s、。!?！？…「」『』]")


def _norm_text(text: str) -> str:
    return _NORM_RE.sub("", text)


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
        self.sink: UtteranceSink | None = None
        # AI再生中のユーザー発話を割り込みとして扱う調停役(通話ごとに作り直す)。
        self.barge_in: BargeInController | None = None
        # 直近の barge-in で破棄した未再生音声の長さ(ms)。割り込まれたターンの
        # metrics に載せるため、確認窓ハンドラから handle_utterance へ受け渡す。
        self._last_dropped_audio_ms: float = 0.0
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
        # 通話イベント(TTS発話内容/STT結果/割り込み/レイテンシ)をDiscordチャンネルへ
        # 流すデバッグロガー。/debug コマンドで有効化・出力先設定する。
        self.debug = DebugChannelLogger(client)

        # 単一MLXレーン: STT/LLM は 8GB/M3 の GPU・統合メモリを共有するため、複数を
        # 同時に走らせると相互に激遅化してレイテンシが数十秒〜数分に膨れ、応答が
        # 返らなくなる。このロックで MLX 重処理を常に1つずつに直列化する
        # (ARCHITECTURE_v2_PROPOSAL.md §5.2 MLX_CONCURRENCY=1)。
        self._mlx_lock = asyncio.Lock()
        # 発話ごとの連番。過負荷でバックログが溜まったとき、レーンを取れた時点で
        # 自分より新しい発話が来ていれば古い方を破棄する(latest-wins / §5.4)。
        self._utterance_seq = 0
        # 応答ループ検知: 直前のアシスタント応答(正規化)と連続一致回数。ノイズ入力で
        # 履歴が汚染され小型モデルが同じ返答を繰り返すアトラクタに落ちたとき、履歴を
        # リセットして抜け出す。
        self._last_assistant_norm = ""
        self._assistant_repeat = 0

    async def load_models(self) -> None:
        self.loop = asyncio.get_running_loop()
        # デバッグログの flush ループを起動(loop が確定してから)。
        self.debug.start(self.loop)
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
        self.barge_in = BargeInController(self.player)
        self._last_dropped_audio_ms = 0.0

        assert self.loop is not None
        sink = UtteranceSink(
            config.TARGET_USER_ID,
            self.loop,
            self.handle_utterance,
            turn_detector=self.turn_detector,
            on_speech_start=self._on_user_speech_start if config.BARGE_IN_ENABLED else None,
        )
        self.sink = sink
        self.voice_client.listen(sink)

        self.history = []
        self._last_assistant_norm = ""
        self._assistant_repeat = 0
        self.state = CallState.IN_CALL
        print("[call] 通話開始")
        self.debug.emit("📞 **通話開始**")
        # 挨拶の合成・再生と並行してモデルを温め直す。8GB機ではロード〜着信までの
        # アイドルで重みが退避され、初回発話のSTT/LLMが数秒遅くなる(実測: 90sアイドルで
        # STT 2.2s、通話ではさらに悪化して7〜14s)。挨拶TTSの裏で空撃ちして、最初の
        # ユーザー発話を温かい状態で迎える。
        # 挨拶も barge-in 対象にする(ユーザーが挨拶に被せて話し始めたら止める)。
        greet_token = CancellationToken()
        if self.barge_in is not None:
            self.barge_in.set_active(greet_token)
        greet_epoch = self.player.current_epoch() if self.player is not None else 0
        await asyncio.gather(
            self._speak(GREETING, time.monotonic(), greet_epoch, greet_token),
            asyncio.to_thread(self._warm_up_models),
        )
        if self.barge_in is not None:
            self.barge_in.clear_active(greet_token)

    def _warm_up_models(self) -> None:
        """通話開始時にSTT/LLM/Smart Turnを空撃ちして温め直す(ブロッキング)。
        失敗しても通話は続行する(温め直しは最適化であって必須ではない)。"""
        try:
            if self.stt is not None:
                self.stt.warm_up()
            if self.llm is not None:
                self.llm.warm_up()
            if self.turn_detector is not None:
                self.turn_detector.warmup()
        except Exception as e:  # noqa: BLE001
            print(f"[call] 通話前ウォームアップに失敗(続行します): {e}")

    async def end_call(self) -> None:
        if self.voice_client is not None:
            self.voice_client.stop()
            await self.voice_client.disconnect(force=True)
        self.voice_client = None
        self.player = None
        self.sink = None
        self.barge_in = None
        self.history = []
        self.state = CallState.IDLE
        print("[call] 通話終了")
        self.debug.emit("📴 **通話終了**")

    # ------------------------------------------------------------------
    # 会話パイプライン (STT -> LLM(文単位ストリーミング) -> TTS -> 再生)
    # ------------------------------------------------------------------
    async def handle_utterance(self, vad: VadResult) -> None:
        if self.state is not CallState.IN_CALL or self.stt is None or self.llm is None:
            return

        # 発話ごとの連番。過負荷でこの発話がレーン待ちしている間に、より新しい発話が
        # 来ることがある。その場合この古い発話は捨てる(latest-wins / §5.4)。
        self._utterance_seq += 1
        my_seq = self._utterance_seq

        # STT/LLM(MLX)は必ず1つずつ。複数同時だと 8GB/M3 で相互に激遅化し、
        # レイテンシが数十秒〜数分に膨れて「応答しなくなる」(§5.2)。TTS(HTTP)と
        # 再生待ちはMLXではないが、履歴の整合(直列なターン順)を保つため、この
        # ターンが鳴らし終える(または barge-in で中断する)までレーンを保持する。
        async with self._mlx_lock:
            if my_seq != self._utterance_seq:
                # 待っている間に新しい発話が到着。積み上がった古い発話は処理しない。
                print(
                    f"[turn] 過負荷のため古い発話を破棄 (seq={my_seq} / 最新={self._utterance_seq})"
                )
                return
            await self._process_utterance(vad, my_seq)

    async def _process_utterance(self, vad: VadResult, my_seq: int) -> None:
        """1発話の STT→LLM→TTS→再生確定を、MLXレーンを保持したまま実行する。
        呼び出し側(handle_utterance)が _mlx_lock を保持している前提。"""
        # すべてのレイテンシは「話し終わった瞬間」(speech_end)を起点に測る。
        speech_end = vad.speech_end_mono

        # このターンの ID 群と、生成をキャンセルするためのトークン。生成物(WAV)には
        # 生成開始時点の playback_epoch を付け、barge-in で epoch がバンプされた後に
        # 遅れて届いた音声は再生させない(ARCHITECTURE_v2_PROPOSAL.md §3.3 / §14)。
        turn_id = next_turn_id()
        generation_id = next_generation_id()
        cancel_token = CancellationToken()
        gen_epoch = self.player.current_epoch() if self.player is not None else 0
        if self.barge_in is not None:
            self.barge_in.set_active(cancel_token)
        self._last_dropped_audio_ms = 0.0

        record: TurnRecord | None = None
        if self.metrics is not None:
            self._turn_index += 1
            record = TurnRecord(
                turn_index=self._turn_index,
                turn_id=turn_id,
                generation_id=generation_id,
                playback_epoch=gen_epoch,
                utterance_ms=vad.utterance_ms,
                pre_roll_ms=vad.pre_roll_ms,
                ended_at_max=vad.ended_at_max,
                smart_turn_prob=vad.smart_turn_prob,
                continuation_count=vad.continuation_count,
            )
            # 発話終了→最初のAI発声のギャップは、実際の再生スレッドから受け取る。
            if self.player is not None:
                self.player.begin_turn(
                    speech_end,
                    lambda gap_ms, r=record: setattr(r, "response_gap_ms", gap_ms),
                )

        text = await asyncio.to_thread(self.stt.transcribe, vad.pcm)
        stt_ms = (time.monotonic() - speech_end) * 1000
        # 音楽/ノイズに対する Whisper の反復幻聴(「健健健…」「Jazz Jazz…」等)は、
        # 意味の無い応答と無駄なLLM/TTSを生むだけなので、無音と同じく破棄する。
        hallucinated = looks_like_hallucination(text)
        if record is not None:
            record.stt_ms = stt_ms
            record.stt_text = text
        if config.LOG_LATENCY:
            tag = " [幻聴とみなし破棄]" if hallucinated else ""
            print(f"[latency] STT: {stt_ms / 1000:.2f}s -> {text!r}{tag}")
        if not text or hallucinated:
            self.debug.emit(
                (f"🚮 ノイズ/幻聴とみなし破棄: {text}" if hallucinated
                 else f"🈳 無音/破棄(応答なし)")
                + f"  ·STT {stt_ms:.0f}ms"
            )
            # 応答しないターンも、データとして残す(再生は起きないので begin_turn の
            # マーカーを解除してから記録する)。
            if self.barge_in is not None:
                self.barge_in.clear_active(cancel_token)
            if record is not None:
                if self.player is not None:
                    self.player.begin_turn(speech_end, lambda _gap: None)
                self.metrics.write(record)
            return

        self.debug.emit(f"👤 {text}  ·STT {stt_ms:.0f}ms")
        self.history.append({"role": "user", "content": text})
        self.history = self.history[-config.HISTORY_TURNS * 2 :]

        assistant_parts: list[str] = []
        first_logged = False

        def _run_llm() -> None:
            nonlocal first_logged
            for sentence in self.llm.stream_sentences(self.history, cancel_token):
                assistant_parts.append(sentence)
                if not first_logged:
                    first_logged = True
                    elapsed = time.monotonic() - speech_end
                    if record is not None:
                        record.llm_first_sentence_ms = elapsed * 1000
                    if config.LOG_LATENCY:
                        print(f"[latency] LLM first sentence: {elapsed:.2f}s -> {sentence!r}")
                asyncio.run_coroutine_threadsafe(
                    self._speak(sentence, speech_end, gen_epoch, cancel_token, record),
                    self.loop,
                )

        await asyncio.to_thread(_run_llm)

        # 実際に鳴らした範囲だけを会話履歴へ残す(不変条件6)。再生はLLM生成より遅れて
        # 進むため、キューが枯れる(=鳴らし終わる)か barge-in で flush されるまで待つ。
        # ここまでレーンを保持しているので、次のターンのSTTは割り込まない(履歴が整合)。
        await self._wait_playback_settle(cancel_token)
        if self.barge_in is not None:
            self.barge_in.clear_active(cancel_token)

        interrupted = cancel_token.cancelled
        spoken = self.player.take_spoken_texts() if self.player is not None else []
        if spoken:
            content = "".join(spoken)
            if interrupted:
                content += "…"  # 途中で遮られた(未完)ことを履歴上でも示す
            self.history.append({"role": "assistant", "content": content})

            # 応答ループ検知: ノイズ入力で履歴が汚染されると、小型モデルが同じ返答を
            # 繰り返すアトラクタに落ちる(実機で「話が飛んだよ」等の反復を確認)。
            # 直前と同じ応答が続いたら、履歴をリセットしてループから抜け出す。
            norm = _norm_text(content)
            if norm and norm == self._last_assistant_norm:
                self._assistant_repeat += 1
            else:
                self._assistant_repeat = 0
                self._last_assistant_norm = norm
            if self._assistant_repeat >= 2:
                print("[turn] 同じ応答の繰り返しを検知 -> 会話履歴をリセットします")
                self.debug.emit("🔁 応答ループを検知 → 会話履歴をリセット")
                self.history = []
                self._assistant_repeat = 0
                self._last_assistant_norm = ""

        if record is not None:
            record.n_sentences = len(assistant_parts)
            record.interrupted = interrupted
            record.played_chunks = (
                self.player.played_chunk_count() if self.player is not None else 0
            )
            record.dropped_audio_ms = self._last_dropped_audio_ms
            self.metrics.write(record)

            gap = (
                f"{record.response_gap_ms:.0f}ms"
                if record.response_gap_ms is not None
                else "—"
            )
            llm = (
                f"{record.llm_first_sentence_ms:.0f}ms"
                if record.llm_first_sentence_ms is not None
                else "—"
            )
            flag = " ✂️割り込み" if interrupted else ""
            self.debug.emit(
                f"⏱️ 応答ギャップ {gap} ·LLM初文 {llm} ·{record.n_sentences}文{flag}"
            )

    async def _wait_playback_settle(self, cancel_token: CancellationToken) -> None:
        """このターンの再生キューが枯れる(鳴らし終わる)まで待つ。

        barge-in が起きると player.interrupt() がキューを flush するので、待ちは
        すぐ抜ける。安全のため上限(30s)を設け、無限待ちにはしない。"""
        if self.player is None:
            return
        for _ in range(3000):  # 10ms × 3000 = 30s 上限
            if cancel_token.cancelled or not self.player.is_playing_or_pending():
                return
            await asyncio.sleep(0.01)

    async def _on_user_speech_start(self) -> None:
        """AI再生中にユーザーが話し始めたときの割り込み確定処理(確認窓つき)。

        VADの発話開始検知ごとに受信スレッドから呼ばれる。AIが鳴っていなければ何も
        しない。短い確認窓のあいだ発話が続いていれば割り込みを確定し、未再生音声の
        破棄と進行中生成のキャンセルを行う(ARCHITECTURE_v2_PROPOSAL.md §4.14)。"""
        if not config.BARGE_IN_ENABLED or self.player is None or self.barge_in is None:
            return
        if not self.player.is_playing_or_pending():
            return  # AIが喋っていない=割り込みではない(通常のユーザーターン)

        detected_mono = time.monotonic()
        # ノイズ(咳・机音)で止めないための確認窓。まだ喋っていれば割り込み確定。
        await asyncio.sleep(config.BARGE_IN_MIN_SPEECH_MS / 1000)
        if self.sink is not None and not self.sink.is_in_speech():
            return  # 短いノイズだった。AI音声は止めない。
        if not self.player.is_playing_or_pending():
            return  # 確認窓の間にAIが自然に鳴り終わった。

        info = self.barge_in.trigger()
        if info is None:
            return
        self._last_dropped_audio_ms = info.dropped_audio_ms
        stop_ms = (time.monotonic() - detected_mono) * 1000
        print(
            f"[barge-in] ユーザー割り込みを検知 -> 再生停止 "
            f"(未再生 {info.dropped_audio_ms:.0f}ms 破棄, 停止まで {stop_ms:.0f}ms)"
        )
        self.debug.emit(
            f"✂️ **割り込み** — 未再生{info.dropped_audio_ms:.0f}ms破棄 / 停止{stop_ms:.0f}ms"
        )

    async def _speak(
        self,
        sentence: str,
        speech_end: float,
        epoch: int | None = None,
        cancel_token: CancellationToken | None = None,
        record: TurnRecord | None = None,
    ) -> None:
        if self.player is None:
            return
        # 生成開始時の epoch を明示指定しない場合(挨拶など単発発話)は push 時点の
        # 現行 epoch を使う。cancel_token 未指定なら barge-in 監視なしで単純に鳴らす。
        if epoch is None:
            epoch = self.player.current_epoch()
        if cancel_token is not None and cancel_token.cancelled:
            # 既に barge-in 済み。TTSのHTTP往復を無駄に投げない。
            return
        try:
            wav_bytes = await self.tts.synthesize(sentence)
        except TextToSpeechError as exc:
            print(f"[tts] error: {exc}")
            self.debug.emit(f"⚠️ TTS失敗: {exc}")
            return
        if cancel_token is not None and cancel_token.cancelled:
            # 合成中に barge-in された。押し込んでも epoch 不一致で鳴らないが、無駄なので捨てる。
            self.debug.emit(f"🚫 割り込みで破棄(未発声): {sentence}")
            return
        tts_ms = (time.monotonic() - speech_end) * 1000
        if record is not None and record.tts_first_chunk_ms is None:
            record.tts_first_chunk_ms = tts_ms
        # 生成開始時点の epoch を付けて push。以降 barge-in で epoch がバンプされたら
        # このチャンクは read() 側で破棄される。text は再生済み判定=履歴用に持たせる。
        self.player.push_wav(wav_bytes, text=sentence, epoch=epoch)
        # ★ TTSが実際に喋る中身。デバッグチャンネルの主役。
        self.debug.emit(f"🔊 {sentence}  ·TTS {tts_ms:.0f}ms")
        if config.LOG_LATENCY:
            print(f"[latency] TTS chunk ready: {time.monotonic() - speech_end:.2f}s -> {sentence!r}")
