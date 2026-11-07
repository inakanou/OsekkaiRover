"""追従の部品（1.2〜1.8）。"""

import math

import pytest

from osekkai.features.presence.tracking import (
    PAN_LIMIT,
    TILT_MAX,
    TILT_MIN,
    FaceBox,
    Tracker,
    TrackOutput,
    largest,
)

STEP = 0.1  # 見る流れの回りの間


def _adopt(tracker: Tracker, face: FaceBox, now: float = 0.0) -> TrackOutput:
    """2 回続けて見せて、顔を採らせる。2 回目の出力を返す。"""
    tracker.step(face, now)
    return tracker.step(face, now + STEP)


# --- FaceBox ---


def test_顔の位置は範囲の中なら作れる() -> None:
    box = FaceBox(-1.0, 1.0, 0.0)
    assert (box.x, box.y, box.size) == (-1.0, 1.0, 0.0)
    assert FaceBox(1.0, -1.0, 1.0).size == 1.0


@pytest.mark.parametrize(
    ("x", "y", "size"),
    [
        (1.01, 0.0, 0.5),
        (-1.01, 0.0, 0.5),
        (0.0, 1.5, 0.5),
        (0.0, -1.5, 0.5),
        (0.0, 0.0, -0.1),
        (0.0, 0.0, 1.1),
        (math.nan, 0.0, 0.5),
        (0.0, math.inf, 0.5),
        (0.0, 0.0, math.nan),
    ],
)
def test_顔の位置が範囲の外なら誤り(x: float, y: float, size: float) -> None:
    with pytest.raises(ValueError) as info:
        FaceBox(x, y, size)
    # 誤りの文言に値を入れない
    assert "1.01" not in str(info.value)
    assert "1.5" not in str(info.value)


def test_顔の位置は変えられない() -> None:
    box = FaceBox(0.0, 0.0, 0.5)
    with pytest.raises(AttributeError):
        box.x = 0.5  # type: ignore[misc]


def test_最も大きく写った顔を選ぶ() -> None:
    small = FaceBox(0.5, 0.0, 0.1)
    big = FaceBox(-0.5, 0.0, 0.4)
    mid = FaceBox(0.0, 0.0, 0.2)
    assert largest([small, big, mid]) is big
    assert largest([small]) is small


def test_顔がなければ選ばない() -> None:
    assert largest([]) is None


# --- 採る（1.2） ---


def test_1_回だけ見つかっても採らない() -> None:
    tracker = Tracker()
    out = tracker.step(FaceBox(0.5, 0.5, 0.3), 0.0)
    assert out.seen is False
    assert (out.pan, out.tilt, out.gaze) == (0.0, 0.0, 0.0)


def test_2_回続けて見つかったら採る() -> None:
    tracker = Tracker()
    out = _adopt(tracker, FaceBox(0.5, 0.0, 0.3))
    assert out.seen is True
    assert out.lost_for == 0.0
    assert out.pan == pytest.approx(4.0)


def test_1_回だけの見つかりは間に途切れがあれば数えない() -> None:
    tracker = Tracker()
    face = FaceBox(0.5, 0.0, 0.3)
    tracker.step(face, 0.0)
    tracker.step(None, 0.1)
    out = tracker.step(face, 0.2)
    assert out.seen is False
    assert out.pan == 0.0
    out = tracker.step(face, 0.3)
    assert out.seen is True


# --- 首（1.3、1.7） ---


def test_真ん中に近いときは首を動かさない() -> None:
    tracker = Tracker()
    out = _adopt(tracker, FaceBox(0.15, 0.2, 0.3))
    assert out.seen is True
    assert (out.pan, out.tilt) == (0.0, 0.0)
    out = tracker.step(FaceBox(-0.15, -0.2, 0.3), 0.2)
    assert (out.pan, out.tilt) == (0.0, 0.0)


def test_左右だけ真ん中から外れたら左右だけ動かす() -> None:
    tracker = Tracker()
    out = _adopt(tracker, FaceBox(0.5, 0.1, 0.3))
    assert out.pan == pytest.approx(4.0)
    assert out.tilt == 0.0


def test_上下だけ真ん中から外れたら上下だけ動かす() -> None:
    tracker = Tracker()
    out = _adopt(tracker, FaceBox(0.1, 0.5, 0.3))
    assert out.pan == 0.0
    assert out.tilt == pytest.approx(-3.0)
    out = tracker.step(FaceBox(0.1, -0.5, 0.3), 0.2)
    assert out.tilt == pytest.approx(0.0)


