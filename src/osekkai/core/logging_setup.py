"""ログの形式（Monitoring）。

標準エラーに出す。systemd の journal の下（`$JOURNAL_STREAM` がある）では、
journal が時刻を付けるので時刻なしの形式にする。端末では時刻付きにする。
ここでは手渡し先と形式だけを決める。話した文章などをログに出さない決まり（6.5）は、
ログを書く側が守る。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import TextIO

LOGGER_NAME = "osekkai"
JOURNAL_FORMAT = "%(levelname)s %(name)s: %(message)s"
TERMINAL_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# 自分で付けた手渡し先の印（2 回目の設定で付け替える）
_MARK = "_osekkai"


def configure_logging(
    verbose: bool = False,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> logging.Logger:
    """`osekkai` のロガーに、標準エラーへの手渡し先を 1 つだけ付ける。"""
    source = os.environ if env is None else env
    fmt = JOURNAL_FORMAT if source.get("JOURNAL_STREAM") else TERMINAL_FORMAT

    logger = logging.getLogger(LOGGER_NAME)
    for old in [h for h in logger.handlers if getattr(h, _MARK, False)]:
        logger.removeHandler(old)
        old.close()

    handler = logging.StreamHandler(stream)  # None なら標準エラー
    handler.setFormatter(logging.Formatter(fmt))
    setattr(handler, _MARK, True)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger
