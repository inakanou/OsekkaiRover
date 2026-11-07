"""見つけてあいさつの決まった言葉と LCD の文字（3.3〜3.6、3.8、5.1〜5.5）。

覚えた人の言葉は、呼び名の場所（`NAME`）を `fill` で置き換えて使う。
呼び名は `str.format` に通さない（呼び名の中の `{` や `}` をそのまま入れる）。
ここの言葉と呼び名は、ログと self-check に出さない（6.3）。
"""

from __future__ import annotations

from typing import Final

from osekkai.core.types import Glyph

NAME: Final = "{name}"  # 呼び名の場所

# あいさつ（覚えた人。3.3〜3.5）
MORNING_KNOWN: Final = f"おはよう、{NAME}！きょうも よろしくね"
OKAERI_KNOWN: Final = f"{NAME}、おかえり！まってたよ"
HELLO_KNOWN: Final = (
    f"あ、{NAME}だ！",
    f"{NAME}、やっほー！",
    f"{NAME}、あそびに きてくれたの？うれしいな",
)

# あいさつ（知らない人。呼び名を入れない。3.6）
MORNING_UNKNOWN: Final = "おはよう！きょうも よろしくね"
OKAERI_UNKNOWN: Final = "おかえり！まってたよ"
HELLO_UNKNOWN: Final = (
    "あ、みつけた！",
    "やっほー！",
    "あそびに きてくれたの？うれしいな",
)

# 顔を覚えるとき（5.1、5.3〜5.5）
LEARN_SAMPLES: Final = 40  # 集める特徴の数
LEARN_START: Final = f"{NAME}の おかおを おぼえるよ。ローバーの ほうを みてね"
LEARN_TURN: Final = "こんどは すこし よこを むいてみて"
LEARN_DONE: Final = f"おぼえたよ！これからは {NAME}って よぶね"
LEARN_FAILED: Final = "うまく おぼえられなかった。あかるい ところで もういちど やってみてね"

# LCD の文字（3.8、5.2）。どれも 16 セルに収まる
_HEART: Final = chr(Glyph.HEART)
LCD_FOUND: Final = f"{_HEART} ﾐﾂｹﾀ! {_HEART}"
LCD_LEARNED: Final = "ｵﾎﾞｴﾀﾖ!"


def lcd_learning(count: int, total: int = LEARN_SAMPLES) -> str:
    """覚えている間の LCD の文字（「ｵﾎﾞｴﾃﾙﾖ 集めた数/40」。5.2）。"""
    return f"ｵﾎﾞｴﾃﾙﾖ {count}/{total}"


def fill(template: str, call_name: str) -> str:
    """言葉の呼び名の場所に、呼び名を入れる。呼び名のない言葉はそのまま返す。"""
    return template.replace(NAME, call_name)