def test_首は回りごとに少しずつ足していく() -> None:
    tracker = Tracker()
    face = FaceBox(-0.5, -0.5, 0.3)
    out = _adopt(tracker, face)
    assert (out.pan, out.tilt) == pytest.approx((-4.0, 3.0))
    out = tracker.step(face, 0.2)
    assert (out.pan, out.tilt) == pytest.approx((-8.0, 6.0))


def test_左右は_40_度までに収める() -> None:
    tracker = Tracker()
    now = 0.0
    out = _adopt(tracker, FaceBox(1.0, 0.0, 0.3), now)
    for _ in range(20):
        now += STEP
        out = tracker.step(FaceBox(1.0, 0.0, 0.3), now)
    assert out.pan == PAN_LIMIT == 40.0
    for _ in range(40):
        now += STEP
        out = tracker.step(FaceBox(-1.0, 0.0, 0.3), now)
    assert out.pan == -PAN_LIMIT


def test_上下は_マイナス_15_から_30_度に収める() -> None:
    tracker = Tracker()
    now = 0.0
    out = _adopt(tracker, FaceBox(0.0, -1.0, 0.3), now)
    for _ in range(20):
        now += STEP
        out = tracker.step(FaceBox(0.0, -1.0, 0.3), now)
    assert out.tilt == TILT_MAX == 30.0
    for _ in range(40):
        now += STEP
        out = tracker.step(FaceBox(0.0, 1.0, 0.3), now)
    assert out.tilt == TILT_MIN == -15.0


# --- 見失う（1.5） ---


def test_見えない間も_2_5_秒までは見つけているまま() -> None:
    tracker = Tracker()
    _adopt(tracker, FaceBox(0.5, 0.0, 0.3), 0.0)  # 最後に見えたのは 0.1
    out = tracker.step(None, 2.5)
    assert out.seen is True
    assert out.lost_for == pytest.approx(2.4)
    assert out.pan == pytest.approx(4.0)  # 見えない間は首を動かさない


def test_2_5_秒見えなければ見失う() -> None:
    tracker = Tracker()
    _adopt(tracker, FaceBox(0.5, 0.0, 0.3), 0.0)
    out = tracker.step(None, 2.6)
    assert out.seen is False
    assert out.lost_for == pytest.approx(2.5)
    assert out.gaze == 0.0
    assert out.pan == pytest.approx(4.0)  # 5 秒までは首はそのまま


def test_見えない間に_1_回見えれば見つけているまま続ける() -> None:
    tracker = Tracker()
    face = FaceBox(0.5, 0.0, 0.3)
    _adopt(tracker, face, 0.0)
    tracker.step(None, 2.0)
    out = tracker.step(face, 2.1)
    assert out.seen is True
    assert out.lost_for == 0.0
    out = tracker.step(None, 4.5)
    assert out.seen is True


def test_見失った後は_また_2_回続けて見つからないと採らない() -> None:
    tracker = Tracker()
    face = FaceBox(0.5, 0.0, 0.3)
    _adopt(tracker, face, 0.0)
    tracker.step(None, 3.0)  # 見失う
    out = tracker.step(face, 3.1)
    assert out.seen is False
    assert out.pan == pytest.approx(4.0)
    out = tracker.step(face, 3.2)
    assert out.seen is True
    assert out.lost_for == 0.0
    assert out.pan == pytest.approx(8.0)


# --- 正面に戻る（1.6） ---


def test_5_秒までは首を正面に戻さない() -> None:
    tracker = Tracker()
    _adopt(tracker, FaceBox(1.0, 1.0, 0.3), 0.0)
    out = tracker.step(None, 5.1)  # 見えなくなって 5.0 秒
    assert out.lost_for == pytest.approx(5.0)
    assert (out.pan, out.tilt) == pytest.approx((8.0, -6.0))


def test_5_秒を過ぎたら回りごとに_0_8_倍で正面へ戻す() -> None:
    tracker = Tracker()
    _adopt(tracker, FaceBox(1.0, 1.0, 0.3), 0.0)
    out = tracker.step(None, 5.2)
    assert (out.pan, out.tilt) == pytest.approx((6.4, -4.8))
    out = tracker.step(None, 5.3)
    assert (out.pan, out.tilt) == pytest.approx((5.12, -3.84))


