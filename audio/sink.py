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
from typing import Awaitable, Callable

import numpy as np
from discord.ext import voice_recv
from silero_vad import VADIterator, load_silero_vad

import config

OnUtterance = Callable[[np.ndarray], Awaitable[None]]


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


class UtteranceSink(voice_recv.AudioSink):
    """指定した1人のユーザーの発話だけを拾い、発話が終わるたびにコールバックする。"""

    def __init__(
        self,
        target_user_id: int,
        loop: asyncio.AbstractEventLoop,
        on_utterance: OnUtterance,
    ) -> None:
        super().__init__()
        self._target_user_id = target_user_id
        self._loop = loop
        self._on_utterance = on_utterance
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
                state.in_speech = True
                print("[sink] VAD: 発話開始を検知")

            if state.in_speech:
                state.utterance = np.concatenate([state.utterance, frame])

            max_samples = config.VAD_MAX_UTTERANCE_MS / 1000 * config.VAD_SAMPLE_RATE
            if event is not None and "end" in event and state.in_speech:
                self._finish_utterance()
            elif state.in_speech and len(state.utterance) >= max_samples:
                self._finish_utterance()

    def _finish_utterance(self) -> None:
        state = self._state
        utterance = state.utterance
        state.utterance = np.zeros(0, dtype=np.float32)
        state.in_speech = False
        state.vad_iterator.reset_states()

        min_samples = config.VAD_MIN_SPEECH_MS / 1000 * config.VAD_SAMPLE_RATE
        duration_ms = len(utterance) / config.VAD_SAMPLE_RATE * 1000
        if len(utterance) < min_samples:
            print(f"[sink] VAD: 発話終了だが短すぎるため破棄 ({duration_ms:.0f}ms)")
            return

        print(f"[sink] VAD: 発話終了を検知 ({duration_ms:.0f}ms) -> STTへ")
        asyncio.run_coroutine_threadsafe(self._on_utterance(utterance), self._loop)

    def cleanup(self) -> None:
        pass
