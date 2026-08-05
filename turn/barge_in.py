"""割り込み(barge-in)を第一級イベントとして扱う(ARCHITECTURE_v2_PROPOSAL.md §4.14)。

AI音声の再生中にユーザーが話し始めたら:

  1. 短い確認窓でノイズを除外(呼び出し側が担当)
  2. playback_epoch をバンプして未再生WAV・再生中チャンクを破棄(player.interrupt())
  3. 進行中のLLM/TTSをキャンセル(CancellationToken)
  4. 実際に鳴らした範囲だけを会話履歴へ残す(player.take_spoken_texts())

Phase 1 では backchannel(「うん」「なるほど」)と hard interruption の分類はせず、
発話開始=割り込みとして単純に扱う。resume 機構(誤割り込みの復帰)も入れない。
"""
from __future__ import annotations

from typing import Optional

from audio.player import InterruptInfo, QueuedPCMSource
from turn.cancellation import CancellationToken


class BargeInController:
    """1通話ぶんの割り込み調停。現在の生成に紐づく CancellationToken を保持し、
    割り込み発火時に player の epoch バンプとトークンのキャンセルをまとめて行う。"""

    def __init__(self, player: QueuedPCMSource) -> None:
        self._player = player
        self._active_token: Optional[CancellationToken] = None

    def set_active(self, token: CancellationToken) -> None:
        """新しいターンの生成が始まったら、その CancellationToken を登録する。"""
        self._active_token = token

    def clear_active(self, token: CancellationToken) -> None:
        """ターンの生成が正常終了したら登録解除する(取り違えを避けるため一致確認)。"""
        if self._active_token is token:
            self._active_token = None

    def trigger(self) -> Optional[InterruptInfo]:
        """割り込みを確定させる。AI音声が鳴っておらず生成中でもなければ何もしない。

        player の epoch をバンプして未再生音声を破棄し、進行中の生成をキャンセルする。
        実際に鳴らした発話テキストの回収は、そのターンの handle_utterance 側が
        再生の落ち着き(barge-in による flush)を待ってから take_spoken_texts() で行う。
        """
        active = self._active_token
        if not self._player.is_playing_or_pending() and active is None:
            return None

        info: InterruptInfo = self._player.interrupt()
        if active is not None:
            active.cancel("barge_in")
            self._active_token = None
        return info
