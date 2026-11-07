"""資源の調停（Arbiter と Lease）の貸し出し・返却・待ちのテスト。"""

import logging
import threading
import time
from collections.abc import Callable

import pytest

from osekkai.adapters.fake import FakeDeviceSet
from osekkai.adapters.fake.clock import FakeClock
from osekkai.core.arbiter import Arbiter, Lease, LeaseState
from osekkai.core.config import MotionSettings
from osekkai.core.motion import MotionService
from osekkai.core.ports import Unavailable
from osekkai.core.types import Priority, Resource, ResourceBusy

V = Resource.VOICE
D = Resource.DISPLAY
N = Resource.NECK
R = Resource.DRIVE


@pytest.fixture
def motion(fakes: FakeDeviceSet, clock: FakeClock) -> MotionService:
    return MotionService(fakes.servos, fakes.motors, MotionSettings(), clock)


@pytest.fixture
def arbiter(clock: FakeClock, motion: MotionService) -> Arbiter:
    return Arbiter(clock, motion)


def _wait_until(predicate: object, timeout: float = 2.0) -> None:
    assert callable(predicate)
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "条件が満たされませんでした"
        time.sleep(0.005)


# --- 貸し出し（4.1、4.2） ---


def test_空いている資源を貸す(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [V, D])
    assert isinstance(lease, Lease)
    assert lease.active
    assert lease.state is LeaseState.ACTIVE
    assert lease.owner == "a"
    assert lease.priority == Priority.INTERACTIVE
    assert lease.resources == frozenset({V, D})
    assert not lease.preempt_requested.is_set()
    assert arbiter.holder(V) == "a"
    assert arbiter.holder(D) == "a"
    assert arbiter.holder(N) is None


def test_同じ資源を2つ目に貸さない(arbiter: Arbiter) -> None:
    arbiter.acquire("a", Priority.INTERACTIVE, [V])
    with pytest.raises(ResourceBusy):
        arbiter.acquire("b", Priority.INTERACTIVE, [V])
    assert arbiter.holder(V) == "a"


def test_同じ持ち主でも同じ資源を2つ目に貸さない(arbiter: Arbiter) -> None:
    arbiter.acquire("a", Priority.INTERACTIVE, [V])
    with pytest.raises(ResourceBusy):
        arbiter.acquire("a", Priority.INTERACTIVE, [V])


def test_低い優先度は高い方の持つ資源で断られる(arbiter: Arbiter) -> None:
    arbiter.acquire("high", Priority.SCHEDULED, [V])
    with pytest.raises(ResourceBusy):
        arbiter.acquire("low", Priority.BACKGROUND, [V])


def test_資源の集合は全部か無しかで貸す(arbiter: Arbiter) -> None:
    arbiter.acquire("a", Priority.INTERACTIVE, [V])
    with pytest.raises(ResourceBusy):
        arbiter.acquire("b", Priority.INTERACTIVE, [V, D])
    # 空いていた DISPLAY は b に貸されていない
    assert arbiter.holder(D) is None
    other = arbiter.acquire("c", Priority.INTERACTIVE, [D])
    assert other.active


def test_空の資源の集合は受け付けない(arbiter: Arbiter) -> None:
    with pytest.raises(ValueError):
        arbiter.acquire("a", Priority.INTERACTIVE, [])


def test_負の待ち時間は受け付けない(arbiter: Arbiter) -> None:
    with pytest.raises(ValueError):
        arbiter.acquire("a", Priority.INTERACTIVE, [V], wait=-1.0)


# --- 返却 ---