def test_正面に近くなったら_0_にそろえる() -> None:
    tracker = Tracker()
    _adopt(tracker, FaceBox(1.0, 1.0, 0.3), 0.0)
    now = 5.2
    out = tracker.step(None, now)
    for _ in range(30):
        now += STEP
        out = tracker.step(None, now)
    assert (out.pan, out.tilt) == (0.0, 0.0)
    assert out.seen is False


def test_一度も見ていなければ首は正面のまま() -> None:
    tracker = Tracker()
    out = tracker.step(None, 0.0)
    assert out == TrackOutput(pan=0.0, tilt=0.0, gaze=0.0, seen=False, lost_for=0.0)
    out = tracker.step(None, 100.0)
    assert (out.pan, out.tilt, out.seen) == (0.0, 0.0, False)


# --- 目の向き（1.4） ---


def test_目は顔の位置と首の向きから決める() -> None:
    tracker = Tracker()
    out = _adopt(tracker, FaceBox(0.5, 0.0, 0.3))
    # 0.5 × 0.6 + 4 / 40
    assert out.gaze == pytest.approx(0.4)


def test_見えない間で見つけているときは最後の顔の位置で目を決める() -> None:
    tracker = Tracker()
    _adopt(tracker, FaceBox(0.5, 0.0, 0.3))
    out = tracker.step(None, 1.0)
    assert out.gaze == pytest.approx(0.4)


def test_目の向きは_マイナス_1_から_1_に収める() -> None:
    tracker = Tracker()
    now = 0.0
    out = _adopt(tracker, FaceBox(1.0, 0.0, 0.3), now)
    for _ in range(10):
        now += STEP
        out = tracker.step(FaceBox(1.0, 0.0, 0.3), now)
    assert out.gaze == 1.0
    tracker = Tracker()
    now = 0.0
    out = _adopt(tracker, FaceBox(-1.0, 0.0, 0.3), now)
    for _ in range(10):
        now += STEP
        out = tracker.step(FaceBox(-1.0, 0.0, 0.3), now)
    assert out.gaze == -1.0


def test_採る前の目は正面() -> None:
    tracker = Tracker()
    out = tracker.step(FaceBox(1.0, 0.0, 0.3), 0.0)
    assert out.gaze == 0.0


# --- 反転（1.8） ---


def test_左右の反転で左右の向きが逆になる() -> None:
    tracker = Tracker(pan_reverse=True)
    out = _adopt(tracker, FaceBox(0.5, 0.5, 0.3))
    assert out.pan == pytest.approx(-4.0)
    assert out.tilt == pytest.approx(-3.0)  # 上下はそのまま
    # 目は顔の向き（反転の前）で決めるので、反転なしと同じ
    assert out.gaze == pytest.approx(0.4)


def test_上下の反転で上下の向きが逆になる() -> None:
    tracker = Tracker(tilt_reverse=True)
    out = _adopt(tracker, FaceBox(0.5, 0.5, 0.3))
    assert out.tilt == pytest.approx(3.0)
    assert out.pan == pytest.approx(4.0)  # 左右はそのまま


def test_上下の反転でも範囲は首の角度で収める() -> None:
    tracker = Tracker(tilt_reverse=True)
    now = 0.0
    out = _adopt(tracker, FaceBox(0.0, 1.0, 0.3), now)
    for _ in range(20):
        now += STEP
        out = tracker.step(FaceBox(0.0, 1.0, 0.3), now)
    assert out.tilt == TILT_MAX


def test_目の向きの反転で目の向きが逆になる() -> None:
    tracker = Tracker(gaze_reverse=True)
    out = _adopt(tracker, FaceBox(0.5, 0.0, 0.3))
    assert out.pan == pytest.approx(4.0)  # 首はそのまま
    assert out.gaze == pytest.approx(-0.4)


def test_左右と目の両方の反転() -> None:
    tracker = Tracker(pan_reverse=True, gaze_reverse=True)
    out = _adopt(tracker, FaceBox(0.5, 0.0, 0.3))
    assert out.pan == pytest.approx(-4.0)
    assert out.gaze == pytest.approx(-0.4)


def test_見失ったら反転していても目は正面() -> None:
    tracker = Tracker(gaze_reverse=True)
    _adopt(tracker, FaceBox(0.5, 0.0, 0.3))
    out = tracker.step(None, 3.0)
    assert out.gaze == 0.0
