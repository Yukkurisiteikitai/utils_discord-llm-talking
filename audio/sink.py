"""discord-ext-voice-recv を使った、リアルタイム音声受信とSilero VADによる
発話区間検出。

write() はライブラリ内部の受信スレッド(asyncioループとは別スレッド)から
同期的に呼ばれる。ここでは重い処理を一切行わず、VAD判定と発話バッファへの
追記のみを行い、発話が終わったと判定した瞬間に
asyncio.run_coroutine_threadsafe でメインのイベントループにコールバックを
投げてSTT以降のパイプラインを起動する。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy as np
from discord.ext import voice_recv
from silero_vad import VADIterator, load_silero_vad

import config


@dataclass
class VadResult:
    """1発話ぶんのPCMと、その発話区間のVADメタ情報。

    speech_start_mono / speech_end_mono は time.monotonic() 基準。呼び出し側が
    「話し終わってから何msで応答が出たか」を測る起点として speech_end_mono を使う。
    """

    pcm: np.ndarray
    speech_start_mono: float
    speech_end_mono: float
    utterance_ms: float
    pre_roll_ms: float
    ended_at_max: bool
    # Smart Turn の判定値(OFF/未実行なら prob=None, count=0)。閾値チューニング用。
    smart_turn_prob: float | None = None
    continuation_count: int = 0


OnUtterance = Callable[[VadResult], Awaitable[None]]


def _resample_to_16k_mono(pcm_bytes: bytes) -> np.ndarray:
    """Discordの48kHzステレオ16bit PCMを16kHzモノラルfloat32([-1, 1])に変換する。

    48000 -> 16000 は正確に3:1のため、素朴な間引き平均で十分な品質が出る
    (音楽的忠実性ではなく、VAD/音声認識用途のため)。
    """
    stereo = np.frombuffer(pcm_bytes, dtype=np.int16).reshape(-1, config.DISCORD_CHANNELS)
    mono = stereo.astype(np.float32).mean(axis=1)
    trim = len(mono) - (len(mono) % 3)
    mono = mono[:trim].reshape(-1, 3).mean(axis=1)
    return (mono / 32768.0).astype(np.float32)


class _UserState:
    def __init__(self) -> None:
        self.vad_iterator = VADIterator(
            load_silero_vad(onnx=True),
            threshold=config.VAD_THRESHOLD,
            sampling_rate=config.VAD_SAMPLE_RATE,
            min_silence_duration_ms=config.VAD_MIN_SILENCE_MS,
        )
        self.pending = np.zeros(0, dtype=np.float32)  # VADチャンク境界待ちの端数
        self.utterance = np.zeros(0, dtype=np.float32)  # 発話中の蓄積バッファ
        self.in_speech = False
        self.frames_received = 0  # 診断用: write()が実際に呼ばれているか確認する

        # pre-roll: 未発話中の直近フレームを溜めておくリング。発話開始が確定した
        # 瞬間に発話バッファの先頭へ移して、語頭欠落を防ぐ(間の自然さ)。
        self.pre_roll_capacity = int(
            config.VAD_PRE_ROLL_MS / 1000 * config.VAD_SAMPLE_RATE
        )
        self.pre_roll = np.zeros(0, dtype=np.float32)
        self.speech_start_mono = 0.0  # 発話開始を検知した time.monotonic()
        self.pre_roll_ms = 0.0        # 実際に付け足した pre-roll の長さ
        # Smart Turn: 沈黙で「終了」と出たがまだ続くと判定した継続待ち状態。
        # in_speech は True のまま維持し、後続フレームを同一発話へ連結する。
        self.awaiting_continuation = False
        # Smart Turn 計装: 今の発話での「まだ続く」延長回数と、最後に得た完了確率。
        # 発話確定ごとに VadResult へ載せてリセットする(次ターンに漏らさない)。
        self.continuation_count = 0
        self.last_smart_turn_prob: float | None = None


class UtteranceSink(voice_recv.AudioSink):
    """指定した1人のユーザーの発話だけを拾い、発話が終わるたびにコールバックする。"""

    def __init__(
        self,
        target_user_id: int,
        loop: asyncio.AbstractEventLoop,
        on_utterance: OnUtterance,
        turn_detector=None,
    ) -> None:
        super().__init__()
        self._target_user_id = target_user_id
        self._loop = loop
        self._on_utterance = on_utterance
        # None のとき Smart Turn は完全に無効(=従来の沈黙タイマー方式のまま)。
        self._turn_detector = turn_detector
        self._state = _UserState()

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None or user.id != self._target_user_id:
            return

        state = self._state
        state.frames_received += 1
        if state.frames_received == 1:
            print("[sink] 最初の音声フレームを受信しました(受信は生きています)")
        elif state.frames_received % 250 == 0:
            # 20ms/フレームなので250フレーム = 約5秒ごとの生存確認ログ。
            print(f"[sink] 音声受信中... (累計{state.frames_received}フレーム)")

        chunk = _resample_to_16k_mono(bytes(data.pcm))
        self._feed(chunk)

    def _feed(self, chunk: np.ndarray) -> None:
        state = self._state
        state.pending = np.concatenate([state.pending, chunk])

        while len(state.pending) >= config.VAD_CHUNK_SAMPLES:
            frame = state.pending[: config.VAD_CHUNK_SAMPLES]
            state.pending = state.pending[config.VAD_CHUNK_SAMPLES :]

            event = state.vad_iterator(frame, return_seconds=False)

            if event is not None and "start" in event:
                if state.awaiting_continuation:
                    # Smart Turnで「まだ続く」と判定して継続待ちだった。溜めてある
                    # 発話をクロバーせず、同一ターンの続きとして扱う(息継ぎ後の再開)。
                    state.awaiting_continuation = False
                    print("[sink] Smart Turn: 継続発話を再開(同一ターンに連結)")
                else:
                    state.in_speech = True
                    state.speech_start_mono = time.monotonic()
                    # 確定直前までのpre-rollを発話先頭に付けて語頭欠落を防ぐ。
                    state.utterance = state.pre_roll.copy()
                    state.pre_roll_ms = (
                        len(state.pre_roll) / config.VAD_SAMPLE_RATE * 1000
                    )
                    print("[sink] VAD: 発話開始を検知")

            if state.in_speech:
                state.utterance = np.concatenate([state.utterance, frame])
            elif state.pre_roll_capacity > 0:
                # 未発話中はpre-rollリングに溜める(容量を超えた古い分は捨てる)。
                state.pre_roll = np.concatenate([state.pre_roll, frame])[
                    -state.pre_roll_capacity :
                ]

            max_samples = config.VAD_MAX_UTTERANCE_MS / 1000 * config.VAD_SAMPLE_RATE
            st_max_samples = config.SMART_TURN_MAX_MS / 1000 * config.VAD_SAMPLE_RATE
            if event is not None and "end" in event and state.in_speech:
                if self._should_continue(state):
                    # 区切らずに継続。in_speech は True のまま維持して後続を連結する。
                    state.awaiting_continuation = True
                else:
                    self._finish_utterance(ended_at_max=False)
            elif state.in_speech and len(state.utterance) >= max_samples:
                self._finish_utterance(ended_at_max=True)
            elif state.awaiting_continuation and len(state.utterance) >= st_max_samples:
                # Smart Turnが誤って「未完了」を出し続けても、ここで強制的に区切る
                # (継続待ちが VAD_MAX まで伸びてハングするのを防ぐ短めの安全弁)。
                self._finish_utterance(ended_at_max=True)

    def _should_continue(self, state: _UserState) -> bool:
        """沈黙で終了判定が出た瞬間に、Smart Turnで「まだ続くか」を確認する。

        Smart Turn無効(detector=None)なら常にFalse=従来通り即区切る。
        推論失敗時も安全側に倒してFalse(沈黙方式で区切る)。
        """
        if self._turn_detector is None:
            return False
        st_max_samples = config.SMART_TURN_MAX_MS / 1000 * config.VAD_SAMPLE_RATE
        if len(state.utterance) >= st_max_samples:
            return False
        try:
            is_complete, prob = self._turn_detector.detect(state.utterance)
        except Exception as e:  # noqa: BLE001
            print(f"[sink] Smart Turn 推論失敗、沈黙方式で区切ります: {e}")
            return False
        state.last_smart_turn_prob = prob
        if is_complete:
            return False
        state.continuation_count += 1
        print(f"[sink] Smart Turn: まだ続くと判定 (prob={prob:.2f}) -> 継続待ち")
        return True

    def _finish_utterance(self, ended_at_max: bool) -> None:
        state = self._state
        utterance = state.utterance
        speech_start_mono = state.speech_start_mono
        pre_roll_ms = state.pre_roll_ms
        smart_turn_prob = state.last_smart_turn_prob
        continuation_count = state.continuation_count
        speech_end_mono = time.monotonic()

        state.utterance = np.zeros(0, dtype=np.float32)
        state.in_speech = False
        state.awaiting_continuation = False
        state.pre_roll = np.zeros(0, dtype=np.float32)
        state.pre_roll_ms = 0.0
        state.continuation_count = 0
        state.last_smart_turn_prob = None
        state.vad_iterator.reset_states()

        min_samples = config.VAD_MIN_SPEECH_MS / 1000 * config.VAD_SAMPLE_RATE
        duration_ms = len(utterance) / config.VAD_SAMPLE_RATE * 1000
        if len(utterance) < min_samples:
            print(f"[sink] VAD: 発話終了だが短すぎるため破棄 ({duration_ms:.0f}ms)")
            return

        print(f"[sink] VAD: 発話終了を検知 ({duration_ms:.0f}ms) -> STTへ")
        result = VadResult(
            pcm=utterance,
            speech_start_mono=speech_start_mono,
            speech_end_mono=speech_end_mono,
            utterance_ms=duration_ms,
            pre_roll_ms=pre_roll_ms,
            ended_at_max=ended_at_max,
            smart_turn_prob=smart_turn_prob,
            continuation_count=continuation_count,
        )
        asyncio.run_coroutine_threadsafe(self._on_utterance(result), self._loop)

    def cleanup(self) -> None:
        pass
