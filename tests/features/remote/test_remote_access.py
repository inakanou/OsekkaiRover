"""家のネットワークのアドレスかの判定（要件 1.1、1.2）。

例のアドレスは、範囲の端の値と文書用の範囲だけを使う。
"""

import ast
from pathlib import Path

import pytest

from osekkai.features.remote import access


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.0",
        "10.255.255.255",
        "172.16.0.0",
        "172.31.255.255",
        "192.168.0.0",
        "192.168.255.255",
        "127.0.0.1",
        "::1",
        "fd00::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_決まった範囲のアドレスは家(addr: str) -> None:
    assert access.is_home_address(addr) is True


@pytest.mark.parametrize(
    "addr",
    [
        "172.15.255.255",
        "172.32.0.0",
        "100.64.0.1",
        "169.254.1.1",
        "8.8.8.8",
        "192.0.2.1",
        "203.0.113.5",
        "2001:db8::1",
        "fe00::1",
        "::",
        "0.0.0.0",
    ],
)
def test_範囲の外のアドレスは家ではない(addr: str) -> None:
    assert access.is_home_address(addr) is False


def test_IPv4_を写した_IPv6_は_IPv4_に直して判定する() -> None:
    assert access.is_home_address("::ffff:192.168.1.5") is True
    assert access.is_home_address("::ffff:10.1.2.3") is True
    assert access.is_home_address("::ffff:8.8.8.8") is False
    assert access.is_home_address("::ffff:100.64.0.1") is False


def test_スコープ付きの値はスコープを除いて判定する() -> None:
    assert access.is_home_address("fe80::1%eth0") is True
    assert access.is_home_address("fe80::1%2") is True
    assert access.is_home_address("2001:db8::1%eth0") is False


@pytest.mark.parametrize(
    "addr",
    [None, "", " ", "abc", "999.1.1.1", "192.168.1", "192.168.1.5/24", "%eth0", "fe80::1%", ":::"],
)
def test_読めない値は家ではない(addr: str | None) -> None:
    assert access.is_home_address(addr) is False


def test_家の範囲は決まった_7_つ() -> None:
    assert [str(n) for n in access.HOME_NETWORKS] == [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    ]


def test_is_private_を使わない() -> None:
    tree = ast.parse(Path(access.__file__).read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "is_private" not in names


# --- 要求の Host（DNS の付け替えを防ぐ） ---


@pytest.mark.parametrize(
    "host",
    [
        "192.168.1.20",
        "192.168.1.20:8080",
        "127.0.0.1:8080",
        "[::1]",
        "[fe80::1]:8080",
        "localhost",
        "localhost:8080",
        "LOCALHOST",
        "raspberrypi5",
        "raspberrypi5:8080",
        "rover.local",
        "rover.local:8080",
        "Rover.Local",
    ],
)
def test_家のアドレスか家の名前のHostは受ける(host: str) -> None:
    assert access.is_home_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "203.0.113.5",
        "203.0.113.5:8080",
        "[2001:db8::1]:8080",
        "evil.example",
        "evil.example:8080",
        "rover.local.evil.example",
        ".local",
        "3232235777",
        "fe80::1",
        "[fe80::1",
        "raspberrypi5:",
        "raspberrypi5:80a",
        "rover_pi",
        "-rover",
        "rover.local:8080:1",
    ],
)
def test_ほかのHostは受けない(host: str | None) -> None:
    assert access.is_home_host(host) is False
