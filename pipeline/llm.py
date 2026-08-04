"""mlx-lmによるローカルLLM応答生成。

文の区切り(。！？など)が来るたびに逐次yieldする。これにより、LLMが
応答全体を生成し終えるのを待たずに最初の一文をすぐTTSに渡して再生を
開始できる(体感レイテンシを下げるための肝)。
"""
from __future__ import annotations

import re
import time
from typing import Iterator

from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_logits_processors, make_sampler

import config

_SENTENCE_END_RE = re.compile(r"[。!?！？\n]")

# 小型モデルは同じ単語・フレーズを延々と繰り返す壊れた生成に陥ることがある
# (実機で「もっともっともっと...」が数百回続く例を確認済み)。
# repetition_penaltyで発生自体を抑制しつつ、それでも起きた場合に備えて
# 末尾の繰り返しパターンを検知して強制的に打ち切る安全弁も併用する。
_REPETITION_MIN_REPEATS = 5
_REPETITION_MAX_PATTERN_LEN = 12


def _detect_and_trim_repetition(text: str) -> tuple[str, bool]:
    """末尾が同一パターンの繰り返しになっていないか調べる。

    見つかった場合は、繰り返しを1回分だけ残してトリムしたテキストと
    True を返す(全部消すと不自然に文が切れるため)。
    """
    for pattern_len in range(1, _REPETITION_MAX_PATTERN_LEN + 1):
        needed = pattern_len * _REPETITION_MIN_REPEATS
        if len(text) < needed:
            continue
        tail = text[-needed:]
        pattern = tail[:pattern_len]
        if pattern * _REPETITION_MIN_REPEATS == tail:
            trimmed = text[: len(text) - needed + pattern_len]
            return trimmed, True
    return text, False


class LanguageModel:
    def __init__(self, model_repo: str = config.LLM_MODEL_REPO):
        t0 = time.monotonic()
        self.model, self.tokenizer = load(model_repo)
        self._sampler = make_sampler(temp=config.LLM_TEMPERATURE)
        self._logits_processors = make_logits_processors(
            repetition_penalty=config.LLM_REPETITION_PENALTY,
            repetition_context_size=20,
        )
        print(f"[llm] loaded {model_repo} in {time.monotonic() - t0:.2f}s")
        self._warm_up()

    def _warm_up(self) -> None:
        # mlx-lmの初回generate呼び出しはMetalカーネルのJITコンパイルが走り、
        # 数秒〜10秒近くかかることがある。ここで1回空撃ちしておくことで、
        # 通話中の最初の発話でこのコストを踏まないようにする。
        t0 = time.monotonic()
        for _ in self.stream_sentences([{"role": "user", "content": "こんにちは"}]):
            pass
        print(f"[llm] warm-up generation in {time.monotonic() - t0:.2f}s")

    def _build_prompt(self, history: list[dict]) -> str:
        messages = [{"role": "system", "content": config.SYSTEM_PROMPT}, *history]
        return self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

    def stream_sentences(self, history: list[dict]) -> Iterator[str]:
        """会話履歴(role/content の辞書リスト)から応答を生成し、文単位で逐次yieldする。

        同期・ブロッキング処理なので、呼び出し側で別スレッドから呼ぶこと。
        """
        prompt = self._build_prompt(history)
        buffer = ""
        for response in stream_generate(
            self.model,
            self.tokenizer,
            prompt=prompt,
            max_tokens=config.LLM_MAX_TOKENS,
            sampler=self._sampler,
            logits_processors=self._logits_processors,
        ):
            buffer += response.text

            buffer, repeated = _detect_and_trim_repetition(buffer)
            if repeated:
                print(f"[llm] 同じ文字列の繰り返しを検知したため生成を打ち切りました: {buffer!r}")
                break

            match = _SENTENCE_END_RE.search(buffer)
            while match:
                sentence = buffer[: match.end()].strip()
                buffer = buffer[match.end():]
                if sentence:
                    yield sentence
                match = _SENTENCE_END_RE.search(buffer)
        remainder = buffer.strip()
        if remainder:
            yield remainder
