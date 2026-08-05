"""Discord「電話」Bot エントリポイント。

起動すると、ランダムな間隔でTARGET_USER_IDにDM着信を送る。
`/call-me` で即座に着信をテストでき、`/hangup` で通話を終了できる。
"""
from __future__ import annotations

import asyncio
import ctypes.util
import logging
import os
import random

# discord.py(aiohttp)は既定で certifi のCAバンドルを使うため、Cloudflare WARP /
# Zero Trust などTLS検査プロキシが入った環境では、差し込まれた社内/Gateway CAを
# 知らず discord.com への接続が SSLCertVerificationError("self-signed certificate
# in certificate chain")で落ちる。truststore を注入すると Python の SSL 検証が
# macOSキーチェーン(WARPのGateway CAを含む)を使うようになり、WARPのON/OFFに
# 関係なく繋がる。truststore が無い環境では従来通り certifi にフォールバックする。
try:
    import truststore

    truststore.inject_into_ssl()
    print("[bot] truststore を注入しました(SSL検証にmacOSキーチェーンを使用)")
except Exception as _e:  # noqa: BLE001
    print(f"[bot] truststore を注入できませんでした({_e}) — certifiで続行します")

import discord
from discord import app_commands

import config
from call_flow import CallManager


def _quiet_voice_recv_logging() -> None:
    """discord-ext-voice-recvは"Received unexpected rtcp packet"等のINFOログを
    ほぼ1秒おきに出し続ける。実機でこのログ出力自体がCPUを消費し応答レイテンシを
    悪化させる一因になっていたため、WARNING以上のみに絞る。"""
    logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)
    logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.WARNING)


def _ensure_opus_loaded() -> None:
    """discord.pyのボイス機能に必要なlibopusをロードする。

    macOS(特にApple Silicon/Homebrew)では ctypes.util.find_library('opus') が
    /opt/homebrew 配下のライブラリを見つけられず None を返すことがあり、その場合
    discord.py側の自動ロードが失敗して voice_client.play() 時に
    discord.opus.OpusNotLoaded が発生する。実機で確認済みのため、
    find_libraryが失敗した場合はHomebrewの既知パスに直接フォールバックする。
    """
    if discord.opus.is_loaded():
        return

    candidate = ctypes.util.find_library("opus")
    if candidate is None:
        for path in (
            "/opt/homebrew/lib/libopus.dylib",  # Apple Silicon Homebrew
            "/usr/local/lib/libopus.dylib",  # Intel Homebrew
        ):
            if os.path.exists(path):
                candidate = path
                break

    if candidate is None:
        raise RuntimeError(
            "libopusが見つかりません。`brew install opus` でインストールしてください。"
        )

    discord.opus.load_opus(candidate)
    print(f"[bot] libopusをロードしました: {candidate}")


def _patch_voice_recv_resilience() -> None:
    """discord-ext-voice-recvの受信ループを、1パケットのデコードエラーで
    死なないようにパッチする。

    実機で確認した重大なバグ: PacketRouter.run() は _do_run() 全体を
    1つのtry/exceptで囲っており、デコード中に discord.opus.OpusError
    ("corrupted stream" 等)が1回でも起きると、ログを出した後
    voice_client.stop_listening() を呼んでスレッドごと終了する。
    つまりネットワーク由来のパケット破損が1回でも起きると、その通話は
    以後ずっと音声を受信できなくなる(「最初の応答以降スタックする」の
    直接の原因だった)。ここでは _do_run をパケット単位のtry/exceptに
    差し替え、1パケット失敗してもループを継続できるようにする。
    _get_next_packet() がバッファからパケットを取り出した後にデコードする
    実装のため、例外を握りつぶして次に進んでも同じ壊れたパケットを
    再試行することはなく安全。
    """
    from discord.ext.voice_recv import router as voice_recv_router

    # フルトレースバックのログ出力(log.exception)は1回あたり無視できない
    # CPUコストがあり、パケットロス多発時に応答レイテンシが数倍〜数十倍に
    # 悪化する原因になることを実機で確認した。ここでは件数カウンタのみ
    # 軽量に出力する。
    _err_count = {"pop_data": 0, "write": 0}

    def _resilient_do_run(self) -> None:
        while not self._end_thread.is_set():
            self.waiter.wait()
            with self._lock:
                for decoder in self.waiter.items:
                    try:
                        data = decoder.pop_data()
                    except Exception:
                        _err_count["pop_data"] += 1
                        if _err_count["pop_data"] % 100 == 1:
                            print(
                                f"[voice_recv patch] パケットデコード失敗 累計"
                                f"{_err_count['pop_data']}件 - スキップします"
                            )
                        continue
                    if data is not None:
                        try:
                            self.sink.write(data.source, data)
                        except Exception:
                            _err_count["write"] += 1
                            if _err_count["write"] % 100 == 1:
                                print(
                                    f"[voice_recv patch] sink.write失敗 累計"
                                    f"{_err_count['write']}件"
                                )

    voice_recv_router.PacketRouter._do_run = _resilient_do_run
    print("[bot] discord-ext-voice-recvの受信ループに耐障害性パッチを適用しました")


