#!/usr/bin/env python3
"""Voice Doctor — 通話が「繋がるのに聞こえない」系を起動前に切り分ける診断ツール。

このプロジェクト(discord_caller)専用。HANDOFF.md にある実機の落とし穴
(libopusのパス / davey=DAVE / voice-recv / VOICEVOX未起動 / Bot権限)を
一括でチェックする。Hermes Agent の discord-voice-doctor.py 設計を、
本プロジェクトの config.py / .env に合わせて作り直したもの(MIT由来の設計参照)。

使い方:
    .venv/bin/python scripts/voice_doctor.py
"""
from __future__ import annotations

import ctypes.util
import os
import shutil
import sys
from pathlib import Path

# プロジェクトルートを import パスに追加(config を読むため)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OK = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"


def check(label: str, ok: bool, detail: str = "") -> bool:
    symbol = OK if ok else FAIL
    msg = f"  {symbol} {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    return ok


def warn(label: str, detail: str = "") -> None:
    msg = f"  {WARN} {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def _mask(value: str) -> str:
    if not value or len(value) < 8:
        return "****"
    return f"{value[:4]}{'*' * (len(value) - 4)}"


def check_packages() -> bool:
    """必須Pythonパッケージ。1つでも欠けると通話は成立しない。"""
    section("Python パッケージ")
    ok = True

    try:
        import discord

        check("discord.py", True, f"v{discord.__version__}")
    except ImportError:
        ok = check("discord.py", False, "pip install 'discord.py[voice]'")

    try:
        import nacl  # noqa: F401
        import nacl.secret

        nacl.secret.Aead(bytes(32))
        check("PyNaCl (Aead)", True, f"v{getattr(nacl, '__version__', '?')}")
    except Exception as e:
        ok = check("PyNaCl (Aead)", False, f"{e} — need >=1.5.0")

    # davey = DAVE(E2EE音声)復号。これが無いと受信音声が100% corrupted になる。
    try:
        import davey

        check("davey (DAVE E2EE 復号)", True, f"v{getattr(davey, '__version__', '?')}")
    except ImportError:
        ok = check("davey (DAVE E2EE 復号)", False, "pip install davey — 無いと受信音声が全滅")

    try:
        from discord.ext import voice_recv  # noqa: F401

        check("discord-ext-voice-recv", True)
    except ImportError:
        ok = check(
            "discord-ext-voice-recv",
            False,
            "pip install git+https://github.com/imayhaveborkedit/discord-ext-voice-recv.git",
        )

    # ローカルML(MLX)。STT/LLMの実体。
    for mod, label in [
        ("mlx_whisper", "mlx-whisper (STT)"),
        ("mlx_lm", "mlx-lm (LLM)"),
        ("onnxruntime", "onnxruntime (VAD/Smart Turn)"),
        ("numpy", "numpy"),
    ]:
        try:
            __import__(mod)
            check(label, True)
        except ImportError:
            ok = check(label, False, f"pip install {mod.replace('_', '-')}")

    return ok


def check_system_tools() -> bool:
    """libopus / ffmpeg。特にlibopusはApple Silicon Homebrewで自動検出に失敗する。"""
    section("システムツール")
    ok = True

    try:
        import discord

        if discord.opus.is_loaded():
            check("libopus", True, "already loaded")
        else:
            path = ctypes.util.find_library("opus")
            if not path:
                # bot._ensure_opus_loaded と同じ既知パス(Apple Silicon優先)
                for candidate in (
                    "/opt/homebrew/lib/libopus.dylib",  # Apple Silicon Homebrew
                    "/usr/local/lib/libopus.dylib",  # Intel Homebrew
                ):
                    if os.path.isfile(candidate):
                        path = candidate
                        break
            if path:
                discord.opus.load_opus(path)
                check("libopus", discord.opus.is_loaded(), path)
                ok &= discord.opus.is_loaded()
            else:
                ok = check("libopus", False, "brew install opus")
    except Exception as e:  # noqa: BLE001
        ok = check("libopus", False, str(e))

    # ffmpeg は現構成(VOICEVOX HTTP + 生PCM再生)では必須ではないため warn 止まり。
    ff = shutil.which("ffmpeg")
    if ff:
        check("ffmpeg", True, ff)
    else:
        warn("ffmpeg", "未検出 — 現構成では必須ではないが、あると保険になる")

    return ok


