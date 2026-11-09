"""3 段のリース（操作・見るだけ・なし）を持つ（要件 8.4、12.2〜12.5）。

偽の文脈が偽のリースを貸す。中断の知らせ（preempt）は、ロックを持たない所から呼ぶ。
偽物は基盤の後始末（止める・首を戻す）を覚えないので、calls に stop や home を期待しない。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

import pytest
from remote_fakes import REMOTE_RESOURCES, FakeClock, FakeContext, FakeLease

from osekkai.core.types import Resource
from osekkai.features.remote.leases import LeaseKeeper, Level

NECK_ONLY = frozenset({Resource.NECK})
JOIN_S = 5.0  # 別のスレッドを待つ上限（実際の秒）


class _Lost:
    """on_lost の呼ばれた回数を数える。"""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def _keeper(on_lost: Callable[[], None] | None = None) -> tuple[LeaseKeeper, FakeContext, _Lost]:
    ctx = FakeContext(FakeClock())
    lost = _Lost()
    keeper = LeaseKeeper(ctx.as_context(), on_lost if on_lost is not None else lost)
    return keeper, ctx, lost


def _same(a: object, b: object) -> bool:
    """同じ物か（基盤のリースの型と偽のリースの型を比べる）。"""
    return a is b


def _not_lost(keeper: LeaseKeeper) -> bool:
    """lost が下りているか（want の後に読み直す）。"""
    return not keeper.lost


def _acquires(ctx: FakeContext) -> list[tuple[object, ...]]:
    return [c for c in ctx.calls if c[0] == "acquire"]


def test_初めはリースを持たない() -> None:
    keeper, ctx, _ = _keeper()
    assert keeper.lease is None
    assert not keeper.lost
    assert ctx.calls == []


def test_操作の段は4つの資源を待たずに借りる() -> None:
    keeper, ctx, _ = _keeper()
    assert keeper.want(Level.OPERATE) is Level.OPERATE
    assert ctx.calls == [("acquire", REMOTE_RESOURCES, 0)]
    assert _same(keeper.lease, ctx.leases[-1])


def test_見るだけの段は首だけを借りる() -> None:
    keeper, ctx, _ = _keeper()
    assert keeper.want(Level.VIEW) is Level.VIEW
    assert ctx.calls == [("acquire", NECK_ONLY, 0)]


def test_同じ段なら借り直さない() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.OPERATE)
    assert keeper.want(Level.OPERATE) is Level.OPERATE
    assert len(_acquires(ctx)) == 1


def test_見るだけから操作へは返してから借り直す() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.VIEW)
    assert keeper.want(Level.OPERATE) is Level.OPERATE
    assert ctx.calls == [
        ("acquire", NECK_ONLY, 0),
        ("release",),
        ("acquire", REMOTE_RESOURCES, 0),
    ]


def test_操作から見るだけへは返してから首だけを借りる() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.OPERATE)
    assert keeper.want(Level.VIEW) is Level.VIEW
    assert ctx.calls == [
        ("acquire", REMOTE_RESOURCES, 0),
        ("release",),
        ("acquire", NECK_ONLY, 0),
    ]
    assert keeper.lease is not None
    assert keeper.lease.resources == NECK_ONLY


def test_見るだけからなしへは返す() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.VIEW)
    assert keeper.want(Level.NONE) is Level.NONE
    assert ctx.calls == [("acquire", NECK_ONLY, 0), ("release",)]
    assert keeper.lease is None


def test_操作からなしへは返す() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.OPERATE)
    keeper.want(Level.NONE)
    assert ctx.calls[-1] == ("release",)
    assert keeper.lease is None


def test_releaseで返す() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.OPERATE)
    keeper.release()
    assert ctx.calls[-1] == ("release",)
    assert keeper.lease is None
    keeper.release()  # 何度呼んでもよい
    assert ctx.calls.count(("release",)) == 1


def test_中断の知らせでon_lostを呼んでからリースを返す() -> None:
    order: list[str] = []
    ctx = FakeContext(FakeClock())
    keeper = LeaseKeeper(ctx.as_context(), lambda: order.append(f"lost:{len(ctx.calls)}"))
    keeper.want(Level.OPERATE)
    lease = ctx.leases[-1]
    lease.preempt()
    # on_lost は返す前（calls は acquire と preempt の 2 つ）
    assert order == ["lost:2"]
    assert ctx.calls == [("acquire", REMOTE_RESOURCES, 0), ("preempt",), ("release",)]
    assert lease.active is False
    assert keeper.lost
    assert keeper.lease is None


def test_中断の知らせで話さない() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.OPERATE)
    ctx.preempt_latest()
    assert not [c for c in ctx.calls if c[0] in ("say", "show")]


def test_on_lostが例外を投げても返す(caplog: pytest.LogCaptureFixture) -> None:
    def boom() -> None:
        raise RuntimeError("hidden")

    keeper, ctx, _ = _keeper(boom)
    keeper.want(Level.OPERATE)
    ctx.preempt_latest()
    assert ctx.calls[-1] == ("release",)
    assert keeper.lost
    assert "RuntimeError" in caplog.text
    assert "hidden" not in caplog.text


def test_on_lostの中から状態を読める() -> None:
    seen: list[object] = []
    ctx = FakeContext(FakeClock())
    keeper: LeaseKeeper

    def on_lost() -> None:
        seen.append((keeper.lost, keeper.lease))

    keeper = LeaseKeeper(ctx.as_context(), on_lost)
    keeper.want(Level.VIEW)
    ctx.preempt_latest()
    assert seen == [(True, None)]


def test_借りられないときはなしにしてlostを立てる() -> None:
    keeper, ctx, lost = _keeper()
    ctx.busy = {Resource.DRIVE}
    assert keeper.want(Level.OPERATE) is Level.NONE
    assert keeper.lease is None
    assert keeper.lost
    assert lost.count == 0  # 持っていないので止めるものはない


def test_操作は届くたびに借り直す() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.OPERATE)
    ctx.preempt_latest()
    ctx.busy = {Resource.DRIVE}
    assert keeper.want(Level.OPERATE) is Level.NONE
    assert keeper.want(Level.OPERATE) is Level.NONE
    assert keeper.lost
    ctx.busy = set()
    assert keeper.want(Level.OPERATE) is Level.OPERATE
    assert _not_lost(keeper)
    assert len(_acquires(ctx)) == 4


def test_見るだけの借り直しは5秒あける() -> None:
    keeper, ctx, _ = _keeper()
    clock = ctx.clock
    ctx.busy = {Resource.NECK}
    assert keeper.want(Level.VIEW) is Level.NONE
    clock.advance(4.9)
    assert keeper.want(Level.VIEW) is Level.NONE
    assert len(_acquires(ctx)) == 1  # 5 秒たつまで試さない
    clock.advance(0.1)
    assert keeper.want(Level.VIEW) is Level.NONE
    assert len(_acquires(ctx)) == 2
    ctx.busy = set()
    clock.advance(4.0)
    assert keeper.want(Level.VIEW) is Level.NONE
    clock.advance(1.0)
    assert keeper.want(Level.VIEW) is Level.VIEW
    assert not keeper.lost
    assert len(_acquires(ctx)) == 3


def test_中断の後の見るだけの借り直しも5秒あける() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.VIEW)
    ctx.preempt_latest()
    assert keeper.want(Level.VIEW) is Level.NONE
    assert len(_acquires(ctx)) == 1
    ctx.clock.advance(5.0)
    assert keeper.want(Level.VIEW) is Level.VIEW
    assert not keeper.lost


def test_見るだけの間隔の中でも操作は試す() -> None:
    keeper, ctx, _ = _keeper()
    ctx.busy = {Resource.NECK}
    keeper.want(Level.VIEW)
    ctx.busy = set()
    assert keeper.want(Level.OPERATE) is Level.OPERATE


def test_なしにするとlostを下ろす() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.OPERATE)
    ctx.preempt_latest()
    assert keeper.want(Level.NONE) is Level.NONE
    assert not keeper.lost


def test_取り上げられたリースに気づいて借り直す() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.OPERATE)
    ctx.leases[-1].revoke()
    assert keeper.lease is None
    assert keeper.want(Level.OPERATE) is Level.OPERATE
    assert len(_acquires(ctx)) == 2
    assert _same(keeper.lease, ctx.leases[-1])


def test_取り上げられた見るだけのリースはlostにして5秒あける() -> None:
    keeper, ctx, _ = _keeper()
    keeper.want(Level.VIEW)
    ctx.leases[-1].revoke()
    assert keeper.want(Level.VIEW) is Level.NONE
    assert keeper.lost
    ctx.clock.advance(5.0)
    assert keeper.want(Level.VIEW) is Level.VIEW


def test_返した前のリースの遅れた知らせは今のリースに触らない() -> None:
    keeper, ctx, lost = _keeper()
    keeper.want(Level.VIEW)
    old = ctx.leases[-1]
    keeper.want(Level.OPERATE)
    # 基盤の別のスレッドが、返す前に出した知らせを遅れて届けたのと同じ
    assert old.on_preempt is not None
    old.on_preempt()
    assert lost.count == 0
    assert not keeper.lost
    assert _same(keeper.lease, ctx.leases[-1])
    assert ctx.leases[-1].active


class _PreemptOnGrant(FakeContext):
    """貸した直後、want が返る前に、別のスレッドから中断を知らせる文脈。"""

    def __init__(self) -> None:
        super().__init__(FakeClock())
        self.thread_done = False

    def acquire(
        self,
        resources: Iterable[Resource],
        *,
        wait: float = 0.0,
        on_preempt: Callable[[], None] | None = None,
    ) -> FakeLease:
        lease = super().acquire(resources, wait=wait, on_preempt=on_preempt)
        thread = threading.Thread(target=lease.preempt, name="remote-test-preempt", daemon=True)
        thread.start()
        thread.join(JOIN_S)
        self.thread_done = not thread.is_alive()
        return lease


def test_wantの途中に別のスレッドから中断が来ても詰まらない() -> None:
    ctx = _PreemptOnGrant()
    lost = _Lost()
    keeper = LeaseKeeper(ctx.as_context(), lost)
    assert keeper.want(Level.OPERATE) is Level.NONE
    assert ctx.thread_done is True  # 中断の受け手は want のロックを待たない
    assert lost.count == 1
    assert keeper.lost
    assert keeper.lease is None
    assert ctx.leases[-1].active is False


def test_別のスレッドの中断と_wantが並んでも詰まらない() -> None:
    keeper, ctx, lost = _keeper()
    for _ in range(50):
        keeper.want(Level.OPERATE)
        lease = ctx.leases[-1]
        thread = threading.Thread(target=lease.preempt, name="remote-test-preempt", daemon=True)
        thread.start()
        keeper.want(Level.VIEW)
        thread.join(JOIN_S)
        assert not thread.is_alive()
        keeper.want(Level.NONE)
        assert keeper.lease is None
