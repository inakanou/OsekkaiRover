"""日課の決まった言葉と LCD の文字、呼び名の差し込み（1.5、1.6、9.1、9.2）。"""

from typing import Final

from osekkai.core.types import Glyph

STAR: Final = chr(Glyph.STAR)  # LCD の星の絵の文字
STARS_MAX: Final = 999  # 画面に出す星の数の上限（9.2）
DEFAULT_CALL_NAME: Final = "きみ"  # 呼び名がないときの代わり（1.6）
NAME_SLOT: Final = "{name}"

LATER_SPEECH: Final = "あとで できたら ボタンを おしてね"
LCD_LATER: Final = "ｱﾄﾃﾞ ﾈ"
LCD_SLEEP: Final = "ｵﾔｽﾐ"


def idle_info(stars: int) -> str:
    """待機画面に出す星の数（例: ★12）。999 を超えたら ★999。"""
    return f"{STAR}{min(stars, STARS_MAX)}"


def lcd_done(stars: int) -> str:
    """できたときの LCD の文字（例: ﾔｯﾀﾈ! ★12）。"""
    return f"ﾔｯﾀﾈ! {idle_info(stars)}"


def fill_name(text: str, call_name: str) -> str:
    """言葉の `{name}` を呼び名にする。ほかの波かっこはそのまま（`str.format` は使わない）。"""
    name = call_name if call_name.strip() else DEFAULT_CALL_NAME
    return text.replace(NAME_SLOT, name)
