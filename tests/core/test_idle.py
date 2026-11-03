"""待機画面（IdleScreen）のテスト（1.2、9.1〜9.3、10.2）。"""

import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import JST, FakeClock
from osekkai.core.arbiter import Arbiter
from osekkai.core.config import MotionSettings
from osekkai.core.display import DisplayService
from osekkai.core.idle import IDLE_OWNER, IdleScreen, IdleState
from osekkai.core.lcd_text import to_lcd_codes
from osekkai.core.motion import MotionService
from osekkai.core.types import Face, Glyph, Priority, Resource

D = Resource.DISPLAY
START = datetime(2026, 3, 7, 8, 5, 30, tzinfo=JST)


def _row(text: str) -> tuple[int, ...]:
    codes = to_lcd_codes(text)[:16]
    return tuple(codes) + (0x20,) * (16 - len(codes))


def _text(row: tuple[int, ...]) -> str:
    return "".join(chr(c) for c in row)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "条件が満たされませんでした"
        time.sleep(0.005)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def arbiter(fakes: FakeDeviceSet, clock: FakeClock) -> Arbiter:
    motion = MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)
    return Arbiter(clock, motion, display=DisplayService(fakes.display))


@pytest.fixture
def idle(arbiter: Arbiter, clock: FakeClock) -> Iterator[IdleScreen]:
    screen = IdleScreen(arbiter, clock)
    yield screen
    screen.stop()


def _state(screen: IdleScreen) -> IdleState:
    # 性質を直に比べると、mypy が 1 回目の比較で型を狭めてしまう
    return screen.state


