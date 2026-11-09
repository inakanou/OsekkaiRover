"""操作する端末を 1 台にする（要件 3.1〜3.4）。

偽の時計で 10 秒を進める。端末は印の文字で見分ける（テストの印は短い仮の文字）。
"""

import threading
from datetime import datetime

from remote_fakes import FakeClock

from osekkai.features.remote.operator_slot import ClaimResult, OperatorSlot

A = "tab-a"
B = "tab-b"
THREADS = 20


def _slot() -> tuple[OperatorSlot, FakeClock]:
    clock = FakeClock()
    return OperatorSlot(clock), clock


class _BackClock:
    """単調な時刻を戻せる時計（時計のおかしさを起こす）。"""

    def __init__(self) -> None:
        self.mono = 1000.0

    def now(self) -> datetime:
        return datetime(2026, 1, 1).astimezone()

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.mono += seconds

    def is_synchronized(self) -> bool:
        return True


# --- 操作する端末になる（3.1） ---


def test_始めは操作する端末がない() -> None:
    slot, _ = _slot()
    assert slot.holder() is None


def test_空のときに操作した端末が操作する端末になる() -> None:
    slot, _ = _slot()
    assert slot.claim(A) is ClaimResult.MINE
    assert slot.holder() == A


def test_操作する端末はもう一度操作しても_MINE() -> None:
    slot, _ = _slot()
    slot.claim(A)
    assert slot.claim(A) is ClaimResult.MINE
    assert slot.holder() == A


# --- ほかの端末は操作できない（3.2） ---


def test_ほかの端末が操作する端末のときは_OTHER() -> None:
    slot, _ = _slot()
    slot.claim(A)
    assert slot.claim(B) is ClaimResult.OTHER
    assert slot.holder() == A


def test_空にならない間はほかの端末は何度でも_OTHER() -> None:
    slot, clock = _slot()
    slot.claim(A)
    for _ in range(5):
        clock.advance(1.0)
        assert slot.claim(B) is ClaimResult.OTHER
    assert slot.holder() == A


# --- 10 秒で交代（3.3） ---


def test_最後の操作から_10_秒たつと空になる() -> None:
    slot, clock = _slot()
    slot.claim(A)
    clock.advance(OperatorSlot.IDLE_SECONDS)
    assert slot.expire() is True
    assert slot.holder() is None


def test_10_秒より前は空にならない() -> None:
    slot, clock = _slot()
    slot.claim(A)
    clock.advance(9.99)
    assert slot.expire() is False
    assert slot.holder() == A


def test_空になったらほかの端末が操作する端末になれる() -> None:
    slot, clock = _slot()
    slot.claim(A)
    clock.advance(10.0)
    slot.expire()
    assert slot.claim(B) is ClaimResult.MINE
    assert slot.holder() == B
    assert slot.claim(A) is ClaimResult.OTHER


def test_空のときの_expire_は_False() -> None:
    slot, clock = _slot()
    clock.advance(60.0)
    assert slot.expire() is False


def test_空にした後の_expire_は_False() -> None:
    slot, clock = _slot()
    slot.claim(A)
    clock.advance(10.0)
    assert slot.expire() is True
    assert slot.expire() is False


def test_操作する端末の操作は_10_秒を延ばす() -> None:
    slot, clock = _slot()
    slot.claim(A)
    clock.advance(8.0)
    slot.claim(A)
    clock.advance(8.0)
    assert slot.expire() is False
    assert slot.holder() == A
    clock.advance(2.0)
    assert slot.expire() is True


def test_ほかの端末の操作は_10_秒を延ばさない() -> None:
    slot, clock = _slot()
    slot.claim(A)
    clock.advance(8.0)
    slot.claim(B)
    clock.advance(2.0)
    assert slot.expire() is True
    assert slot.holder() is None


def test_時計が戻ったら空にする() -> None:
    clock = _BackClock()
    slot = OperatorSlot(clock)
    slot.claim(A)
    clock.mono -= 1.0
    assert slot.expire() is True
    assert slot.holder() is None


# --- ログインが無効になった（3.4） ---


def test_操作する端末を落とすと空になる() -> None:
    slot, _ = _slot()
    slot.claim(A)
    assert slot.drop(A) is True
    assert slot.holder() is None
    assert slot.claim(B) is ClaimResult.MINE


def test_ほかの端末を落としても操作する端末は変わらない() -> None:
    slot, _ = _slot()
    slot.claim(A)
    assert slot.drop(B) is False
    assert slot.holder() == A


def test_空のときに落としても_False() -> None:
    slot, _ = _slot()
    assert slot.drop(A) is False


def test_空になった後に落としても_False() -> None:
    slot, clock = _slot()
    slot.claim(A)
    clock.advance(10.0)
    slot.expire()
    assert slot.drop(A) is False


# --- いくつものスレッドから ---


def test_同時に操作してもひとつだけが_MINE() -> None:
    slot, _ = _slot()
    start = threading.Barrier(THREADS)
    results: list[ClaimResult] = []
    lock = threading.Lock()

    def claim(i: int) -> None:
        start.wait()
        result = slot.claim(f"tab-{i}")
        with lock:
            results.append(result)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(ClaimResult.MINE) == 1
    assert results.count(ClaimResult.OTHER) == THREADS - 1
    assert slot.holder() is not None
