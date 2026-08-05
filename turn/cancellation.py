"""投機・生成のキャンセル土台(ARCHITECTURE_v2_PROPOSAL.md §3.3 / §4.9)。

M3 / 8GB では `asyncio.Task.cancel()` だけで `to_thread` 内部の MLX 生成が必ず
止まるとは限らない。そのため二層で守る:

  1. `CancellationToken` を生成ループの中で確認し、可能な場所で停止する。
  2. 停止できなかった成果物も `generation_id` / `playback_epoch` の不一致で破棄する。

このモジュールはその1層目(協調的キャンセル)と、非同期成果物へ付ける
単調増加IDの採番だけを担う。重い依存は持たない。
"""
from __future__ import annotations

import itertools

# 通話を跨いでも単調増加する generation_id。古いLLM出力を「現行か」で判定して
# 破棄するために使う(ARCHITECTURE_v2_PROPOSAL.md §3.3 の ID 体系)。
_generation_counter = itertools.count(1)
_turn_counter = itertools.count(1)


def next_generation_id() -> int:
    return next(_generation_counter)


def next_turn_id() -> int:
    return next(_turn_counter)


class CancellationToken:
    """1つの生成(LLM/TTS)に紐づく協調的キャンセルフラグ。

    生成側は分割可能な地点(トークンごと・文ごと・synthesize前)で
    `cancelled` を確認し、True なら速やかに中断する。停止できなかった成果物も
    epoch / generation_id 検査で後段が捨てるため、これは best-effort でよい。
    """

    __slots__ = ("_cancelled", "reason")

    def __init__(self) -> None:
        self._cancelled = False
        self.reason = ""

    def cancel(self, reason: str = "") -> None:
        self._cancelled = True
        if reason and not self.reason:
            self.reason = reason

    @property
    def cancelled(self) -> bool:
        return self._cancelled
