"""ターン単位のレイテンシ / ターンテイキング計測を、1行1JSONのJSONLで記録する。

速さ(応答レイテンシ)と間の自然さ(発話終了→AI発声開始のギャップ)を実会話から
測るための最小の計測基盤。重い依存は一切持たない(標準ライブラリのみ)ので、
音声パイプラインとは独立に import / テストできる。

すべての *_ms は、そのターンでユーザーが話し終わった瞬間(VADの発話終了検知,
speech_end)を起点にした経過時間。これにより「話し終わってから何msで何が起きたか」
が1行で読める。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TurnRecord:
    """1ターンぶんの計測値。未計測の項目は None のままにする(推測で埋めない)。"""

    turn_index: int

    # --- ID 体系(ARCHITECTURE_v2_PROPOSAL.md §3.3)。非同期成果物の混入防止と
    #     barge-in/投機の因果追跡に使う。0 = 旧経路/未設定。 ---
    turn_id: int = 0
    generation_id: int = 0
    playback_epoch: int = 0

    # --- VAD(発話区間)---
    utterance_ms: float = 0.0       # pre-roll を含む発話バッファ全体の長さ
    pre_roll_ms: float = 0.0        # 実際に先頭へ付け足した pre-roll の長さ
    ended_at_max: bool = False      # 無音ではなく最大長で強制区切りされたか

    # --- 各段階のレイテンシ(speech_end 起点, ms)---
    stt_ms: Optional[float] = None                  # 文字起こし完了まで
    llm_first_sentence_ms: Optional[float] = None   # LLMが最初の1文を出すまで
    tts_first_chunk_ms: Optional[float] = None       # 最初の文の合成が終わるまで

    # --- 体感の肝: 発話終了 → 実際にAI音声が鳴り始めるまで(ms)---
    response_gap_ms: Optional[float] = None

    n_sentences: int = 0            # 応答の文数
    stt_text: str = ""              # 認識結果(空 = 無音/破棄)

    # --- Smart Turn(意味的終話検出)。OFF/未実行なら prob=None, count=0 ---
    smart_turn_prob: Optional[float] = None   # ターン確定時の最後の完了確率
    continuation_count: int = 0

    # --- Barge-in(割り込み)。ARCHITECTURE_v2_PROPOSAL.md §7.3 ---
    interrupted: bool = False        # このターンの応答がユーザー割り込みで打ち切られたか
    played_chunks: int = 0           # 実際に再生を開始したチャンク数
    dropped_audio_ms: float = 0.0    # 割り込みで破棄した未再生音声の長さ(ms)               # 「まだ続く」で延長した回数

    # 参考: 壁時計(人が読むログや他ログとの突合せ用)。
    wall_clock: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))


class TurnLogger:
    """TurnRecord を JSONL ファイルへ追記するだけの薄いロガー。

    write() は音声再生スレッド由来のコールバックとイベントループの双方から
    呼ばれ得るため、ファイル追記をロックで直列化する。
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: TurnRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=False)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
