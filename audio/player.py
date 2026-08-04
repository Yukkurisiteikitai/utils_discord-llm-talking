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
_FRAME_SAMPLES = _FRAME_BYTES // 2  # int16換算のサンプル数(ステレオ interleave 込み)
_SILENCE_FRAME = b"\x00" * _FRAME_BYTES


def _generate_ambient_pcm(seconds: float = 3.0) -> bytes:
    """ファイル未指定時に使う、ごく低音のソフトなアンビエント(空調のような
    ホワイトノイズを平滑化した音)を生成する。48kHz/ステレオ/16bit・振幅控えめ。

    実際の音量は再生時の idle/duck ゲインでさらに絞るので、ここでは
    そこそこの基準振幅(±約6000)で作っておく。
    """
    n = int(config.DISCORD_SAMPLE_RATE * seconds)
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(n).astype(np.float32)
    # 簡易ローパス(移動平均)でシューッとした耳障りな高域を落とす。
    k = 64
    kernel = np.ones(k, dtype=np.float32) / k
    smooth = np.convolve(noise, kernel, mode="same")
    smooth /= (np.max(np.abs(smooth)) or 1.0)
    # ループ境界をなめらかにするため両端をフェード。
    fade = np.linspace(0.0, 1.0, num=min(2400, n // 2), dtype=np.float32)
    smooth[: len(fade)] *= fade
    smooth[-len(fade) :] *= fade[::-1]
    mono = (smooth * 6000).astype(np.int16)
    stereo = np.repeat(mono, config.DISCORD_CHANNELS)
    return stereo.tobytes()


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

    def __init__(
        self,
        ambient_pcm: Optional[bytes] = None,
        idle_gain: float = 0.0,
        duck_gain: float = 0.0,
    ) -> None:
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._current = b""
        self._offset = 0
        # このターンの応答が最初に鳴った瞬間を測るためのマーカー。
        # begin_turn() で発話終了時刻をセットし、最初の実フレーム再生時に
        # 経過(ms)をコールバックへ渡して1回だけ発火する(体感レイテンシ=間の指標)。
        self._turn_start_mono: Optional[float] = None
        self._on_playback_start: Optional[Callable[[float], None]] = None

        # アンビエント(常時鳴る低音の"間"の音)+ ダッキング。ambient_pcm が None の
        # ときは完全に無効で、read() は従来通り「発話 or 無音フレーム」を返す。
        # Hermes Agent(MIT)の voice_mixer 方式(1本の連続sourceに子を足し引き)を、
        # このプロジェクトの単一sourceに合わせて最小構成で取り込んだもの。
        self._ambient = (
            np.frombuffer(ambient_pcm, dtype=np.int16).astype(np.int32)
            if ambient_pcm
            else None
        )
        self._ambient_pos = 0
        self._idle_gain = idle_gain  # 発話していないとき(idle)のアンビエント音量
        self._duck_gain = duck_gain  # 発話中にアンビエントを絞る(ダッキング)音量

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

    def _next_speech_frame(self) -> Optional[bytes]:
        """次の発話フレーム(20ms)を返す。キューが空なら None。"""
        while self._offset >= len(self._current):
            try:
                self._current = self._queue.get_nowait()
                self._offset = 0
                self._report_playback_start()
            except queue.Empty:
                return None

        chunk = self._current[self._offset : self._offset + _FRAME_BYTES]
        self._offset += _FRAME_BYTES
        if len(chunk) < _FRAME_BYTES:
            chunk += b"\x00" * (_FRAME_BYTES - len(chunk))
        return chunk

    def _next_ambient_frame(self) -> np.ndarray:
        """アンビエントを20ms分、ループしながら int32 配列で返す。"""
        amb = self._ambient
        assert amb is not None
        pos = self._ambient_pos
        end = pos + _FRAME_SAMPLES
        if end <= len(amb):
            frame = amb[pos:end]
            self._ambient_pos = end % len(amb)
        else:  # ループ境界をまたぐ
            frame = np.concatenate([amb[pos:], amb[: end - len(amb)]])
            self._ambient_pos = end - len(amb)
        return frame

    def read(self) -> bytes:
        speech = self._next_speech_frame()

        # アンビエント無効(既定): 従来と完全に同じ挙動。
        if self._ambient is None:
            return speech if speech is not None else _SILENCE_FRAME

        ambient = self._next_ambient_frame()
        if speech is not None:
            # 発話中はアンビエントを duck_gain まで絞って発話の下に薄く敷く。
            speech_i32 = np.frombuffer(speech, dtype=np.int16).astype(np.int32)
            mixed = speech_i32 + (ambient * self._duck_gain).astype(np.int32)
        else:
            # idle 中はアンビエントのみを idle_gain で鳴らし、"間"を生かす。
            mixed = (ambient * self._idle_gain).astype(np.int32)
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()

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
