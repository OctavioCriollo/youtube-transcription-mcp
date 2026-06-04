from __future__ import annotations

import re
import textwrap
from collections import Counter
from collections.abc import Iterable

_WORD_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_PUNCT_NO_SPACE_BEFORE = re.compile(r"^[,.;:!?%)]")


def normalize_spaces(text: str) -> str:
    return " ".join(str(text).split())


def word_tokens(text: str) -> list[str]:
    return [m.group(0).casefold() for m in _WORD_TOKEN_RE.finditer(text)]


def token_counter(text: str) -> Counter[str]:
    return Counter(word_tokens(text))


def smart_join(words: Iterable[str]) -> str:
    out = ""
    for raw_word in words:
        raw = str(raw_word)
        stripped = raw.strip()
        if not stripped:
            continue
        if not out:
            out = stripped
        elif raw[:1].isspace():
            out += raw
        elif _PUNCT_NO_SPACE_BEFORE.match(stripped):
            out += stripped
        else:
            out += " " + stripped
    return normalize_spaces(out)


def wrap_lines(text: str, max_cpl: int = 42) -> list[str]:
    """Wrap text without dropping content.

    This function intentionally has no max-lines parameter. Subtitle builders
    must split cues before wrapping if they need a 2-line limit.
    """
    cleaned = normalize_spaces(text)
    if not cleaned:
        return [""]
    wrapped = textwrap.wrap(
        cleaned,
        width=max_cpl,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapped or [cleaned]