def _dave_decrypt_or_passthrough(session, user_id, davey_mod, data, recovery):
    """DAVE復号を試み、(Opusへ渡すバイト列, status)を返す純関数(テスト可能)。

    status:
      "decrypted"   … DAVE復号に成功
      "passthrough" … 非暗号化(passthrough)フレーム。DAVE前のデータを素通しする
      "skip"        … 復号失敗。破棄(b"")

    Hermes Agent(MIT)の adapter.py に倣い、例外メッセージが "Unencrypted" を
    含む場合は passthrough とみなし、NaCl復号済みの元データ(=DAVE前のOpus)を
    そのまま返す。それ以外の例外は従来通り破棄する。
    recovery=False のときは passthrough 復帰を行わず、あらゆる失敗を "skip" に倒す
    (=現行の実証済み挙動を完全に維持)。
    """
    try:
        out = session.decrypt(user_id, davey_mod.MediaType.audio, data)
        return out, "decrypted"
    except Exception as e:  # noqa: BLE001
        if recovery and "Unencrypted" in str(e):
            return data, "passthrough"
        return b"", "skip"


def _patch_voice_recv_dave_decrypt() -> None:
    """受信音声が100%の確率で `corrupted stream` になっていた根本原因への対処。

    実機調査で判明した内容: discord.py 2.7.1 は Discord が2026年3月に必須化した
    音声のE2EE(DAVEプロトコル、`davey`ライブラリでMLSベースのフレーム暗号化を実装)
    に対応済みだが、これは discord.py 本体の送信パス向け。一方
    discord-ext-voice-recv は独自に受信パイプラインを再実装しており、DAVE/davey の
    存在を一切知らない(コード内にdave/mlsへの参照が無いことを確認済み)。
    そのため受信パケットは通常のトランスポート層暗号化だけ解かれ、DAVEのMLS層は
    暗号化されたままOpusデコーダに渡され、必ず失敗する。

    discord.py側が保持している davey.DaveSession (VoiceClient._connection.dave_session)
    を使い、Opusデコードの直前にDAVE復号を追加で行うことで対処する。
    """
    import davey
    from discord.ext.voice_recv import opus as voice_recv_opus

    def _get_dave_session(self):
        vc = self.sink.voice_client
        connection = getattr(vc, "_connection", None)
        return getattr(connection, "dave_session", None) if connection else None

    # 診断/統計用のカウンタ。1パケットごとにフルトレースバックをログ出力すると
    # CPUを大きく消費し、実機で応答レイテンシが数倍〜数十倍に悪化する現象を
    # 確認したため、ログは軽量なカウンタ表示に留める。
    _diag_state = {
        "logged": False,
        "decrypt_ok": 0,
        "decrypt_fail": 0,
        "passthrough": 0,
    }
    _recovery = config.DAVE_PASSTHROUGH_RECOVERY

    def _decode_packet_with_dave(self, packet):
        assert self._decoder is not None

        if packet:
            data = packet.decrypted_data
            session = self._get_dave_session()

            if not _diag_state["logged"]:
                _diag_state["logged"] = True
                print(
                    f"[dave patch] session={'あり' if session else 'なし'} "
                    f"ready={session.ready if session else None} "
                    f"protocol_version={session.protocol_version if session else None} "
                    f"passthrough_recovery={_recovery}"
                )

            if session is not None and session.ready:
                user_id = self._cached_id
                if user_id is None:
                    user_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)
                if user_id is not None:
                    # Hermes(MIT)準拠の passthrough 復帰を含む純関数に委譲。
                    # recovery=False のときは従来通り、失敗はすべて "skip"。
                    data, status = _dave_decrypt_or_passthrough(
                        session, user_id, davey, data, _recovery
                    )
                    if status == "skip":
                        # コンフォートノイズ/無音など DAVE で暗号化されていない特殊
                        # フレームの復号失敗。復号できないデータを Opus に渡しても
                        # 二重に失敗するだけなので、このパケットは破棄する。
                        _diag_state["decrypt_fail"] += 1
                        if _diag_state["decrypt_fail"] % 100 == 1:
                            print(
                                f"[dave patch] 復号失敗 累計{_diag_state['decrypt_fail']}件"
                                f"(成功{_diag_state['decrypt_ok']}件 / "
                                f"passthrough{_diag_state['passthrough']}件) - スキップします"
                            )
                        return packet, b""
                    if status == "passthrough":
                        # 非暗号化フレーム。DAVE前のデータをそのまま Opus に通す。
                        _diag_state["passthrough"] += 1
                        if _diag_state["passthrough"] % 100 == 1:
                            print(
                                f"[dave patch] passthrough(非暗号化)フレームを素通し "
                                f"累計{_diag_state['passthrough']}件"
                            )
                    else:  # "decrypted"
                        _diag_state["decrypt_ok"] += 1
            pcm = self._decoder.decode(data, fec=False)
            return packet, pcm

        # フェイクパケット(FEC用)。この分岐は元の実装のまま。
        next_packet = self._buffer.peek_next()
        if next_packet is not None:
            nextdata: bytes = next_packet.decrypted_data  # type: ignore
            pcm = self._decoder.decode(nextdata, fec=True)
        else:
            pcm = self._decoder.decode(None, fec=False)

        return packet, pcm

    voice_recv_opus.PacketDecoder._get_dave_session = _get_dave_session
    voice_recv_opus.PacketDecoder._decode_packet = _decode_packet_with_dave
    print("[bot] DAVE(E2EE音声)復号パッチを適用しました")


