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
import threading
import time
import wave
from dataclasses import dataclass
from typing import Callable, List, Optional

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


@dataclass
class AudioChunk:
    """再生キューの1要素。playback_epoch を持ち、read() 側で現行epochと
    一致しないチャンク(=barge-in 等で無効化された古い音声)を破棄する。
    text は「実際に再生された範囲だけを会話履歴へ残す」(不変条件6)ための表示テキスト。"""

    epoch: int
    pcm: bytes
    text: str = ""


@dataclass
class InterruptInfo:
    """interrupt() の結果。バンプ後の新しいepochと、破棄した未再生音声の長さ(ms)。"""

    epoch: int
    dropped_audio_ms: float


def _pcm_ms(pcm_bytes: int) -> float:
    """Discord PCM のバイト数を再生時間(ms)へ。20ms = _FRAME_BYTES。"""
    return pcm_bytes / _FRAME_BYTES * 20.0


class QueuedPCMSource(discord.AudioSource):
    """文ごとのPCMデータをキューイングして順番に再生し続けるAudioSource。

    通話開始時に一度 voice_client.play(source) するだけで、以後は
    push_wav() で追加した音声が届いた順に途切れなく再生される。

    ARCHITECTURE_v2_PROPOSAL.md §4.13: 各チャンクに playback_epoch を付け、
    read() は毎フレーム現行epochを確認して古いチャンクを破棄する。barge-in が
    epoch をバンプ(interrupt())すれば、生成が間に合わず後から届いた古いWAVも
    含めて一切鳴らさない。会話履歴は「実際に鳴らしたチャンクのtext」から作る。
    """

    def __init__(
        self,
        ambient_pcm: Optional[bytes] = None,
        idle_gain: float = 0.0,
        duck_gain: float = 0.0,
    ) -> None:
        self._queue: "queue.Queue[AudioChunk]" = queue.Queue()
        self._current: Optional[AudioChunk] = None
        self._offset = 0
        # 現在の再生epoch。barge-in のたびに +1 する。読み書きは int の代入/参照で
        # GIL 下では原子的だが、キュー掃除と整合させるため _lock 内でも触る。
        self._epoch = 0
        self._lock = threading.Lock()
        # このターンで実際に再生を開始したチャンクのtext(=ユーザーに届いた発話)。
        # 途中で barge-in されても、鳴った分だけがここに積まれる。
        self._spoken_texts: List[str] = []
        self._played_chunks = 0

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
        """このターンの応答再生が最初に鳴った瞬間に、発話終了からの経過(ms)を通知する。

        あわせて「このターンで実際に鳴らした発話」の集計をリセットする。"""
        self._turn_start_mono = speech_end_mono
        self._on_playback_start = on_start
        self._spoken_texts = []
        self._played_chunks = 0

    def current_epoch(self) -> int:
        return self._epoch

    def push_wav(self, wav_bytes: bytes, text: str = "", epoch: Optional[int] = None) -> None:
        """合成済みWAVを再生キューへ積む。epoch を省略すると現行epochを付ける。

        生成開始時に捕まえた epoch を明示的に渡すことで、その生成が barge-in で
        無効化された後に届いた遅延チャンクは古いepochを持ち、read() で破棄される。"""
        ep = self._epoch if epoch is None else epoch
        self._queue.put(AudioChunk(ep, _wav_to_discord_pcm(wav_bytes), text))

    def interrupt(self) -> InterruptInfo:
        """再生epochをバンプし、現在再生中のチャンクと未再生キューを全て破棄する。

        戻り値に破棄した未再生音声の長さ(ms)を含める。read() は次フレーム以降、
        古いepochのチャンクを鳴らさず捨てるため、以降 push された遅延WAVも無害化される。"""
        with self._lock:
            dropped = 0
            if self._current is not None:
                dropped += max(0, len(self._current.pcm) - self._offset)
                self._current = None
                self._offset = 0
            while True:
                try:
                    chunk = self._queue.get_nowait()
                except queue.Empty:
                    break
                dropped += len(chunk.pcm)
            self._epoch += 1
            # 割り込みが起きた=このターンの応答はここで確定(以降は鳴らさない)。
            self._turn_start_mono = None
            self._on_playback_start = None
            return InterruptInfo(epoch=self._epoch, dropped_audio_ms=_pcm_ms(dropped))

    def take_spoken_texts(self) -> List[str]:
        """このターンで実際に再生開始したチャンクのtextを取り出してクリアする。"""
        with self._lock:
            spoken = self._spoken_texts
            self._spoken_texts = []
            return spoken

    def played_chunk_count(self) -> int:
        return self._played_chunks

    def is_playing_or_pending(self) -> bool:
        cur = self._current
        return not self._queue.empty() or (cur is not None and self._offset < len(cur.pcm))

    def _next_speech_frame(self) -> Optional[bytes]:
        """次の発話フレーム(20ms)を返す。キューが空なら None。

        現行epochと一致しないチャンクは鳴らさずに読み飛ばす(barge-in で無効化された
        古い音声を漏らさない)。"""
        with self._lock:
            while self._current is None or self._offset >= len(self._current.pcm):
                try:
                    chunk = self._queue.get_nowait()
                except queue.Empty:
                    self._current = None
                    return None
                if chunk.epoch != self._epoch:
                    # 割り込みで無効化された古いチャンク。鳴らさず捨てて次へ。
                    continue
                self._current = chunk
                self._offset = 0
                self._played_chunks += 1
                if chunk.text:
                    self._spoken_texts.append(chunk.text)
                self._report_playback_start()

            pcm = self._current.pcm
            frame = pcm[self._offset : self._offset + _FRAME_BYTES]
            self._offset += _FRAME_BYTES
        if len(frame) < _FRAME_BYTES:
            frame += b"\x00" * (_FRAME_BYTES - len(frame))
        return frame

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