def test_返却は何度呼んでもよい(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    lease.release()
    lease.release()
    assert not lease.active
    assert lease.state is LeaseState.RELEASED
    assert arbiter.holder(V) is None


def test_文脈を出ると返す(arbiter: Arbiter) -> None:
    with arbiter.acquire("a", Priority.INTERACTIVE, [V]) as lease:
        assert arbiter.holder(V) == "a"
    assert lease.state is LeaseState.RELEASED
    assert arbiter.holder(V) is None


def test_例外で文脈を出ても返す(arbiter: Arbiter) -> None:
    def use() -> None:
        with arbiter.acquire("a", Priority.INTERACTIVE, [V]):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        use()
    assert arbiter.holder(V) is None


def test_終わったリースは再び有効にならない(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    lease.release()
    # 同じ資源がほかに貸されても、古いリースは無効のまま
    again = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    assert again.active
    assert not lease.active
    lease.release()  # 古いリースの返却は、新しいリースに響かない
    assert again.active
    assert arbiter.holder(V) == "a"


def test_返した資源はほかの機能が借りられる(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    lease.release()
    other = arbiter.acquire("b", Priority.INTERACTIVE, [V])
    assert other.active
    assert arbiter.holder(V) == "b"


# --- 待ち（4.7） ---


def test_待ち時間が0なら直ちに使用中で断る(arbiter: Arbiter, clock: FakeClock) -> None:
    arbiter.acquire("a", Priority.INTERACTIVE, [V])
    start = clock.monotonic()
    with pytest.raises(ResourceBusy):
        arbiter.acquire("b", Priority.INTERACTIVE, [V], wait=0.0)
    assert clock.monotonic() == start


def test_待てば返されたときに借りる(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    result: list[Lease] = []
    errors: list[BaseException] = []

    def wait_and_acquire() -> None:
        try:
            result.append(arbiter.acquire("b", Priority.INTERACTIVE, [V], wait=5.0))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=wait_and_acquire)
    thread.start()
    time.sleep(0.05)
    assert thread.is_alive()  # まだ待っている
    lease.release()
    thread.join(2.0)
    assert not thread.is_alive()
    assert errors == []
    assert result[0].active
    assert arbiter.holder(V) == "b"


def test_待っても返されなければ使用中で断る(arbiter: Arbiter) -> None:
    arbiter.acquire("a", Priority.INTERACTIVE, [V])
    start = time.monotonic()
    with pytest.raises(ResourceBusy):
        arbiter.acquire("b", Priority.INTERACTIVE, [V], wait=0.05)
    assert time.monotonic() - start < 1.0
    assert arbiter.holder(V) == "a"


def test_偽の時計が期限を過ぎていれば待たずに断る(arbiter: Arbiter, clock: FakeClock) -> None:
    arbiter.acquire("a", Priority.INTERACTIVE, [V])
    errors: list[BaseException] = []

    def wait_and_acquire() -> None:
        try:
            arbiter.acquire("b", Priority.INTERACTIVE, [V], wait=30.0)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=wait_and_acquire)
    thread.start()
    time.sleep(0.05)
    clock.advance(31.0)
    # 時刻が進んだことに、ほかの資源の返却で気づかせる
    arbiter.acquire("c", Priority.INTERACTIVE, [D]).release()
    thread.join(2.0)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ResourceBusy)


def test_待っている間に空いた資源の一部だけでは貸さない(arbiter: Arbiter) -> None:
    first = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    second = arbiter.acquire("b", Priority.INTERACTIVE, [D])
    result: list[Lease] = []

    def wait_and_acquire() -> None:
        result.append(arbiter.acquire("c", Priority.INTERACTIVE, [V, D], wait=5.0))

    thread = threading.Thread(target=wait_and_acquire)
    thread.start()
    time.sleep(0.02)
    first.release()
    time.sleep(0.05)
    assert result == []
    assert arbiter.holder(V) is None  # 一部だけ先に押さえない
    second.release()
    thread.join(2.0)
    assert result[0].resources == frozenset({V, D})


# --- 空いたときの動き（2.5、4.8） ---


def test_走行が空いたらモーターを止める(
    arbiter: Arbiter, motion: MotionService, fakes: FakeDeviceSet
) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [R])
    motion.drive(10, 50)
    assert fakes.motors.speeds == (50, 50)
    lease.release()
    assert fakes.motors.speeds == (0, 0)
    assert fakes.motors.calls[-1].op == "set_speeds"
    assert fakes.motors.calls[-1].args == (0, 0)


def test_首が空いたら正面に戻す(
    arbiter: Arbiter, motion: MotionService, fakes: FakeDeviceSet
) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [N])
    motion.look(40, 20)
    lease.release()
    assert fakes.servos.angles["pan"] == 0.0
    assert fakes.servos.angles["tilt"] == 0.0


def test_首が空いてもほかの機能の走行のステアリングは変えない(
    arbiter: Arbiter, motion: MotionService, fakes: FakeDeviceSet
) -> None:
    driving = arbiter.acquire("b", Priority.REMOTE, [R])
    motion.drive(20, 50)
    neck = arbiter.acquire("a", Priority.INTERACTIVE, [N])
    motion.look(40, 20)
    neck.release()
    assert fakes.servos.angles == {"pan": 0.0, "tilt": 0.0, "steering": 20.0}
    assert driving.active


