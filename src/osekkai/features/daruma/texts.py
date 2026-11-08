"""だるまさんの決まった言葉・LCD の文字・唱え言葉・首の角度・待つ長さ。

要件 1.1〜1.5、2.1〜2.8、3.3、3.4、4.1〜4.3、4.6。

ここはほかの部品を読まない（rules が texts を読むので、逆は作らない）。
ここの言葉と呼び名は、ログと self-check に出さない（7.5）。
呼び名は `str.format` に通さない（呼び名の中の `{` や `}` をそのまま入れる）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from osekkai.core.types import Face, Glyph

# 始める語句（1.1）。入口が正規化して含むかを見る
START_PHRASES: Final = ("だるま", "転んだ")

NAME_SLOT: Final = "{name}"  # 呼び名の場所
DEFAULT_CALL_NAME: Final = "きみ"  # 呼び名がないときの代わり（3.4）

# 話す言葉（要件の文のとおり）
SAY_INTRO: Final = (
    "だるまさんが ころんだ、しよう！ローバーが おにだよ。"
    "ローバーから はなれて、スタートの ところに いってね"
)  # 1.2
SAY_GO: Final = f"ローバーに タッチ できたら {NAME_SLOT}の かち。いくよー！"  # 1.3
SAY_NO_CAMERA: Final = "ごめんね、カメラが ないと あそべないんだ"  # 1.4
# 始める前の次の予定（1.5）。途中の 4.3 とは「あとで」の有無が違う
SAY_NEXT_TASK_BEFORE: Final = "あ、つぎの よていの じかんが ちかいね。また あとで あそぼうね"
SAY_KORONDA: Final = "ころんだ！"  # 2.4
SAY_MOVED: Final = "あっ、うごいた！スタートに もどってね"  # 2.7
SAY_WIN: Final = f"タッチ されちゃった！{NAME_SLOT}の かち！やったね！"  # 3.3
SAY_MAX_ROUNDS: Final = "きょうは ここまで！たのしかったね。また あそぼうね"  # 4.1
SAY_NOBODY: Final = "あれ、だれも いないのかな。また あそぼうね"  # 4.2
SAY_NEXT_TASK: Final = "あ、つぎの よていの じかんが ちかいね。また あそぼうね"  # 4.3
SAY_CAMERA_LOST: Final = "ごめんね、カメラが うまく うごかないみたい。また あそぼうね"  # 4.6


@dataclass(frozen=True)
class Chant:
    """唱え言葉。話す言葉と、LCD に出す文字の組（2.1）。"""

    speech: str
    lcd: str


# 唱え言葉の 3 つ（2.1）。毎回ここから選ぶ
CHANTS: Final = (
    Chant("だーるーまーさーんーがー", "ﾀﾞｰﾙｰﾏｰｻｰﾝｶﾞｰ"),
    Chant("だるまさんが", "ﾀﾞﾙﾏｻﾝｶﾞ"),
    Chant("だるまさんがー", "ﾀﾞﾙﾏｻﾝｶﾞｰ"),
)

# LCD の文字（どれも 16 セルに収まる。中央に置くのはアダプタ）
_HEART: Final = chr(Glyph.HEART)  # LCD の ROM に「♥」はないので、絵の文字のハートを使う
LCD_INTRO: Final = ("ﾀﾞﾙﾏｻﾝｶﾞ", "ｺﾛﾝﾀﾞ!")  # 始めの 2 行（1.2）
LCD_WATCH: Final = "ﾐﾃﾙﾖ!"  # 2.4
LCD_MOVED: Final = "ｳｺﾞｲﾀ!"  # 2.7
LCD_WIN: Final = f"{_HEART} ﾀｯﾁ! ｶﾁ! {_HEART}"  # 3.3

# LCD の 2 行目の顔
FACE_CHANT: Final = Face.NORMAL  # 唱える間（1 行目は唱え言葉。2.1）
FACE_WATCH: Final = Face.CALL  # 呼びかけの顔（2.4）
FACE_MOVED: Final = Face.CALL  # 呼びかけの顔（2.7）
FACE_WIN: Final = Face.HAPPY  # 喜んだ顔（3.3）

# 首の角度（pan、tilt）。範囲に収めるのは基盤（5.2）
POSE_AWAY: Final = (70.0, -30.0)  # 横にそらす（2.2）
POSE_FACING: Final = (0.0, 10.0)  # 正面（2.4）

# 待つ長さ（秒）
INTRO_WAIT_S: Final = 5.0  # 始めの言葉の後（1.3）
MOVED_WAIT_S: Final = 5.0  # 動いたの言葉の後（2.8）
PAUSE_MAX_S: Final = 1.2  # 唱えた後の間の最大（2.3）


def say_with_name(template: str, call_name: str) -> str:
    """言葉の `{name}` を呼び名にする。空（空白だけを含む）なら「きみ」（3.4）。"""
    name = call_name if call_name.strip() else DEFAULT_CALL_NAME
    return template.replace(NAME_SLOT, name)
