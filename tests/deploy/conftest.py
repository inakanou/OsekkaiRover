"""道具のテストで共通に使う準備。"""

import pytest
from fake_runner import FakeRunner


@pytest.fixture
def fake_runner() -> FakeRunner:
    """呼ばれたコマンドを記録し、決めた結果を返す偽の Runner。"""
    return FakeRunner()