def test_走行も首も持たないリースの返却ではモーターも首も触らない(
    arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    arbiter.acquire("a", Priority.INTERACTIVE, [V, D]).release()
    assert fakes.motors.calls == []
    assert fakes.servos.calls == []


def test_走行だけのリースの返却では首を戻さない(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    arbiter.acquire("a", Priority.INTERACTIVE, [R]).release()
    assert fakes.servos.calls == []
    assert fakes.motors.ops() == ["set_speeds"]


def test_返却を何度呼んでも止めるのは1回(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [R])
    lease.release()
    lease.release()
    assert fakes.motors.ops() == ["set_speeds"]


def test_モーターや首が使えなくても返却は終わる(
    clock: FakeClock, fakes: FakeDeviceSet, caplog: pytest.LogCaptureFixture
) -> None:
    motion = MotionService(
        Unavailable("servos", "missing"), Unavailable("motors", "missing"), MotionSettings(), clock
    )
    arbiter = Arbiter(clock, motion)
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [R, N])
    with caplog.at_level(logging.WARNING, logger="osekkai"):
        lease.release()
    assert not lease.active
    assert arbiter.holder(R) is None
    assert arbiter.holder(N) is None
    assert "DeviceUnavailable" in caplog.text
    assert arbiter.acquire("b", Priority.INTERACTIVE, [R, N]).active


def test_書き込みに失敗しても返却は終わる(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    fakes.motors.fail_on.add("set_speeds")
    fakes.servos.fail_on.add("set_angle")
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [R, N])
    lease.release()
    assert arbiter.holder(R) is None
    assert arbiter.holder(N) is None


# --- 空いたときの合図 ---


def test_資源が空いたら合図を呼ぶ(arbiter: Arbiter) -> None:
    called: list[str] = []
    arbiter.on_resource_freed(D, lambda: called.append("display"))
    arbiter.on_resource_freed(V, lambda: called.append("voice"))
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [D])
    assert called == []
    lease.release()
    lease.release()
    assert called == ["display"]


def test_合図の受け手の例外で調停は止まらない(
    arbiter: Arbiter, caplog: pytest.LogCaptureFixture
) -> None:
    called: list[str] = []

    def broken() -> None:
        raise RuntimeError("hidden message")

    arbiter.on_resource_freed(D, broken)
    arbiter.on_resource_freed(D, lambda: called.append("second"))
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [D])
    with caplog.at_level(logging.WARNING, logger="osekkai"):
        lease.release()
    assert called == ["second"]
    assert "RuntimeError" in caplog.text
    assert "hidden message" not in caplog.text
    assert arbiter.acquire("b", Priority.INTERACTIVE, [D]).active


def test_合図の受け手は調停のロックの外で呼ばれ借り直せる(arbiter: Arbiter) -> None:
    leases: list[Lease] = []
    arbiter.on_resource_freed(D, lambda: leases.append(arbiter.acquire("idle", Priority.IDLE, [D])))
    done = threading.Event()

    def run() -> None:
        arbiter.acquire("a", Priority.INTERACTIVE, [D]).release()
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert done.wait(2.0), "合図の受け手が借り直すと行き詰まった"
    assert leases[0].active
    assert arbiter.holder(D) == "idle"


def test_合図のときには資源が空いている(arbiter: Arbiter) -> None:
    holders: list[str | None] = []
    arbiter.on_resource_freed(R, lambda: holders.append(arbiter.holder(R)))
    arbiter.acquire("a", Priority.INTERACTIVE, [R]).release()
    assert holders == [None]


# --- 一括の取り上げ（3.5） ---


def test_持ち主のすべてのリースを直ちに取り上げる(
    arbiter: Arbiter, motion: MotionService, fakes: FakeDeviceSet
) -> None:
    freed: list[Resource] = []

    def recorder(resource: Resource) -> Callable[[], None]:
        return lambda: freed.append(resource)

    for resource in Resource:
        arbiter.on_resource_freed(resource, recorder(resource))
    first = arbiter.acquire("bad", Priority.INTERACTIVE, [R, V])
    second = arbiter.acquire("bad", Priority.INTERACTIVE, [N])
    other = arbiter.acquire("good", Priority.INTERACTIVE, [D])
    motion.drive(5, 40)
    motion.look(30, 10)

    arbiter.revoke_all("bad")

    assert first.state is LeaseState.REVOKED
    assert second.state is LeaseState.REVOKED
    assert not first.active
    assert not second.active
    assert other.active
    assert arbiter.holder(R) is None
    assert arbiter.holder(V) is None
    assert arbiter.holder(N) is None
    assert arbiter.holder(D) == "good"
    assert fakes.motors.speeds == (0, 0)
    # 首は正面に戻る。ステアリングはモーターを止めたうえでそのまま
    assert fakes.servos.angles == {"pan": 0.0, "tilt": 0.0, "steering": 5.0}
    assert sorted(freed, key=lambda r: r.value) == sorted([R, V, N], key=lambda r: r.value)