def _face_row(face: Face, offset: int) -> tuple[int, ...]:
    text = face.text()
    cells = len(to_lcd_codes(text))
    return _row(" " * ((16 - cells) // 2 + offset) + text)


# --- 表示（1.2、9.1、10.2） ---


def test_始めると画面を借りて日付と時刻と顔を出す(
    idle: IdleScreen, arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    idle.start(run_thread=False)
    assert arbiter.holder(D) == IDLE_OWNER
    assert arbiter.lease_for(D) is not None
    assert arbiter.lease_for(D).priority == Priority.IDLE  # type: ignore[union-attr]
    assert _state(idle) is IdleState.SHOWING
    assert fakes.display.rows[0] == _row("03/07 08:05")
    assert fakes.display.rows[1] == _face_row(Face.NORMAL, 0)


def test_分が変わると1行目が変わる(
    idle: IdleScreen, clock: FakeClock, fakes: FakeDeviceSet
) -> None:
    idle.start(run_thread=False)
    clock.advance(20)  # 08:05:50。分は同じ
    writes = len(fakes.display.calls)
    idle.tick()
    assert fakes.display.rows[0] == _row("03/07 08:05")
    assert len(fakes.display.calls) == writes  # 変わらなければ書かない
    clock.advance(15)  # 08:06:05
    idle.tick()
    assert fakes.display.rows[0] == _row("03/07 08:06")
    # 2 行目は変わらないので書き直さない
    assert [(c.op, c.args) for c in fakes.display.calls[writes:]] == [
        ("write_line", (0, _row("03/07 08:06")))
    ]


def test_時刻が確かでなければ時刻の代わりに線を出す(
    idle: IdleScreen, clock: FakeClock, fakes: FakeDeviceSet
) -> None:
    clock.set_synchronized(False)
    idle.start(run_thread=False)
    assert fakes.display.rows[0] == _row("03/07 --:--")
    clock.set_synchronized(True)
    idle.tick()
    assert fakes.display.rows[0] == _row("03/07 08:05")


def test_スレッドが毎秒たしかめて分の変化を出す(
    arbiter: Arbiter, clock: FakeClock, fakes: FakeDeviceSet
) -> None:
    screen = IdleScreen(arbiter, clock, interval=0.01)
    screen.start()
    try:
        running = screen.is_running
        assert running
        clock.advance(60)
        _wait_until(lambda: fakes.display.rows[0] == _row("03/07 08:06"))
    finally:
        began = time.monotonic()
        screen.stop()
    assert time.monotonic() - began < 0.5
    assert not screen.is_running
    assert arbiter.holder(D) is None


def test_止めるとすぐに戻り画面を返す(arbiter: Arbiter, clock: FakeClock) -> None:
    # 実際の 1 秒を待っていても、止める合図ですぐに起きる
    screen = IdleScreen(arbiter, clock)
    screen.start()
    began = time.monotonic()
    screen.stop()
    assert time.monotonic() - began < 0.5
    assert arbiter.holder(D) is None
    # 止めた後に画面が空いても借り直さない
    lease = arbiter.acquire("feature", Priority.INTERACTIVE, [D])
    lease.release()
    assert arbiter.holder(D) is None


# --- 短い情報と顔（9.2、9.3） ---


def test_短い情報を右寄せで出す(idle: IdleScreen, fakes: FakeDeviceSet) -> None:
    idle.start(run_thread=False)
    idle.set_info("ｱﾒ")
    assert fakes.display.rows[0] == _row("03/07 08:05   ｱﾒ")
    idle.set_info(None)
    assert fakes.display.rows[0] == _row("03/07 08:05")


def test_短い情報は空きのセルに切り詰める(idle: IdleScreen, fakes: FakeDeviceSet) -> None:
    idle.start(run_thread=False)
    # 時刻との間に 1 セル空け、残りの 4 セルに収める
    idle.set_info("ABCDEFGH")
    assert fakes.display.rows[0] == _row("03/07 08:05 ABCD")
    # 濁点は 1 セルとして数え、かなと濁点の間では切らない
    idle.set_info("ABCガ")
    assert fakes.display.rows[0] == _row("03/07 08:05  ABC")
    idle.set_info("ABガ")
    assert fakes.display.rows[0] == _row("03/07 08:05 ABガ")


def test_短い情報は最後に呼んだものを出す(idle: IdleScreen, fakes: FakeDeviceSet) -> None:
    idle.start(run_thread=False)
    idle.set_info("A")
    idle.set_info("B")
    assert _text(fakes.display.rows[0]).endswith("    B")


def test_顔と視線を反映する(idle: IdleScreen, fakes: FakeDeviceSet) -> None:
    idle.start(run_thread=False)
    idle.set_face(Face.HAPPY)
    assert fakes.display.rows[1] == _face_row(Face.HAPPY, 0)
    assert chr(Glyph.HEART) in _text(fakes.display.rows[1])
    idle.set_face(Face.NORMAL, 1.0)
    assert fakes.display.rows[1] == _face_row(Face.NORMAL, 4)
    idle.set_face(Face.NORMAL, -1.0)
    assert fakes.display.rows[1] == _face_row(Face.NORMAL, -4)
    idle.set_face(Face.NORMAL, 0.5)
    assert fakes.display.rows[1] == _face_row(Face.NORMAL, 2)


@pytest.mark.parametrize("face", list(Face))
def test_視線の範囲の外は端に収め16セルを超えない(
    idle: IdleScreen, fakes: FakeDeviceSet, face: Face
) -> None:
    idle.start(run_thread=False)
    idle.set_face(face, 5.0)
    assert fakes.display.rows[1] == _face_row(face, 4)
    assert to_lcd_codes(face.text())[-1] in fakes.display.rows[1]
    idle.set_face(face, -5.0)
    assert fakes.display.rows[1] == _face_row(face, -4)
    idle.set_face(face, float("nan"))
    assert fakes.display.rows[1] == _face_row(face, 0)


def test_退いている間の変更は戻ったときに出す(
    idle: IdleScreen, arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    idle.start(run_thread=False)
    lease = arbiter.acquire("feature", Priority.INTERACTIVE, [D], wait=1.0)
    lease.display.show("HELLO", "WORLD")
    idle.set_info("X")
    idle.set_face(Face.CALL)
    assert fakes.display.rows[0] == _row("HELLO")
    lease.release()
    assert fakes.display.rows[0] == _row("03/07 08:05    X")
    assert fakes.display.rows[1] == _face_row(Face.CALL, 0)


# --- 退くことと戻ること（1.2、4.3） ---


def test_機能が画面を借りるとすぐに退き返すと戻る(
    idle: IdleScreen, arbiter: Arbiter, clock: FakeClock, fakes: FakeDeviceSet
) -> None:
    idle.start(run_thread=False)
    began = time.monotonic()
    lease = arbiter.acquire("feature", Priority.BACKGROUND, [D])
    # 2 秒の取り上げを待たず、偽の時計も進めずに借りられる
    assert time.monotonic() - began < 1.0
    assert clock.monotonic() == 1000.0
    assert _state(idle) is IdleState.YIELDED
    lease.display.show("ABC", "DEF")
    idle.tick()  # 退いている間は描かない
    assert fakes.display.rows == [_row("ABC"), _row("DEF")]
    lease.release()
    assert arbiter.holder(D) == IDLE_OWNER
    assert _state(idle) is IdleState.SHOWING
    assert fakes.display.rows == [_row("03/07 08:05"), _face_row(Face.NORMAL, 0)]


def test_始めたときに画面が使われていれば空くまで待つ(
    idle: IdleScreen, arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    lease = arbiter.acquire("feature", Priority.INTERACTIVE, [D])
    idle.start(run_thread=False)
    assert _state(idle) is IdleState.YIELDED
    assert fakes.display.rows == [(), ()]
    lease.release()
    assert _state(idle) is IdleState.SHOWING
    assert fakes.display.rows[0] == _row("03/07 08:05")


def test_取り上げられても空いたら戻る(idle: IdleScreen, arbiter: Arbiter) -> None:
    idle.start(run_thread=False)
    arbiter.revoke_all(IDLE_OWNER)
    assert arbiter.holder(D) == IDLE_OWNER  # 空いた合図で借り直す
    assert _state(idle) is IdleState.SHOWING


def test_空いた合図を逃しても次のたしかめで戻る(
    arbiter: Arbiter, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(arbiter, "on_resource_freed", lambda resource, callback: None)
    screen = IdleScreen(arbiter, clock)
    lease = arbiter.acquire("feature", Priority.INTERACTIVE, [D])
    screen.start(run_thread=False)
    lease.release()
    assert _state(screen) is IdleState.YIELDED
    screen.tick()
    assert _state(screen) is IdleState.SHOWING
    screen.stop()


def test_機能の中断を何度くり返しても戻る(
    idle: IdleScreen, arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    idle.start(run_thread=False)
    for i in range(5):
        lease = arbiter.acquire(f"f{i}", Priority.REMOTE, [D])
        lease.display.show(str(i))
        lease.release()
        assert fakes.display.rows[0] == _row("03/07 08:05")
    assert _state(idle) is IdleState.SHOWING


def test_複数のスレッドから同時に借りても行き詰まらない(arbiter: Arbiter, clock: FakeClock) -> None:
    screen = IdleScreen(arbiter, clock, interval=0.001)
    screen.start()
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for _ in range(20):
                lease = arbiter.acquire(f"f{n}", Priority.REMOTE, [D], wait=2.0)
                lease.display.show("x")
                lease.release()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    screen.stop()
    assert not errors
    assert not any(t.is_alive() for t in threads)


# --- 静かな状態（9.4、9.5 の待機画面の側） ---


def test_静かな間は何も出さずバックライトを消す(
    idle: IdleScreen, arbiter: Arbiter, clock: FakeClock, fakes: FakeDeviceSet
) -> None:
    idle.start(run_thread=False)
    idle.set_quiet(True)
    assert _state(idle) is IdleState.QUIET
    assert idle.quiet
    assert fakes.display.rows == [_row(""), _row("")]
    assert fakes.display.backlight is False
    clock.advance(120)
    idle.tick()
    assert fakes.display.rows == [_row(""), _row("")]
    # 静かな間も、機能は画面を借りられる
    lease = arbiter.acquire("feature", Priority.INTERACTIVE, [D])
    assert _state(idle) is IdleState.QUIET
    lease.release()
    assert arbiter.holder(D) == IDLE_OWNER
    assert fakes.display.rows == [_row(""), _row("")]
    assert fakes.display.backlight is False


def test_静かな状態を終えるとバックライトを点けて戻る(
    idle: IdleScreen, clock: FakeClock, fakes: FakeDeviceSet
) -> None:
    idle.start(run_thread=False)
    idle.set_quiet(True)
    clock.advance(60)
    idle.set_quiet(False)
    assert _state(idle) is IdleState.SHOWING
    assert fakes.display.backlight is True
    assert fakes.display.rows == [_row("03/07 08:06"), _face_row(Face.NORMAL, 0)]


def test_機能がバックライトを消して返してもバックライトを点け直す(
    idle: IdleScreen, arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    idle.start(run_thread=False)
    lease = arbiter.acquire("feature", Priority.INTERACTIVE, [D])
    lease.display.set_backlight(False)
    lease.release()
    assert fakes.display.backlight is True


def test_画面が使えなくても止まらず次に描き直す(
    idle: IdleScreen, fakes: FakeDeviceSet, caplog: pytest.LogCaptureFixture
) -> None:
    fakes.display.fail_on.add("write_line")
    idle.start(run_thread=False)  # 例外を外に出さない
    assert _state(idle) is IdleState.SHOWING
    fakes.display.fail_on.clear()
    idle.tick()
    assert fakes.display.rows[0] == _row("03/07 08:05")
