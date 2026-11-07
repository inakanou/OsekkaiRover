"""追従の部品（1.2〜1.8）。

顔の位置から、首の角度（左右と上下）と待機画面の目の向きを決める。
2 回続けて見つかったら採り、2.5 秒見えなければ見失い、5 秒を過ぎたら首を少しずつ正面へ戻す。
時計は持たない。時刻（秒）は呼ぶ側が渡す。純粋な Python で、ハードもログも使わない。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

ADOPT_COUNT: Final = 2  # この回数続けて見つかったら採る（1.2）
DEAD_X: Final = 0.15  # 左右の不感帯（1.3）
DEAD_Y: Final = 0.2  # 上下の不感帯
PAN_GAIN: Final = 8.0  # 回りごとに足す左右の角度の係数
TILT_GAIN: Final = 6.0  # 回りごとに足す上下の角度の係数
PAN_LIMIT: Final = 40.0  # 左右は ±40 度（1.7）
TILT_MIN: Final = -15.0  # 上下は −15〜30 度
TILT_MAX: Final = 30.0
LOST_AFTER: Final = 2.5  # この秒数見えなければ見失う（1.5）
HOME_AFTER: Final = 5.0  # この秒数を過ぎたら正面へ戻す（1.6）
HOME_RATE: Final = 0.8  # 回りごとにかける倍率
HOME_SNAP: Final = 1.0  # この角度以下になったら 0 にそろえる
GAZE_X: Final = 0.6  # 目の向きにかける顔の位置の係数（1.4）

MSG_POSITION = "顔の位置は -1 から 1 の数にしてください"
MSG_SIZE = "顔の大きさは 0 から 1 の数にしてください"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _in_range(value: float, low: float, high: float) -> bool:
    return math.isfinite(value) and low <= value <= high


@dataclass(frozen=True)
class FaceBox:
    """映像の中の顔の位置。x・y は真ん中から −1〜1（右と下が +）、size は幅の割合（0〜1）。"""

    x: float
    y: float
    size: float

    def __post_init__(self) -> None:
        if not (_in_range(self.x, -1.0, 1.0) and _in_range(self.y, -1.0, 1.0)):
            raise ValueError(MSG_POSITION)
        if not _in_range(self.size, 0.0, 1.0):
            raise ValueError(MSG_SIZE)


@dataclass(frozen=True)
class TrackOutput:
    """1 回の回りの結果。pan・tilt は首の角度（度）、gaze は目の向き（−1〜1）。

    seen は顔を見つけているか、lost_for は最後に見えてからの秒数（見えているときは 0）。
    """

    pan: float
    tilt: float
    gaze: float
    seen: bool
    lost_for: float


def largest(faces: Iterable[FaceBox]) -> FaceBox | None:
    """最も大きく写った顔を選ぶ（1.2）。顔がなければ None。"""
    return max(faces, key=lambda face: face.size, default=None)


class Tracker:
    """顔の位置から、首の角度と目の向きを少しずつ決める。

    反転の設定（1.8）は符号で当てる。左右と上下の反転は首の角度の向きを、
    目の向きの反転は目の向きを逆にする。範囲は首の角度で収める。
    """

    def __init__(
        self, *, pan_reverse: bool = False, tilt_reverse: bool = False, gaze_reverse: bool = False
    ) -> None:
        self._pan_sign = -1.0 if pan_reverse else 1.0
        self._tilt_sign = -1.0 if tilt_reverse else 1.0
        self._gaze_sign = -1.0 if gaze_reverse else 1.0
        self._pan = 0.0
        self._tilt = 0.0
        self._streak = 0  # 続けて見つかった回数（採る前）
        self._seen = False
        self._last_x = 0.0  # 最後に見えた顔の左右の位置
        self._last_seen_at: float | None = None  # 最後に見えた時刻（一度も採っていなければ None）

    def step(self, face: FaceBox | None, now: float) -> TrackOutput:
        """1 回の回りの顔（なければ None）と今の時刻（秒）から、首と目を決める。"""
        if face is None:
            self._streak = 0
        elif self._seen:
            self._follow(face, now)
        else:
            self._streak += 1
            if self._streak >= ADOPT_COUNT:
                self._seen = True
                self._follow(face, now)

        lost_for = 0.0 if self._last_seen_at is None else now - self._last_seen_at
        if self._seen and lost_for >= LOST_AFTER:
            self._seen = False
            self._streak = 0
        if not self._seen and lost_for > HOME_AFTER:
            self._pan = self._ease(self._pan)
            self._tilt = self._ease(self._tilt)

        gaze = 0.0
        if self._seen:
            # 首の向き（反転の前の向き）と顔の位置から、顔のある方へ目を向ける
            head = self._pan_sign * self._pan / PAN_LIMIT
            gaze = self._gaze_sign * _clamp(self._last_x * GAZE_X + head, -1.0, 1.0)
        return TrackOutput(
            pan=self._pan, tilt=self._tilt, gaze=gaze, seen=self._seen, lost_for=lost_for
        )

    def _follow(self, face: FaceBox, now: float) -> None:
        if abs(face.x) > DEAD_X:
            pan = self._pan + self._pan_sign * face.x * PAN_GAIN
            self._pan = _clamp(pan, -PAN_LIMIT, PAN_LIMIT)
        if abs(face.y) > DEAD_Y:
            tilt = self._tilt - self._tilt_sign * face.y * TILT_GAIN
            self._tilt = _clamp(tilt, TILT_MIN, TILT_MAX)
        self._last_x = face.x
        self._last_seen_at = now

    @staticmethod
    def _ease(value: float) -> float:
        eased = value * HOME_RATE
        return 0.0 if abs(eased) <= HOME_SNAP else eased