def test_取り上げたリースの返却は何もしない(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    lease = arbiter.acquire("bad", Priority.INTERACTIVE, [R])
    arbiter.revoke_all("bad")
    lease.release()
    assert lease.state is LeaseState.REVOKED
    assert fakes.motors.ops() == ["set_speeds"]


def test_持ち主がリースを持たなければ取り上げは何もしない(arbiter: Arbiter) -> None:
    lease = arbiter.acquire("a", Priority.INTERACTIVE, [V])
    arbiter.revoke_all("nobody")
    assert lease.active


def test_取り上げると待っていた機能が借りる(arbiter: Arbiter) -> None:
    arbiter.acquire("bad", Priority.INTERACTIVE, [V])
    result: list[Lease] = []
    thread = threading.Thread(
        target=lambda: result.append(arbiter.acquire("b", Priority.INTERACTIVE, [V], wait=5.0))
    )
    thread.start()
    time.sleep(0.02)
    arbiter.revoke_all("bad")
    thread.join(2.0)
    assert result[0].active
    _wait_until(lambda: arbiter.holder(V) == "b")


# --- 優先度による中断と取り上げ（4.3〜4.5） ---


def _state(lease: Lease) -> LeaseState:
    """状態を読み直す（mypy が前の確認で型を絞り込まないように）。"""
    return lease.state


class _Requester:
    """別のスレッドで acquire を呼び、結果か例外を残す。"""

    def __init__(
        self,
        arbiter: Arbiter,
        owner: str,
        priority: int,
        resources: list[Resource],
        wait: float = 0.0,
    ) -> None:
        self.result: list[Lease] = []
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(
            target=self._run, args=(arbiter, owner, priority, resources, wait), daemon=True
        )
        self.thread.start()

    def _run(
        self, arbiter: Arbiter, owner: str, priority: int, resources: list[Resource], wait: float
    ) -> None:
        try:
            self.result.append(arbiter.acquire(owner, priority, resources, wait=wait))
        except BaseException as error:
            self.errors.append(error)

    def join(self) -> None:
        self.thread.join(2.0)
        assert not self.thread.is_alive(), "高い方が借りられないまま止まった"


def test_低い方に中断を1回だけ知らせ返されたら高い方に貸す(arbiter: Arbiter) -> None:
    called: list[threading.Thread] = []
    low = arbiter.acquire(
        "low",
        Priority.BACKGROUND,
        [V, D],
        on_preempt=lambda: called.append(threading.current_thread()),
    )
    high = _Requester(arbiter, "high", Priority.SCHEDULED, [V])

    _wait_until(lambda: len(called) == 1)
    assert low.preempt_requested.is_set()
    assert low.state is LeaseState.PREEMPT_REQUESTED
    assert low.active  # 返すまでは有効のまま
    assert called[0] is not high.thread  # 借りに来たスレッドでは呼ばない
    time.sleep(0.1)
    assert high.thread.is_alive()  # wait=0 でも、返されるのを待つ
    assert len(called) == 1

    low.release()
    high.join()
    assert high.errors == []
    assert high.result[0].active
    assert _state(low) is LeaseState.RELEASED
    assert arbiter.holder(V) == "high"
    assert arbiter.holder(D) is None
    assert len(called) == 1


def test_2秒で返されなければ取り上げて高い方に貸す(arbiter: Arbiter, clock: FakeClock) -> None:
    low = arbiter.acquire("low", Priority.BACKGROUND, [V])
    high = _Requester(arbiter, "high", Priority.SCHEDULED, [V])
    assert low.preempt_requested.wait(2.0)

    clock.advance(Arbiter.PREEMPT_GRACE_S - 0.1)
    time.sleep(0.15)
    assert low.state is LeaseState.PREEMPT_REQUESTED  # まだ 2 秒たっていない
    assert high.thread.is_alive()

    clock.advance(0.1)
    high.join()
    assert high.errors == []
    assert high.result[0].active
    assert _state(low) is LeaseState.REVOKED
    assert not low.active
    assert arbiter.holder(V) == "high"


def test_偽の時計が進まなくても実際の待ちの上限で取り上げる(
    arbiter: Arbiter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(arbiter, "_PREEMPT_REAL_LIMIT_S", 0.2)
    low = arbiter.acquire("low", Priority.BACKGROUND, [V])
    high = _Requester(arbiter, "high", Priority.SCHEDULED, [V])
    high.join()
    assert high.result[0].active
    assert low.state is LeaseState.REVOKED


def test_走行の中断で返されるのを待たずに直ちにモーターを止める(
    arbiter: Arbiter, motion: MotionService, fakes: FakeDeviceSet
) -> None:
    low = arbiter.acquire("low", Priority.REMOTE, [R])
    motion.drive(0, 50)
    assert fakes.motors.speeds == (50, 50)
    high = _Requester(arbiter, "high", Priority.SCHEDULED, [R, N])

    _wait_until(lambda: "stop_now" in fakes.motors.ops())
    assert fakes.motors.speeds == (0, 0)
    assert low.state is LeaseState.PREEMPT_REQUESTED  # まだ返されていない
    assert high.thread.is_alive()

    low.release()
    high.join()
    assert high.result[0].active
    assert fakes.motors.ops().count("stop_now") == 1


def test_走行を持たないリースの中断では非常停止しない(
    arbiter: Arbiter, fakes: FakeDeviceSet
) -> None:
    low = arbiter.acquire("low", Priority.BACKGROUND, [V])
    high = _Requester(arbiter, "high", Priority.SCHEDULED, [V])
    assert low.preempt_requested.wait(2.0)
    low.release()
    high.join()
    assert "stop_now" not in fakes.motors.ops()


def test_知らせの受け手が止まっても調停は止まらない(arbiter: Arbiter, clock: FakeClock) -> None:
    stuck = threading.Event()
    entered = threading.Event()

    def block() -> None:
        entered.set()
        stuck.wait(5.0)

    try:
        low = arbiter.acquire("low", Priority.BACKGROUND, [V], on_preempt=block)
        high = _Requester(arbiter, "high", Priority.SCHEDULED, [V])
        assert entered.wait(2.0)
        # 受け手が止まっている間も、ほかの貸し出しは進む
        assert arbiter.acquire("other", Priority.INTERACTIVE, [D]).active
        assert arbiter.holder(V) == "low"
        clock.advance(Arbiter.PREEMPT_GRACE_S)
        high.join()
        assert high.result[0].active
        assert low.state is LeaseState.REVOKED
    finally:
        stuck.set()


def test_知らせの受け手の例外は型だけ残して調停は続く(
    arbiter: Arbiter, caplog: pytest.LogCaptureFixture
) -> None:
    def broken() -> None:
        raise RuntimeError("hidden message")

    with caplog.at_level(logging.WARNING, logger="osekkai"):
        low = arbiter.acquire("low", Priority.BACKGROUND, [V], on_preempt=broken)
        high = _Requester(arbiter, "high", Priority.SCHEDULED, [V])
        _wait_until(lambda: "RuntimeError" in caplog.text)
        low.release()
        high.join()
    assert high.result[0].active
    assert "hidden message" not in caplog.text


def test_同じ優先度の持ち主には中断を知らせない(arbiter: Arbiter) -> None:
    called: list[str] = []
    low = arbiter.acquire("a", Priority.INTERACTIVE, [V], on_preempt=lambda: called.append("a"))
    with pytest.raises(ResourceBusy):
        arbiter.acquire("b", Priority.INTERACTIVE, [V])
    time.sleep(0.02)
    assert called == []
    assert not low.preempt_requested.is_set()
    assert low.state is LeaseState.ACTIVE


def test_持ち主の一部でも低くなければ中断を知らせない(arbiter: Arbiter) -> None:
    called: list[str] = []
    low = arbiter.acquire("low", Priority.BACKGROUND, [V], on_preempt=lambda: called.append("low"))
    same = arbiter.acquire("same", Priority.INTERACTIVE, [D])
    with pytest.raises(ResourceBusy):
        arbiter.acquire("b", Priority.INTERACTIVE, [V, D])
    time.sleep(0.02)
    assert called == []
    assert not low.preempt_requested.is_set()
    assert low.state is LeaseState.ACTIVE
    assert same.state is LeaseState.ACTIVE


def test_2度目の高い要求では知らせ直さない(arbiter: Arbiter, fakes: FakeDeviceSet) -> None:
    called: list[str] = []
    low = arbiter.acquire("low", Priority.BACKGROUND, [R], on_preempt=lambda: called.append("low"))
    first = _Requester(arbiter, "high1", Priority.SCHEDULED, [R])
    _wait_until(lambda: len(called) == 1)
    second = _Requester(arbiter, "high2", Priority.SCHEDULED, [R])
    time.sleep(0.1)
    assert called == ["low"]
    assert fakes.motors.ops().count("stop_now") == 1

    low.release()
    first.join()
    second.join()
    # 片方が借り、もう片方は同じ優先度の持ち主に断られる（wait=0）
    assert len(first.result) + len(second.result) == 1
    errors = first.errors + second.errors
    assert len(errors) == 1
    assert isinstance(errors[0], ResourceBusy)
