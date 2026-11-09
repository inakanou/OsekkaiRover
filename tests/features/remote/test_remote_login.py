"""pin の確かめ、まちがいの数え、ログインの印の表（要件 2.1〜2.8）。

pin の例は 4826 を使う。偽の時計で 5 分と 12 時間を進める。
"""

import logging
import threading
from typing import cast

import pytest
from pydantic import SecretStr
from remote_fakes import FakeClock

from osekkai.features.remote.login import LoginGuard, LoginResult, Sessions

PIN = "4826"
WRONG_PIN = "1111"
THREADS = 20


def _guard(pin: str = PIN) -> tuple[LoginGuard, FakeClock]:
    clock = FakeClock()
    return LoginGuard(SecretStr(pin), clock), clock


def _fail(guard: LoginGuard, times: int) -> list[LoginResult]:
    return [guard.try_login(WRONG_PIN) for _ in range(times)]


# --- pin の確かめ（2.2、2.3） ---


def test_正しい_pin_で_OK() -> None:
    guard, _ = _guard()
    assert guard.try_login(PIN) is LoginResult.OK


def test_8_けたの_pin_も確かめる() -> None:
    guard, _ = _guard("48261357")
    assert guard.try_login("48261357") is LoginResult.OK
    assert guard.try_login("48261358") is LoginResult.WRONG


@pytest.mark.parametrize(
    "entered",
    [
        "",
        "123",
        "123456789",
        "１２３４",  # 全角の数字
        "٤٨٢٦",  # ほかの文字の数字（str.isdigit は真）
        "😀😀😀😀",
        "48 6",
        " 4826",
        "4826\n",
        "48a6",
        "-482",
        "9" * 10_000,
    ],
)
def test_4_から_8_けたの数字でない入力は_WRONG(entered: str) -> None:
    guard, _ = _guard()
    assert guard.try_login(entered) is LoginResult.WRONG


@pytest.mark.parametrize("entered", [None, 4826, b"4826", ["4826"]])
def test_文字でない入力も例外にせず_WRONG(entered: object) -> None:
    guard, _ = _guard()
    assert guard.try_login(cast(str, entered)) is LoginResult.WRONG


def test_形の違う入力もまちがいとして数える() -> None:
    guard, _ = _guard()
    for _ in range(LoginGuard.MAX_FAILURES):
        assert guard.try_login("１２３４") is LoginResult.WRONG
    assert guard.try_login(PIN) is LoginResult.LOCKED


@pytest.mark.parametrize("pin", ["", "123", "１２３４", "48a6", "123456789"])
def test_使えない_pin_ではどの入力も_OK_にならない(pin: str) -> None:
    guard, _ = _guard(pin)
    for entered in (pin, "", "1234"):
        assert guard.try_login(entered) is not LoginResult.OK


# --- まちがいの数え（2.4、2.5） ---


def test_まちがい_5_回で止まり_正しい_pin_も_LOCKED() -> None:
    guard, clock = _guard()
    assert _fail(guard, 5) == [LoginResult.WRONG] * 5
    assert guard.try_login(PIN) is LoginResult.LOCKED
    clock.advance(LoginGuard.LOCK_SECONDS - 0.1)
    assert guard.try_login(PIN) is LoginResult.LOCKED


def test_5_分たつと正しい_pin_で入れる() -> None:
    guard, clock = _guard()
    _fail(guard, 5)
    clock.advance(LoginGuard.LOCK_SECONDS)
    assert guard.try_login(PIN) is LoginResult.OK


def test_止めている間の入力は数えない() -> None:
    guard, clock = _guard()
    _fail(guard, 5)
    clock.advance(60)
    assert _fail(guard, 3) == [LoginResult.LOCKED] * 3
    clock.advance(LoginGuard.LOCK_SECONDS - 60)
    # 止めた時から 5 分で解ける。止めている間の入力で延びない
    assert guard.try_login(WRONG_PIN) is LoginResult.WRONG


def test_止めた後はまた_5_回で止まる() -> None:
    guard, clock = _guard()
    _fail(guard, 5)
    clock.advance(LoginGuard.LOCK_SECONDS)
    assert _fail(guard, 4) == [LoginResult.WRONG] * 4
    assert guard.try_login(WRONG_PIN) is LoginResult.WRONG
    assert guard.try_login(PIN) is LoginResult.LOCKED


