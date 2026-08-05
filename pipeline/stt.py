"""MLX Whisperによるローカル音声認識。

mlx_whisper.transcribe() は内部の ModelHolder が読み込み済みモデルを
プロセス内にキャッシュするため、同じ path_or_hf_repo で呼び続ける限り
2回目以降はディスク読み込みが発生しない。__init__ で無音データを1回
文字起こしすることで、実際の通話が始まる前にモデルをメモリに常駐させる
(通話中の初回発話でロード待ちが発生しないようにするため)。
"""
from __future__ import annotations

import time
from collections import Counter

import mlx_whisper
import numpy as np

import config


def looks_like_hallucination(text: str) -> bool:
    """音楽/ノイズに対する Whisper の暴走(同じ文字・短パターンの大量反復)を検出する。

    「健健健健…」「ううう…」「Jazz Jazz Jazz…」のような、意味のない反復出力は
    非音声に対する典型的な幻聴で、放置すると無駄な LLM/TTS を生み、単一MLXレーンを
    長時間占有してしまう。構造的な反復だけを見て弾く(通常の短い発話は誤検知しない)。
    """
    s = "".join(text.split())  # 空白を除いて評価(「Jazz Jazz」等の反復も拾う)
    if len(s) < 16:
        return False
    # 1) 短いパターン(1〜6文字)が列全体をほぼ埋め尽くしている(タイル状の反復)。
    for plen in range(1, 7):
        pattern = s[:plen]
        reps = len(s) // plen
        if reps >= 6 and pattern * reps == s[: plen * reps] and plen * reps >= len(s) * 0.8:
            return True
    # 2) 単一文字が支配的(「健」だけで大半を占める等)。
    _, cnt = Counter(s).most_common(1)[0]
    return cnt / len(s) >= 0.65


class SpeechToText:
    def __init__(self, model_repo: str = config.WHISPER_MODEL_REPO):
        self._model_repo = model_repo
        self.warm_up()

    def warm_up(self) -> None:
        """無音を1回文字起こししてモデルをメモリに常駐させる。ロード時だけでなく
        通話開始時にも呼び、アイドル中に退避した重みを温め直す用途にも使う。"""
        silence = np.zeros(config.VAD_SAMPLE_RATE, dtype=np.float32)
        t0 = time.monotonic()
        mlx_whisper.transcribe(
            silence,
            path_or_hf_repo=self._model_repo,
            language=config.WHISPER_LANGUAGE,
            fp16=True,
        )
        print(f"[stt] warm-up loaded {self._model_repo} in {time.monotonic() - t0:.2f}s")

    def transcribe(self, pcm_f32_16k: np.ndarray) -> str:
        """16kHz・mono・float32([-1, 1])のPCM配列を文字起こしする。同期・ブロッキング処理。"""
        if pcm_f32_16k.size == 0:
            return ""
        result = mlx_whisper.transcribe(
            pcm_f32_16k,
            path_or_hf_repo=self._model_repo,
            language=config.WHISPER_LANGUAGE,
            fp16=True,
            condition_on_previous_text=False,
        )
        return result["text"].strip()
