"""Smart Turn v3.2 による意味的な終話(end-of-turn)検出。

現行の「無音が VAD_MIN_SILENCE_MS 続いたら発話終了」という沈黙タイマー方式は、
文の途中の息継ぎ(間)でも区切ってしまう(早切り)ことがある。Smart Turn は
発話音声そのものを見て「本当に喋り終わったか / まだ続くか」を確率で返す
言語的な終話判定で、間の自然さを上げるために併用する。

モデル(smart-turn-v3.2)と log-mel 特徴量抽出(`_whisper_features.py`)は
pipecat(BSD-2, Daily)の実装を vendor したもの。推論の前処理・入出力は
pipecat `LocalSmartTurnAnalyzerV3._predict_endpoint` に忠実に合わせてある:
  1. 16kHz mono float32 を「末尾8秒」に切り詰め(短ければ先頭ゼロ詰め)
  2. Whisper 風 log-mel 特徴量 (80, 800) を計算
  3. ONNX 入力 "input_features" (1, 80, 800) で推論
  4. 出力はシグモイド確率。prob > threshold で「発話完了」
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

import config
from pipeline._whisper_features import compute_whisper_log_mel_features

_MODEL_SAMPLE_RATE = 16000
_MODEL_SECONDS = 8
_MODEL_SAMPLES = _MODEL_SAMPLE_RATE * _MODEL_SECONDS  # 128000


def _truncate_or_pad_to_8s(audio: np.ndarray) -> np.ndarray:
    """末尾8秒を残す(短ければ先頭にゼロ詰め)。pipecat と同じ挙動。"""
    if len(audio) > _MODEL_SAMPLES:
        return audio[-_MODEL_SAMPLES:]
    if len(audio) < _MODEL_SAMPLES:
        return np.pad(audio, (_MODEL_SAMPLES - len(audio), 0), mode="constant")
    return audio


class SmartTurnDetector:
    """発話音声から「喋り終わったか」を判定する軽量 ONNX 分類器。

    detect(audio) -> (is_complete, probability)
    audio は 16kHz mono float32([-1,1])、長さは任意(内部で8秒に整える)。
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        threshold: float = 0.5,
        cpu_count: int = 1,
    ) -> None:
        self.model_path = str(model_path or config.SMART_TURN_MODEL_PATH)
        self.threshold = threshold
        self._cpu_count = cpu_count
        self._session = None  # 遅延ロード

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort

        if not Path(self.model_path).is_file():
            raise FileNotFoundError(
                f"Smart Turn モデルが見つかりません: {self.model_path}"
            )
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = self._cpu_count
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(self.model_path, sess_options=so)

    def detect(self, audio: np.ndarray) -> tuple[bool, float]:
        """(is_complete, probability) を返す。空入力は「未完了」扱い。"""
        if audio is None or len(audio) == 0:
            return False, 0.0
        self._ensure_session()
        assert self._session is not None

        audio = np.asarray(audio, dtype=np.float32)
        audio = _truncate_or_pad_to_8s(audio)

        log_mel = compute_whisper_log_mel_features(audio, do_normalize=True)
        input_features = np.expand_dims(log_mel, axis=0).astype(np.float32)  # (1,80,800)

        outputs = self._session.run(None, {"input_features": input_features})
        probability = float(np.asarray(outputs[0]).flatten()[0])
        probability = max(0.0, min(1.0, probability))
        return probability > self.threshold, probability

    def warmup(self) -> None:
        """モデルをロードし1回空推論して、初回のロード/JITコストを通話前に払う。"""
        self._ensure_session()
        try:
            self.detect(np.zeros(_MODEL_SAMPLE_RATE, dtype=np.float32))
        except Exception:
            pass
