"""LCD の知らせと、戻し忘れの確認（5.4、5.5、7.1、7.3）。

道具は基盤の実行時のディレクトリに `notice.json` を置き、基盤の `NoticeBoard` がそれを読んで
待機画面の行の代わりに出す。形は基盤の `osekkai.core.notice.Notice.from_dict` が受け付けるもの
（キーは schema・rows・until だけ）にする。道具は osekkai を読み込まないので、形はここにも持つ。

- 版の知らせ: 1 行目に版の表示、2 行目に普段の版か、試しと普段の版の短い名前。20 秒で消える
- 戻し忘れ: 試しの版を始めてから 3 時間を過ぎたら、2 行目だけの知らせを期限なしで書く

ログには版の名前を出さない（固定の文言だけ）。
"""

from __future__ import annotations

import contextlib
import logging
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from rover_deploy.common import Running, Target, version_label, write_json_file
from rover_deploy.layout import ensure_private_dir
from rover_deploy.state import DeployState

logger = logging.getLogger(__name__)

NOTICE_SCHEMA = 1
NOTICE_FILE_NAME = "notice.json"
VERSION_NOTICE_S = 20  # 版の知らせの期限（起動の数秒を含めて、表示はおよそ 10 秒）
REMIND_AFTER_H = 3  # 試しの版を始めてから、戻し忘れを知らせるまでの時間
LCD_CELLS = 16  # LCD の 1 行のセルの数

EVERYDAY_TEXT = "ふだんのばん"
TRIAL_PREFIX = "ためし ふだん:"
SAMPLE_SHORT = "さんぷる"
NO_EVERYDAY = "なし"
REMINDER_TEXT = "ためしのばん もどしてね"

# 濁点・半濁点（分けた形）。LCD では別のセルになる
_VOICING_MARKS = frozenset({"゙", "゚"})


@dataclass(frozen=True)
class Notice:
    """LCD の知らせ。None の行は待機画面のまま。"""

    rows: tuple[str | None, str | None]
    until: float | None  # エポック秒。None は消されるまで


def notice_to_dict(notice: Notice) -> dict[str, object]:
    """基盤の `Notice.from_dict` が受け付ける形にする。"""
    return {"schema": NOTICE_SCHEMA, "rows": list(notice.rows), "until": notice.until}


# --- セルの数 ---


def lcd_cells(text: str) -> int:
    """LCD のセルの数。濁点・半濁点の付いた字は 2 セル（基盤と同じく NFKC の後に分ける）。"""
    return len(unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text)))


def _cut(text: str, cells: int = LCD_CELLS) -> str:
    """`cells` 以内に切り詰める（字の途中で切らない）。"""
    out = ""
    for ch in text:
        if lcd_cells(out + ch) > cells:
            break
        out += ch
    return out


# --- 版の知らせ ---


def _short_name(everyday: Target | None) -> str:
    if everyday is None:
        return NO_EVERYDAY
    if everyday.kind == "sample":
        return SAMPLE_SHORT
    return version_label(everyday)


def _version_row(target: Target) -> str:
    """1 行目。ブランチの名前が長ければ名前のほうを縮め、`@` とコミットの識別子は残す。"""
    label = version_label(target)
    if target.ref_kind != "branch" or target.ref is None or lcd_cells(label) <= LCD_CELLS:
        return _cut(label)
    suffix = label[len(target.ref) :]  # "@abc1234"
    return _cut(target.ref, LCD_CELLS - lcd_cells(suffix)) + suffix


def version_notice(target: Target, everyday: Target | None, now_epoch: float) -> Notice:
    """入れた版の知らせ（7.1、5.4、7.3）。"""
    row1 = _version_row(target)
    if everyday is not None and target == everyday:
        row2 = EVERYDAY_TEXT
    else:
        row2 = _cut(TRIAL_PREFIX + _short_name(everyday))
    return Notice(rows=(row1, row2), until=now_epoch + VERSION_NOTICE_S)


def write_notice(runtime_dir: Path, notice: Notice) -> None:
    """知らせを 0600 で原子的に書く。ディレクトリがなければ 0700 で作る。"""
    ensure_private_dir(runtime_dir)
    write_json_file(runtime_dir / NOTICE_FILE_NAME, notice_to_dict(notice))


def clear_notice(runtime_dir: Path) -> None:
    """知らせを消す。なくてもよい。"""
    with contextlib.suppress(FileNotFoundError):
        (runtime_dir / NOTICE_FILE_NAME).unlink()


# --- 戻し忘れ ---


def remind(state: DeployState, running: Running, now_epoch: float) -> bool:
    """戻し忘れを知らせるときか（5.5）。

    新しい基盤が動いていて、今の版が普段の版でなく、試しを始めてから
    `REMIND_AFTER_H` 時間たっていれば True。
    """
    if running is not Running.CORE:
        return False
    if state.current is None or state.trial_since is None:
        return False
    if state.current == state.everyday:
        return False
    return now_epoch - state.trial_since >= REMIND_AFTER_H * 3600


def send_reminder(runtime_dir: Path, state: DeployState, now_epoch: float) -> DeployState:
    """戻し忘れを知らせる。2 行目だけの知らせを期限なしで書き、警告を 1 行出す。

    知らせた時刻を入れた状態を返す（保存は呼ぶ側が行う）。
    """
    write_notice(runtime_dir, Notice(rows=(None, REMINDER_TEXT), until=None))
    logger.warning("試しの版が長く入っています。普段の版に戻してください")
    return replace(state, reminded_at=now_epoch)
