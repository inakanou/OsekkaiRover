"""呼び名の置き換え（NameGuard）と、返事の整え（shape_reply）。

要件 1.4、3.3、4.1〜4.3。どちらも入出力だけの関数で、ログは出さない。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Final

NAME_MARK: Final = "{name}"
DEFAULT_CALL_NAME: Final = "きみ"
# 会話の AI が全角で書いたときの目印
_WIDE_MARK: Final = "｛name｝"

MAX_SENTENCES: Final = 2
MAX_CHARS: Final = 120

# カタカナ（ァ〜ヶ、ヽヾ）をひらがなにそろえる。1 文字ずつなので位置は変わらない
_KATA_TO_HIRA: Final = {code: code - 0x60 for code in range(0x30A1, 0x30F7)} | {
    0x30FD: 0x309D,
    0x30FE: 0x309E,
}

_MARKS = re.compile(re.escape(NAME_MARK) + "|" + re.escape(_WIDE_MARK))
# 文の終わり（続く区切りはまとめて同じ文に入れる）
_SENTENCE = re.compile(r"[^。！？!?]*[。！？!?]+")
# 行の頭の「-」（箇条書きの頭）
_LINE_HEAD_DASH = re.compile(r"^[ \t　]*-+", re.MULTILINE)
_REMOVE_CHARS: Final = frozenset("*#•")
# 絵文字につく見えない文字（異体字の選び・つなぎ・肌の色・旗のタグ）
_EMOJI_PARTS: Final = frozenset(
    [0xFE0E, 0xFE0F, 0x200D, *range(0x1F3FB, 0x1F400), *range(0xE0020, 0xE0080)]
)


def _fold_kana(text: str) -> str:
    return text.translate(_KATA_TO_HIRA)


class NameGuard:
    """子どもの言葉の呼び名を目印に、返事の目印を呼び名にする。"""

    def __init__(self, call_name: str, variants: Sequence[str]) -> None:
        name = call_name.strip()
        self._call_name = name or DEFAULT_CALL_NAME
        forms: set[str] = set()
        # 呼び名が空（「きみ」）なら置き換えない
        if name and _fold_kana(name) != _fold_kana(DEFAULT_CALL_NAME):
            forms.add(_fold_kana(name))
            # 空や空白だけの別の形は飛ばす
            forms.update(_fold_kana(v.strip()) for v in variants if v.strip())
        # 長い形から先に当てる
        ordered = sorted(forms, key=lambda f: (-len(f), f))
        self._pattern = re.compile("|".join(map(re.escape, ordered))) if ordered else None

    def mask(self, text: str) -> str:
        """呼び名と別の形を目印にする。照合はかなをそろえて、置き換えは元の文字列に行う。"""
        if self._pattern is None:
            return text
        parts: list[str] = []
        last = 0
        for found in self._pattern.finditer(_fold_kana(text)):
            parts.append(text[last : found.start()])
            parts.append(NAME_MARK)
            last = found.end()
        parts.append(text[last:])
        return "".join(parts)

    def fill(self, text: str) -> str:
        """目印（全角も）を呼び名にする。呼び名が空なら「きみ」。"""
        return text.replace(NAME_MARK, self._call_name).replace(_WIDE_MARK, self._call_name)


def _drop_symbols(text: str) -> str:
    text = _LINE_HEAD_DASH.sub("", text)
    return "".join(
        ch
        for ch in text
        if ch not in _REMOVE_CHARS
        and ord(ch) not in _EMOJI_PARTS
        and unicodedata.category(ch) != "So"
    )


def _cut(text: str, limit: int) -> str:
    """limit 文字で切る。目印の途中で切れるときは、目印の前で切る。"""
    if len(text) <= limit:
        return text
    for found in _MARKS.finditer(text):
        if found.start() < limit < found.end():
            return text[: found.start()]
    return text[:limit]


def shape_reply(text: str) -> str:
    """絵文字・記号・箇条書きの頭を除き、2 文・120 文字までにする。空白だけなら空。"""
    cleaned = _drop_symbols(text)
    # 行は空白でつなぐ（話す言葉なので改行は残さない）
    joined = " ".join(line.strip() for line in cleaned.splitlines() if line.strip())
    sentences = _SENTENCE.findall(joined)
    consumed = sum(len(s) for s in sentences)
    if len(sentences) < MAX_SENTENCES and consumed < len(joined):
        # 区切りのない終わりの部分も 1 文とみなす
        sentences.append(joined[consumed:])
    shaped = "".join(sentences[:MAX_SENTENCES]).strip()
    shaped = _cut(shaped, MAX_CHARS).strip()
    return shaped if shaped.strip() else ""