def check_env_and_config() -> tuple[bool, str]:
    """.env の必須値と config の妥当性。token を返す(権限チェックで使う)。"""
    section("設定 (.env / config.py)")
    ok = True
    token = ""

    try:
        import config

        token = config.DISCORD_TOKEN
        check("DISCORD_TOKEN", True, _mask(token))
        for name in ("TARGET_USER_ID", "GUILD_ID", "VOICE_CHANNEL_ID"):
            val = getattr(config, name, None)
            check(name, bool(val), str(val))
            ok &= bool(val)
    except Exception as e:  # noqa: BLE001
        # _require_env が投げるケースを含む
        ok = check("config.py 読み込み", False, str(e))

    return ok, token


def check_voicevox() -> bool:
    """VOICEVOX ENGINE がHTTPで応答するか。未起動だとTTSが無音になる。"""
    section("TTS エンジン (VOICEVOX)")
    try:
        import config

        base = config.TTS_ENGINE_BASE_URL
    except Exception as e:  # noqa: BLE001
        return check("VOICEVOX base_url 取得", False, str(e))

    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base}/version", timeout=3) as r:
            ver = r.read().decode("utf-8", "ignore").strip().strip('"')
        return check("VOICEVOX ENGINE 応答", True, f"{base} v{ver}")
    except urllib.error.URLError:
        warn(
            "VOICEVOX ENGINE",
            f"{base} に未応答 — 未起動の可能性。botは自動起動を試みるが、"
            "初回はGUIアプリを一度承認する必要あり(README参照)",
        )
        return True  # 自動起動があるので fail にはしない
    except Exception as e:  # noqa: BLE001
        warn("VOICEVOX ENGINE", str(e))
        return True


def check_bot_permissions(token: str) -> bool:
    """Discord API で Bot のボイス権限を確認。GUILD_ID のギルドを重点チェック。"""
    section("Bot 権限")
    if not token:
        warn("Bot 権限", "token 無し — スキップ")
        return True
    try:
        import requests
    except ImportError:
        warn("Bot 権限", "requests 未導入 — スキップ")
        return True

    try:
        import config

        target_guild = str(config.GUILD_ID)
    except Exception:
        target_guild = None

    # 必要権限(ビット位置)
    REQUIRED = {"View Channel": 10, "Send Messages": 11, "Connect": 20, "Speak": 21}
    headers = {"Authorization": f"Bot {token}"}
    ok = True
    try:
        r = requests.get(
            "https://discord.com/api/v10/users/@me", headers=headers, timeout=5
        )
        if r.status_code == 401:
            return check("Bot login", False, "invalid token (401)")
        if r.status_code != 200:
            return check("Bot login", False, f"HTTP {r.status_code}")
        name = r.json().get("username", "?")
        check("Bot login", True, name)

        r2 = requests.get(
            "https://discord.com/api/v10/users/@me/guilds", headers=headers, timeout=5
        )
        if r2.status_code != 200:
            warn("Guilds", f"HTTP {r2.status_code}")
            return ok
        guilds = r2.json()
        check("Guilds", True, f"{len(guilds)} guild(s)")

        found_target = False
        for g in guilds:
            is_focus = target_guild is not None and str(g.get("id")) == target_guild
            if target_guild is not None and not is_focus:
                continue
            found_target = found_target or is_focus
            perms = int(g.get("permissions", 0))
            if perms & (1 << 3):  # Administrator
                print(f"    {OK} {g['name']}: Administrator")
                continue
            missing = [n for n, bit in REQUIRED.items() if not (perms & (1 << bit))]
            if missing:
                print(f"    {FAIL} {g['name']}: 権限不足 {', '.join(missing)}")
                ok = False
            else:
                print(f"    {OK} {g['name']}: 必要権限OK (Connect/Speak/View/Send)")

        if target_guild is not None and not found_target:
            ok = check(
                f"対象ギルド {target_guild}",
                False,
                "Botがこのギルドに参加していない",
            )
    except requests.exceptions.RequestException as e:
        warn("Bot 権限", f"Discord API 到達失敗: {e}")

    return ok


def main() -> int:
    print("\033[1m" + "=" * 50 + "\033[0m")
    print("\033[1m  Voice Doctor (discord_caller)\033[0m")
    print("\033[1m" + "=" * 50 + "\033[0m")

    all_ok = True
    all_ok &= check_packages()
    all_ok &= check_system_tools()
    env_ok, token = check_env_and_config()
    all_ok &= env_ok
    all_ok &= check_voicevox()
    all_ok &= check_bot_permissions(token)

    print("\n\033[1m" + "-" * 50 + "\033[0m")
    if all_ok:
        print(f"  {OK} \033[92m全チェック通過 — 通話の前提はそろっています\033[0m")
    else:
        print(f"  {FAIL} \033[91m失敗あり — 上の項目を直してください\033[0m")
    print()
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
