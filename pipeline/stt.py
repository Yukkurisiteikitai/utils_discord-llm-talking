"""MLX Whisperによるローカル音声認識。

mlx_whisper.transcribe() は内部の ModelHolder が読み込み済みモデルを
プロセス内にキャッシュするため、同じ path_or_hf_repo で呼び続ける限り
2回目以降はディスク読み込みが発生しない。__init__ で無音データを1回
文字起こしすることで、実際の通話が始まる前にモデルをメモリに常駐させる
(通話中の初回発話でロード待ちが発生しないようにするため)。
"""
from __future__ import annotations

import time

import mlx_whisper
import numpy as np

import config


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
