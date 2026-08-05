"""ターン処理(ターン判定・投機・割り込み)のためのパッケージ。

ARCHITECTURE_v2_PROPOSAL.md のイベント駆動構成へ段階移行するための入れ物。
Phase 1 では Cancellation / Barge-in の基盤(cancellation.py, barge_in.py)だけを
提供し、以降の Phase でここへ coordinator / endpointer / speculation 等を足していく。
"""