_quiet_voice_recv_logging()
_ensure_opus_loaded()
_patch_voice_recv_resilience()
_patch_voice_recv_dave_decrypt()

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True  # Developer PortalでServer Members Intentを有効にする必要あり

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

call_manager: CallManager | None = None


def _is_target_user(interaction: discord.Interaction) -> bool:
    return interaction.user.id == config.TARGET_USER_ID


@tree.command(name="call-me", description="今すぐ着信をテストする")
async def call_me(interaction: discord.Interaction) -> None:
    if not _is_target_user(interaction):
        await interaction.response.send_message("このコマンドは使えません。", ephemeral=True)
        return
    await interaction.response.send_message("着信を送りました。DMを確認してください。", ephemeral=True)
    assert call_manager is not None
    await call_manager.start_incoming_call()


@tree.command(name="hangup", description="通話を終了する")
async def hangup(interaction: discord.Interaction) -> None:
    if not _is_target_user(interaction):
        await interaction.response.send_message("このコマンドは使えません。", ephemeral=True)
        return
    assert call_manager is not None
    await call_manager.end_call()
    await interaction.response.send_message("通話を終了しました。", ephemeral=True)


async def _random_call_scheduler() -> None:
    assert call_manager is not None
    await client.wait_until_ready()
    while not client.is_closed():
        wait_sec = random.uniform(config.CALL_INTERVAL_MIN_SEC, config.CALL_INTERVAL_MAX_SEC)
        print(f"[schedule] 次の着信まで約{wait_sec / 60:.0f}分")
        await asyncio.sleep(wait_sec)
        await call_manager.start_incoming_call()


@client.event
async def on_ready() -> None:
    global call_manager
    print(f"[bot] logged in as {client.user}")

    guild = discord.Object(id=config.GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)

    if call_manager is None:
        call_manager = CallManager(client)
        print("[bot] モデルをロード中...")
        await call_manager.load_models()
        print("[bot] モデルのロード完了。着信スケジューラを開始します。")
        client.loop.create_task(_random_call_scheduler())


@client.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None
    print(f"[voice_state] member={member.id} before={before_id} after={after_id}")

    if member.id != config.TARGET_USER_ID or call_manager is None:
        return

    joined_target = (
        after.channel is not None
        and after.channel.id == config.VOICE_CHANNEL_ID
        and (before.channel is None or before.channel.id != config.VOICE_CHANNEL_ID)
    )
    left_target = (
        before.channel is not None
        and before.channel.id == config.VOICE_CHANNEL_ID
        and (after.channel is None or after.channel.id != config.VOICE_CHANNEL_ID)
    )

    if joined_target:
        await call_manager.on_target_user_joined_channel()
    elif left_target:
        await call_manager.on_target_user_left_channel()


def main() -> None:
    client.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
