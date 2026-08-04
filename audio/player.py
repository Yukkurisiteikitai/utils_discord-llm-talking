"""TTSで生成したWAV音声をDiscordのボイスチャンネルへ途切れなく再生するための
カスタムAudioSource。

discord.pyの音声再生スレッドは20msごとに同期的に read() を呼び出すため、
ここでは一切ブロッキングしない(データが無ければ無音フレームを返して
ストリームを維持する = 通話中ずっと同じsourceでplay()し続けられる)。
文ごとのWAVはキューに積んでおき、順番に消費する。
"""
from __future__ import annotations

import io
import queue
import time
import wave
from typing import Callable, Optional

import discord
import numpy as np

import config

_FRAME_BYTES = discord.opus.Encoder.FRAME_SIZE  # 20ms分 (48kHz/stereo/16bit) = 3840 bytes
_SILENCE_FRAME = b"\x00" * _FRAME_BYTES


def _wav_to_discord_pcm(wav_bytes: bytes) -> bytes:
    """任意のサンプルレート/チャンネル数のWAVを、Discordが要求する
    48kHz・ステレオ・16bit PCMに変換する。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)

    if rate != config.DISCORD_SAMPLE_RATE and len(samples) > 0:
        duration = len(samples) / rate
        target_len = max(1, int(round(duration * config.DISCORD_SAMPLE_RATE)))
        src_x = np.linspace(0, 1, num=len(samples), endpoint=False)
        dst_x = np.linspace(0, 1, num=target_len, endpoint=False)
        samples = np.interp(dst_x, src_x, samples)

    mono = np.clip(samples, -32768, 32767).astype(np.int16)
    stereo = np.repeat(mono, config.DISCORD_CHANNELS)
    return stereo.tobytes()


class QueuedPCMSource(discord.AudioSource):
    """文ごとのPCMデータをキューイングして順番に再生し続けるAudioSource。

    通話開始時に一度 voice_client.play(source) するだけで、以後は
    push_wav() で追加した音声が届いた順に途切れなく再生される。
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._current = b""
        self._offset = 0
        # このターンの応答が最初に鳴った瞬間を測るためのマーカー。
        # begin_turn() で発話終了時刻をセットし、最初の実フレーム再生時に
        # 経過(ms)をコールバックへ渡して1回だけ発火する(体感レイテンシ=間の指標)。
        self._turn_start_mono: Optional[float] = None
        self._on_playback_start: Optional[Callable[[float], None]] = None

    def begin_turn(
        self, speech_end_mono: float, on_start: Callable[[float], None]
    ) -> None:
        """このターンの応答再生が最初に鳴った瞬間に、発話終了からの経過(ms)を通知する。"""
        self._turn_start_mono = speech_end_mono
        self._on_playback_start = on_start

    def push_wav(self, wav_bytes: bytes) -> None:
        self._queue.put(_wav_to_discord_pcm(wav_bytes))

    def is_playing_or_pending(self) -> bool:
        return not self._queue.empty() or self._offset < len(self._current)

    def read(self) -> bytes:
        while self._offset >= len(self._current):
            try:
                self._current = self._queue.get_nowait()
                self._offset = 0
                self._report_playback_start()
            except queue.Empty:
                return _SILENCE_FRAME

        chunk = self._current[self._offset : self._offset + _FRAME_BYTES]
        self._offset += _FRAME_BYTES
        if len(chunk) < _FRAME_BYTES:
            chunk += b"\x00" * (_FRAME_BYTES - len(chunk))
        return chunk

    def _report_playback_start(self) -> None:
        """このターンで初めて実データを再生に載せた瞬間に1回だけ発火する。"""
        if self._turn_start_mono is None:
            return
        gap_ms = (time.monotonic() - self._turn_start_mono) * 1000
        callback = self._on_playback_start
        self._turn_start_mono = None
        self._on_playback_start = None
        if callback is not None:
            callback(gap_ms)

    def is_opus(self) -> bool:
        return False
