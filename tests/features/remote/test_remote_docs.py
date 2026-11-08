"""ラジコンの手順書（docs/remote.md）の決まり（13.3）。

手順書が設定の項目と self-check の項目をすべて案内し、
公開リポジトリに出してはいけない値（実のアドレス・引用符のない pin）を書かないことを確かめる。
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from osekkai.features.remote.checks import REMOTE_CHECKS
from osekkai.features.remote.config import RemoteConfig

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "remote.md"
EXAMPLE = ROOT / "config" / "osekkai.example.yaml"

# 文書の見本に使ってよいアドレスの範囲（RFC 5737）
_EXAMPLE_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def test_手順書がある() -> None:
    assert DOC.is_file()


def test_手順書は設定の項目をすべて案内する() -> None:
    text = _doc()
    missing = [name for name in RemoteConfig.model_fields if f"`{name}`" not in text]
    assert missing == []


def test_手順書はself_checkの項目をすべて案内する() -> None:
    text = _doc()
    missing = [check.name for check in REMOTE_CHECKS if f"`{check.name}`" not in text]
    assert missing == []


def test_手順書に見本の範囲の外のIPv4アドレスを書かない() -> None:
    found = []
    for match in _IPV4.findall(_doc()):
        try:
            address = ipaddress.ip_address(match)
        except ValueError:
            continue
        if not any(address in network for network in _EXAMPLE_NETWORKS):
            found.append(match)
    assert found == []


def test_手順書のpinは引用符で囲む() -> None:
    assert re.search(r"pin:\s*\d", _doc()) is None


def test_設定の見本が指す手順書がある() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "docs/remote.md" in text
    assert "pin:" not in text.split("\nremote:", 1)[1].split("\n\n", 1)[0]