def test_正しい_pin_で数が_0_に戻る() -> None:
    guard, _ = _guard()
    _fail(guard, 4)
    assert guard.try_login(PIN) is LoginResult.OK
    assert _fail(guard, 4) == [LoginResult.WRONG] * 4
    assert guard.try_login(PIN) is LoginResult.OK


def test_20_スレッドから同時に送っても_1_回ずつ数える() -> None:
    guard, _ = _guard()
    barrier = threading.Barrier(THREADS)
    results: list[LoginResult] = []
    lock = threading.Lock()

    def send() -> None:
        barrier.wait()
        result = guard.try_login(WRONG_PIN)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=send, name=f"remote-test-{i}") for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(LoginResult.WRONG) == LoginGuard.MAX_FAILURES
    assert results.count(LoginResult.LOCKED) == THREADS - LoginGuard.MAX_FAILURES
    assert guard.try_login(PIN) is LoginResult.LOCKED


# --- ログインの印の表（2.6、2.7） ---


def test_作った印は確かめられる() -> None:
    sessions = Sessions(FakeClock())
    token = sessions.start()
    assert sessions.check(token) is True


def test_印は毎回ちがう乱数() -> None:
    sessions = Sessions(FakeClock())
    tokens = {sessions.start() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(token) >= 40 for token in tokens)


@pytest.mark.parametrize("token", [None, "", "abc"])
def test_知らない印は_False(token: str | None) -> None:
    sessions = Sessions(FakeClock())
    sessions.start()
    assert sessions.check(token) is False


def test_12_時間で印が切れる() -> None:
    clock = FakeClock()
    sessions = Sessions(clock)
    token = sessions.start()
    clock.advance(Sessions.LIFETIME_SECONDS - 1)
    assert sessions.check(token) is True
    clock.advance(1)
    assert sessions.check(token) is False
    assert sessions.check(token) is False  # 消したので、切れたまま


def test_おわるで印が切れる() -> None:
    sessions = Sessions(FakeClock())
    token = sessions.start()
    other = sessions.start()
    sessions.end(token)
    assert sessions.check(token) is False
    assert sessions.check(other) is True
    sessions.end(token)  # 2 度目も例外にしない
    sessions.end("abc")


def test_起動し直すと印の表は空() -> None:
    clock = FakeClock()
    token = Sessions(clock).start()
    assert Sessions(clock).check(token) is False


def test_おわると_12_時間で切れたことを知らせる() -> None:
    clock = FakeClock()
    sessions = Sessions(clock)
    ended: list[str] = []
    sessions.on_end(ended.append)
    first = sessions.start()
    second = sessions.start()

    sessions.end(first)
    sessions.end(first)
    assert ended == [first]

    clock.advance(Sessions.LIFETIME_SECONDS)
    assert sessions.check(second) is False
    assert sessions.check(second) is False
    assert ended == [first, second]


def test_知らせる先で例外が出ても印は切れる(caplog: pytest.LogCaptureFixture) -> None:
    sessions = Sessions(FakeClock())
    ended: list[str] = []

    def broken(token: str) -> None:
        raise RuntimeError("boom")

    sessions.on_end(broken)
    sessions.on_end(ended.append)
    token = sessions.start()
    with caplog.at_level(logging.DEBUG):
        sessions.end(token)
    assert sessions.check(token) is False
    assert ended == [token]
    assert "RuntimeError" in caplog.text
    assert token not in caplog.text


def test_知らせる先は印の表を読んでも止まらない() -> None:
    clock = FakeClock()
    sessions = Sessions(clock)
    seen: list[bool] = []
    sessions.on_end(lambda token: seen.append(sessions.check(token)))
    token = sessions.start()
    sessions.end(token)
    assert seen == [False]


# --- ログに出さない（2.8） ---


def test_pin_入力_印がログに出ない(caplog: pytest.LogCaptureFixture) -> None:
    hidden_pin = "73915"
    hidden_try = "64028"
    clock = FakeClock()
    guard = LoginGuard(SecretStr(hidden_pin), clock)
    sessions = Sessions(clock)
    sessions.on_end(lambda token: None)
    with caplog.at_level(logging.DEBUG):
        guard.try_login(hidden_pin)
        for _ in range(6):
            guard.try_login(hidden_try)
        token = sessions.start()
        sessions.check(token)
        sessions.end(token)
        other = sessions.start()
        clock.advance(Sessions.LIFETIME_SECONDS)
        sessions.check(other)
    for value in (hidden_pin, hidden_try, token, other):
        assert value not in caplog.text
        assert all(value not in str(record.args) for record in caplog.records)
