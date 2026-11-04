"""ログの形式（Monitoring）のテスト。"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterator

import pytest

from osekkai.core.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def restore_logger() -> Iterator[None]:
    logger = logging.getLogger("osekkai")
    handlers = list(logger.handlers)
    level = logger.level
    yield
    logger.handlers = handlers
    logger.setLevel(level)


def emit(stream: io.StringIO, name: str = "osekkai.core.test") -> str:
    logging.getLogger(name).info("こんにちは")
    return stream.getvalue()


def test_journalの下では時刻なしの形式になる() -> None:
    stream = io.StringIO()
    configure_logging(env={"JOURNAL_STREAM": "8:1234"}, stream=stream)
    assert emit(stream) == "INFO osekkai.core.test: こんにちは\n"


def test_端末では時刻付きの形式になる() -> None:
    stream = io.StringIO()
    configure_logging(env={}, stream=stream)
    line = emit(stream, "osekkai.features.greet")
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO osekkai\.features\.greet: こんにちは\n",
        line,
    )


def test_2回設定しても手渡し先は1つ() -> None:
    first = io.StringIO()
    second = io.StringIO()
    configure_logging(env={}, stream=first)
    configure_logging(env={"JOURNAL_STREAM": "8:1"}, stream=second)
    ours = [h for h in logging.getLogger("osekkai").handlers if getattr(h, "_osekkai", False)]
    assert len(ours) == 1
    emit(second)
    assert first.getvalue() == ""
    assert second.getvalue() == "INFO osekkai.core.test: こんにちは\n"


def test_詳しく出す指定ではDEBUGも出る() -> None:
    stream = io.StringIO()
    configure_logging(verbose=True, env={"JOURNAL_STREAM": "8:1"}, stream=stream)
    logging.getLogger("osekkai.core.test").debug("くわしく")
    assert stream.getvalue() == "DEBUG osekkai.core.test: くわしく\n"


def test_ふだんはDEBUGを出さない() -> None:
    stream = io.StringIO()
    configure_logging(env={"JOURNAL_STREAM": "8:1"}, stream=stream)
    logging.getLogger("osekkai.core.test").debug("くわしく")
    assert stream.getvalue() == ""


def test_既定では標準エラーに出す(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_err = io.StringIO()
    monkeypatch.setattr("sys.stderr", fake_err)
    configure_logging(env={"JOURNAL_STREAM": "8:1"})
    assert emit(fake_err) == "INFO osekkai.core.test: こんにちは\n"
