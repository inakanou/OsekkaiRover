"""pin の確かめ、まちがいの数え、ログインの印の表（要件 2.1〜2.8）。

- `LoginGuard` は 1 つのロックの中で、続いたまちがいの数と止める期限を持つ。どの端末からも同じ数え
- `Sessions` は乱数の印をメモリだけで持つ。起動し直すと空になる
- pin・入力・印をログに出さない。出すのは例外の型の名前だけ
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from enum import Enum
from typing import Final

from pydantic import SecretStr

from osekkai.core.clock import Clock

_log = logging.getLogger(__name__)

_PIN_MIN: Final = 4
_PIN_MAX: Final = 8
_ASCII_DIGITS: Final = frozenset("0123456789")
_TOKEN_BYTES: Final = 32


class LoginResult(Enum):
    OK = "ok"
    WRONG = "wrong"  # 2.3
    LOCKED = "locked"  # 2.4


def _pin_shape(value: object) -> bool:
    """4〜8 けたの ASCII の数字だけの文字か。長さを先に見て、長い入力を調べない。"""
    return (
        isinstance(value, str)
        and _PIN_MIN <= len(value) <= _PIN_MAX
        and set(value) <= _ASCII_DIGITS
    )


class LoginGuard:
    """pin を確かめ、続いたまちがいを数える。まちがい 5 回で 5 分止める（2.4）。"""

    MAX_FAILURES: Final = 5
    LOCK_SECONDS: Final = 300.0

    def __init__(self, pin: SecretStr, clock: Clock) -> None:
        value = pin.get_secret_value()
        # 使えない pin なら、どの入力も合わない（安全側）
        self._pin: bytes | None = value.encode("ascii") if _pin_shape(value) else None
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until: float | None = None

    def try_login(self, entered: str) -> LoginResult:
        """入力を確かめる。形の違う入力もまちがいとして数え、例外にしない（2.3）。"""
        with self._lock:
            now = self._clock.monotonic()
            if self._locked_until is not None:
                if now < self._locked_until:
                    return LoginResult.LOCKED
                self._locked_until = None
                self._failures = 0
            if self._matches(entered):
                self._failures = 0
                return LoginResult.OK
            self._failures += 1
            if self._failures >= self.MAX_FAILURES:
                self._locked_until = now + self.LOCK_SECONDS
            return LoginResult.WRONG

    def _matches(self, entered: object) -> bool:
        if self._pin is None or not isinstance(entered, str) or not _pin_shape(entered):
            return False
        return hmac.compare_digest(entered.encode("ascii"), self._pin)


class Sessions:
    """ログインの印の表。メモリだけで持ち、12 時間か「おわる」で切れる（2.6、2.7）。"""

    LIFETIME_SECONDS: Final = 12 * 3600.0

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._started: dict[str, float] = {}
        self._callbacks: list[Callable[[str], None]] = []

    def start(self) -> str:
        """新しい印を作る。切れた印は、ここでも消す。"""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._lock:
            expired = self._drop_expired()
            self._started[token] = self._clock.monotonic()
        self._notify(expired)
        return token

    def check(self, token: str | None) -> bool:
        """印が生きているか。12 時間を過ぎた印は消して False（2.6）。"""
        with self._lock:
            expired = self._drop_expired()
            alive = isinstance(token, str) and token in self._started
        self._notify(expired)
        return alive

    def end(self, token: str) -> None:
        """印を切る（2.7）。知らない印は何もしない。"""
        with self._lock:
            known = self._started.pop(token, None) is not None
        if known:
            self._notify([token])

    def on_end(self, callback: Callable[[str], None]) -> None:
        """印が切れたとき（おわる・12 時間）に、その印を渡して呼ぶ先を足す。"""
        with self._lock:
            self._callbacks.append(callback)

    def _drop_expired(self) -> list[str]:
        """ロックを持って呼ぶ。切れた印を消し、消した印を返す。"""
        deadline = self._clock.monotonic() - self.LIFETIME_SECONDS
        expired = [token for token, started in self._started.items() if started <= deadline]
        for token in expired:
            del self._started[token]
        return expired

    def _notify(self, tokens: list[str]) -> None:
        """ロックの外で呼ぶ。呼ぶ先の例外は受けて、型の名前だけログに出す。"""
        if not tokens:
            return
        with self._lock:
            callbacks = list(self._callbacks)
        for token in tokens:
            for callback in callbacks:
                try:
                    callback(token)
                except Exception as e:
                    _log.warning("ログインの終わりの知らせに失敗しました: %s", type(e).__name__)
