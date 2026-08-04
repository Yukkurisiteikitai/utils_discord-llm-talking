"""VOICEVOX互換TTSエンジン(ローカルHTTPサーバー)を使った音声合成クライアント。

VOICEVOX ENGINE / AivisSpeech Engine はどちらもHTTP APIが
/audio_query -> /synthesis で互換なので、どちらを使うかは
config.TTS_ENGINE_BASE_URL / TTS_SPEAKER_ID / TTS_ENGINE_BINARY_PATH /
TTS_ENGINE_APP_NAME の差し替えだけで切り替えられる(このクラスのロジック変更は不要)。
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

import config


class TextToSpeechError(RuntimeError):
    pass


class TextToSpeech:
    def __init__(
        self,
        base_url: str = config.TTS_ENGINE_BASE_URL,
        speaker: int = config.TTS_SPEAKER_ID,
    ):
        self._base_url = base_url.rstrip("/")
        self._speaker = speaker

    async def health_check(self) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self._base_url}/version") as resp:
                    return resp.status == 200
        except (aiohttp.ClientError, TimeoutError, OSError):
            return False

    async def ensure_running(self) -> bool:
        """エンジンが起動しているか確認し、起動していなければ自動起動を試みる。

        macOS専用。実測で、GUIアプリ経由(`open -a`)の起動はElectron自体の
        オーバーヘッドで3分40秒もかかったのに対し、GUIアプリに同梱された
        ENGINE実行ファイルを直接ヘッドレスで叩くと約10秒で起動できたため、
        直接起動を優先し、実行ファイルが見つからない場合のみGUI起動にフォールバックする。
        どちらも失敗、あるいはタイムアウトした場合はFalseを返す
        (未インストール/Gatekeeper未承認のどちらかは自動では区別できない)。
        """
        if await self.health_check():
            return True

        if not self._launch_engine_process():
            return False

        deadline = time.monotonic() + config.TTS_ENGINE_LAUNCH_TIMEOUT_SEC
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            if await self.health_check():
                print("[tts] エンジンの自動起動に成功しました")
                await self._warm_up()
                return True

        print(
            f"[tts] {config.TTS_ENGINE_LAUNCH_TIMEOUT_SEC}秒待ちましたがエンジンが応答しません。"
            f" {config.TTS_ENGINE_APP_NAME} が未インストール、または初回起動時のGatekeeper確認待ちの"
            "可能性があります。一度手動でアプリを起動して承認してください。"
        )
        return False

    def _launch_engine_process(self) -> bool:
        binary = Path(config.TTS_ENGINE_BINARY_PATH)
        if binary.is_file():
            parsed = urlsplit(self._base_url)
            print(f"[tts] エンジンをヘッドレスで直接起動します: {binary}")
            try:
                subprocess.Popen(
                    [str(binary), "--host", parsed.hostname, "--port", str(parsed.port)],
                    cwd=str(binary.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except OSError as exc:
                print(f"[tts] エンジンの直接起動に失敗しました: {exc}(GUI起動にフォールバックします)")

        print(f"[tts] {config.TTS_ENGINE_APP_NAME} をGUIアプリとして起動します...")
        try:
            subprocess.Popen(
                ["open", "-a", config.TTS_ENGINE_APP_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError as exc:
            print(f"[tts] 自動起動コマンドの実行に失敗しました: {exc}")
            return False

    async def _warm_up(self) -> None:
        # 初回のsynthesize呼び出しはモデル初期化コストで数秒余分にかかる
        # (実測で健全化後の初回だけ約4秒、2回目以降は約1.5秒)。ここで1回
        # 空撃ちしておくことで、実際の通話の最初の一言でこのコストを踏まないようにする。
        t0 = time.monotonic()
        try:
            await self.synthesize("こんにちは")
        except TextToSpeechError as exc:
            print(f"[tts] ウォームアップに失敗しました: {exc}")
            return
        print(f"[tts] ウォームアップ完了 ({time.monotonic() - t0:.2f}s)")

    async def synthesize(self, text: str) -> bytes:
        """テキストを合成し、WAV形式の音声バイト列を返す。"""
        timeout = aiohttp.ClientTimeout(total=config.TTS_ENGINE_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self._base_url}/audio_query",
                params={"text": text, "speaker": str(self._speaker)},
            ) as resp:
                if resp.status != 200:
                    raise TextToSpeechError(
                        f"audio_query failed: {resp.status} {await resp.text()}"
                    )
                query = await resp.json()

            async with session.post(
                f"{self._base_url}/synthesis",
                params={"speaker": str(self._speaker)},
                json=query,
            ) as resp:
                if resp.status != 200:
                    raise TextToSpeechError(
                        f"synthesis failed: {resp.status} {await resp.text()}"
                    )
                return await resp.read()
