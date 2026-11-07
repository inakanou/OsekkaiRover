"""音声の入口の決まった言葉と LCD の文字（1.1、1.4、3.4、4.2、5.2、6.1、6.3、6.4）。

ここの言葉はログに出さない（話す言葉と LCD の文字は記録しない決まり）。
"""

from typing import Final

# 話す言葉
SAY_CALL: Final = "なあに？"  # 初めての聞き取り（1.1）
SAY_FOLLOW_UP: Final = "うん、なあに？"  # 続けての聞き取り（4.2）
SAY_NOT_HEARD: Final = (  # 空・失敗（6.1、6.2）
    "ごめんね、きこえなかった。もういちど ボタンを おして はなしてね"
)
SAY_UNKNOWN: Final = "ごめんね、まだ わからないんだ"  # どこにも当てはまらない（3.4）
SAY_SLEEP_TIME: Final = "いまは ねる じかんだよ。また あした おはなし しようね"  # 時間帯の外（5.2）
SAY_PREPARING: Final = "ちょっと まってね。じゅんび しているよ"  # 準備中（6.3）
SAY_UNAVAILABLE: Final = "いまは おはなし できないんだ"  # 使えない（6.4）

# LCD の文字（16 セルに収まる半角カタカナ）
LCD_LISTENING: Final = "ｷｲﾃﾙﾖ"  # 1.1
LCD_SPEAK: Final = "ﾊﾅｼﾃﾈ!"  # 1.2 録音の間（マイクが音を取り始めてから）
LCD_THINKING: Final = "ｶﾝｶﾞｴﾁｭｳ"  # 1.4
LCD_SLEEP_TIME: Final = "ｵﾔｽﾐ ｼﾞｶﾝ"  # 5.2

# 聞き取りの既定のヒント（サンプルと同じ）。登録の語句をこの後ろに足す
DEFAULT_HINT: Final = "ローバー。おはよう。おやすみ。できたよ。なんで？あそぼう。すき。たのしい。"
