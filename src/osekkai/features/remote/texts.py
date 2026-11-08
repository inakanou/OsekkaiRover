"""ラジコンの話す言葉・LCD の文字と顔・端末の知らせの文。

要件 2.3、2.4、3.2、4.7、5.5、6.4、7.3、7.4、8.2、8.3、9.1〜9.3、10.2〜10.4、11.1〜11.3、12.2。
すべて要件の文のとおり。ここはほかの部品を読まない（ほかの部品が texts を読む）。
ここの言葉と、話させた文字は、ログと self-check に出さない（10.5、13.6）。
"""

from __future__ import annotations

from typing import Final

from osekkai.core.types import Face

# 話す言葉
SAY_HORNS: Final = (
    "ブッブー！",
    "プップー！",
    "どいて どいてー！",
    "しゅっぱつ しんこう！",
)  # ここから 1 つを選ぶ（7.3）
SAY_DANGER_OBSTACLE: Final = "おっと、なにか あるよ"  # 前の物で止まった（5.5）
SAY_DANGER_CLIFF: Final = "あぶない！とまるよ"  # 段差で止まった（5.5）
SAY_PHOTO_CHEESE: Final = "はい、チーズ！"  # 撮る前（9.1）
SAY_PHOTO_DONE: Final = "とれたよ！"  # 1 枚以上撮れた（9.2）
SAY_PHOTO_FAILED: Final = "ごめんね、うまく とれなかった"  # 1 枚も撮れなかった（9.3）

# 端末の知らせ
MSG_WRONG_PIN: Final = "ばんごうが ちがうよ"  # 2.3
MSG_LOCKED: Final = "しばらく まってね"  # 2.4
MSG_OTHER_OPERATOR: Final = "いまは ほかの ひとが そうさ ちゅう"  # 3.2
MSG_OUT_OF_HOURS: Final = "いまは ラジコンの じかんじゃないよ"  # 11.1、11.4
MSG_QUIET: Final = MSG_OUT_OF_HOURS  # おやすみも同じ文（11.2）
MSG_NO_CLOCK: Final = "いまは つかえないよ"  # 時刻が確かでない（11.3）
MSG_BUSY: Final = "ローバーは いま ほかの ことを しているよ。ちょっと まってね"  # 12.2
MSG_STREAM_FULL: Final = "いまは みられないよ"  # 映像を見る端末が多い（8.2）
MSG_CAMERA_UNAVAILABLE: Final = "カメラが つかえないよ"  # 8.3
MSG_TEXT_EMPTY: Final = "もじを いれてね"  # 10.3
MSG_TALKING: Final = "いま おはなし ちゅう。ちょっと まってね"  # 10.4
MSG_DANGER_OBSTACLE: Final = "まえに なにか あるよ"  # 5.5
MSG_DANGER_CLIFF: Final = "だんさが あるよ！"  # 5.5
MSG_BACK_GUIDE: Final = "うしろは みえないよ。ゆっくり さがるよ"  # 後退のボタンの近く（6.4）

# LCD の文字（どれも 16 セルに収まる。中央に置くのはアダプタ）
LCD_IDLE: Final = "ﾗｼﾞｺﾝ"  # 止まっている（4.7）
LCD_FORWARD: Final = "ﾌﾞｰﾝ!"  # 前に進む（4.7）
LCD_BACK: Final = "ﾊﾞｯｸ"  # 後退（4.7）
LCD_STOP: Final = "ｽﾄｯﾌﾟ!"  # 危険で止まった（5.5）
LCD_DANCE: Final = "ﾀﾞﾝｽ!"  # 7.4
LCD_PHOTO: Final = "ﾊｲ ﾁｰｽﾞ!"  # 9.1
LCD_TALK: Final = "ｵﾊﾅｼ ﾁｭｳ"  # 10.2

# LCD の 2 行目の顔
FACE_DRIVE: Final = Face.HAPPY  # 走行の表示は笑顔（4.7）
FACE_STOP: Final = Face.CALL  # 呼びかけの顔（5.5）
FACE_DANCE: Final = Face.HAPPY  # 笑顔（7.4）
FACE_TALK: Final = Face.CALL  # 呼びかけの顔（10.2）
