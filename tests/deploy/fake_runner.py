"""テストで使う偽の Runner。呼ばれたコマンドを記録し、決めた結果を返す。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rover_deploy.system import RunResult, check_result


@dataclass(frozen=True)
class Call:
    """1 回の呼び出しの記録。"""

    argv: tuple[str, ...]
    input: bytes | None
    timeout: float | None
    check: bool
    cwd: Path | None
    env: Mapping[str, str] | None


@dataclass
class _Script:
    prefix: tuple[str, ...]
    outcomes: list[RunResult | BaseException] = field(default_factory=list)


def _as_bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


class FakeRunner:
    """argv の先頭が一致する、いちばん長い決まりの結果を返す。

    同じ先頭に何度も決めると、呼ばれるたびに順に返し、最後の 1 つは返し続ける。
    決めていないコマンドは、`strict` なら AssertionError、そうでなければ成功を返す。
    """

    def __init__(self, *, strict: bool = True) -> None:
        self.calls: list[Call] = []
        self._strict = strict
        self._scripts: list[_Script] = []

    def _script_for(self, prefix: Sequence[str]) -> _Script:
        key = tuple(prefix)
        for script in self._scripts:
            if script.prefix == key:
                return script
        script = _Script(key)
        self._scripts.append(script)
        return script

    def script(
        self,
        prefix: Sequence[str],
        *,
        returncode: int = 0,
        stdout: str | bytes = b"",
        stderr: str | bytes = b"",
    ) -> None:
        result = RunResult(returncode, _as_bytes(stdout), _as_bytes(stderr))
        self._script_for(prefix).outcomes.append(result)

    def raise_on(self, prefix: Sequence[str], error: BaseException) -> None:
        self._script_for(prefix).outcomes.append(error)

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        return [call.argv for call in self.calls]

    def __call__(
        self,
        argv: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float | None = None,
        check: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        args = tuple(argv)
        self.calls.append(Call(args, input, timeout, check, cwd, env))
        matches = [s for s in self._scripts if args[: len(s.prefix)] == s.prefix and s.outcomes]
        if not matches:
            if self._strict:
                raise AssertionError(f"決めていないコマンドが呼ばれました: {args}")
            return check_result(args, RunResult(0, b"", b""), check=check)
        script = max(matches, key=lambda s: len(s.prefix))
        outcome = script.outcomes[0] if len(script.outcomes) == 1 else script.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return check_result(args, outcome, check=check)
