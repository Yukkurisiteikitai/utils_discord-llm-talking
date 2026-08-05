"""通話中のイベント(TTS発話内容・STT結果・割り込み・レイテンシ)を、指定した
Discordテキストチャンネルへリアルタイムに流すためのデバッグロガー。

ターミナルの print ログを見られない状況(スマホから通話しているなど)でも、
会話の中身と各段のレイテンシを Discord 上で追えるようにするのが目的。

設計上の注意:
- emit() は音声パイプラインの受信スレッド/生成スレッド/イベントループの
  どこからでも呼ばれ得る。そのため emit() は「ロックを取って行をバッファに
  積むだけ」の軽量・スレッドセーフな処理に留め、実際の channel.send() は
  イベントループ上の flush ループに任せる。
- Discord のチャンネルは 5秒で5メッセージ程度のレート制限がある。1イベント=
  1送信にすると通話中すぐ制限に当たるため、約1秒ごとに行をまとめて1メッセージ
  として送る(2000文字上限で分割)。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import List, Optional

import discord

import config

_DISCORD_MSG_LIMIT = 2000


class DebugChannelLogger:
    """通話イベントを Discord チャンネルへ流すバッファ付きロガー。

    /debug スラッシュコマンドから enable()/disable()/set_channel() で操作する。
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client
        self.enabled: bool = config.DEBUG_LOG_ENABLED
        self.channel_id: int = config.DEBUG_LOG_CHANNEL_ID
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._buffer: List[str] = []
        self._lock = threading.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """flush ループを開始する(モデルロード後、loop が確定してから呼ぶ)。"""
        self._loop = loop
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush_loop())

    # ------------------------------------------------------------------
    # 設定(/debug コマンドから呼ぶ)
    # ------------------------------------------------------------------
    def enable(self, channel_id: Optional[int] = None) -> None:
        if channel_id:
            self.channel_id = channel_id
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    def set_channel(self, channel_id: int) -> None:
        self.channel_id = channel_id

    def status_text(self) -> str:
        state = "ON" if self.enabled else "OFF"
        where = f"<#{self.channel_id}>" if self.channel_id else "(未設定)"
        return f"デバッグログ: **{state}** / 出力先: {where}"

    # ------------------------------------------------------------------
    # 記録(パイプラインの各所から呼ぶ)
    # ------------------------------------------------------------------
    def emit(self, message: str) -> None:
        """1行をバッファへ積む。無効/出力先未設定なら即return(どのスレッドからでも安全)。"""
        if not self.enabled or not self.channel_id:
            return
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self._buffer.append(f"`{ts}` {message}")

    # ------------------------------------------------------------------
    # 送信(イベントループ側)
    # ------------------------------------------------------------------
    async def _flush_loop(self) -> None:
        interval = config.DEBUG_LOG_FLUSH_INTERVAL_SEC
        while True:
            try:
                await asyncio.sleep(interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 - デバッグログで通話を止めない
                print(f"[debug-log] flush 失敗(続行します): {e}")

    async def _flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            lines = self._buffer
            self._buffer = []
        channel = self._resolve_channel()
        if channel is None:
            return
        for chunk in _pack_lines(lines):
            try:
                await channel.send(chunk)
            except Exception as e:  # noqa: BLE001
                print(f"[debug-log] 送信失敗(続行します): {e}")
                return

    def _resolve_channel(self) -> Optional[discord.abc.Messageable]:
        if not self.channel_id:
            return None
        channel = self._client.get_channel(self.channel_id)
        return channel if isinstance(channel, discord.abc.Messageable) else None


def _pack_lines(lines: List[str]) -> List[str]:
    """行リストを、Discord の2000文字上限に収まるメッセージ群へまとめる。"""
    messages: List[str] = []
    current = ""
    for line in lines:
        # 1行だけで上限を超える異常ケースは切り詰める。
        if len(line) > _DISCORD_MSG_LIMIT:
            line = line[: _DISCORD_MSG_LIMIT - 1] + "…"
        if current and len(current) + 1 + len(line) > _DISCORD_MSG_LIMIT:
            messages.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        messages.append(current)
    return messages
