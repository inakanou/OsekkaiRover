"""首だけのダンス（要件 7.4、7.6）。

首の向きを変える口・続けてよいかの合図・待ち・踊る前の向きだけで進める。
待ちは偽物で数を記録するだけ。本当には待たない。
"""

from __future__ import annotations

import inspect

import pytest

from osekkai.features.remote.dance import DANCE_STEPS, STEP_SECONDS, DanceStep, run_dance

BACK_TO = (5.0, -10.0)


class Recorder:
    """首の動き・待ち・合図の確かめを、1 つの列に順に記録する。"""

    def __init__(self, go_answers: list[bool] | None = None) -> None:
        self.calls: list[tuple[str, tuple[float, ...]]] = []
        self._answers = go_answers

    def look(self, pan: float, tilt: float) -> None:
        self.calls.append(("look", (pan, tilt)))

    def sleep(self, seconds: float) -> None:
        self.calls.append(("sleep", (seconds,)))

    def go(self) -> bool:
        self.calls.append(("go", ()))
        if self._answers is None:
            return True
        return self._answers.pop(0) if self._answers else False

    def looks(self) -> list[tuple[float, ...]]:
        return [args for name, args in self.calls if name == "look"]

    def sleeps(self) -> list[float]:
        return [args[0] for name, args in self.calls if name == "sleep"]


def _run(rec: Recorder) -> bool:
    return run_dance(rec.look, rec.go, rec.sleep, BACK_TO)


def test_段は_6_つで首を左右_30_度と上_15_度に向ける() -> None:
    assert len(DANCE_STEPS) == 6
    assert [s.pan for s in DANCE_STEPS] == [30.0, -30.0, 30.0, -30.0, 30.0, -30.0]
    assert all(s.tilt == 15.0 for s in DANCE_STEPS)
    assert all(isinstance(s, DanceStep) for s in DANCE_STEPS)


def test_1_段は_0_25_秒() -> None:
    assert STEP_SECONDS == 0.25


def test_最後まで踊ると段の順に首を動かして踊る前の向きに戻す() -> None:
    rec = Recorder()
    assert _run(rec) is True
    assert rec.looks() == [(s.pan, s.tilt) for s in DANCE_STEPS] + [BACK_TO]
    assert rec.sleeps() == [STEP_SECONDS] * 6


def test_段ごとに合図を確かめてから動き_動いたあとに待つ() -> None:
    rec = Recorder()
    _run(rec)
    expected: list[tuple[str, tuple[float, ...]]] = []
    for s in DANCE_STEPS:
        expected += [("go", ()), ("look", (s.pan, s.tilt)), ("sleep", (STEP_SECONDS,))]
    expected += [("go", ()), ("look", BACK_TO)]
    assert rec.calls == expected


@pytest.mark.parametrize("done", [0, 1, 3, 5])
def test_途中で合図が偽になると次の段に進まず首を戻さない(done: int) -> None:
    rec = Recorder([True] * done + [False])
    assert _run(rec) is False
    assert rec.looks() == [(s.pan, s.tilt) for s in DANCE_STEPS[:done]]
    assert BACK_TO not in rec.looks()
    assert rec.calls[-1] == ("go", ())


def test_最後の段のあとに合図が偽なら首を戻さない() -> None:
    rec = Recorder([True] * 6 + [False])
    assert _run(rec) is False
    assert rec.looks() == [(s.pan, s.tilt) for s in DANCE_STEPS]
    assert rec.calls[-1] == ("go", ())


def test_首の口の例外はそのまま呼ぶ側に出る() -> None:
    class Revoked(Exception):
        pass

    def look(pan: float, tilt: float) -> None:
        raise Revoked

    with pytest.raises(Revoked):
        run_dance(look, lambda: True, lambda s: None, BACK_TO)


def test_走行の口を受け取らない() -> None:
    params = list(inspect.signature(run_dance).parameters)
    assert params == ["look", "go", "sleep", "back_to"]
