"""家のネットワークのアドレスかを判定する（要件 1.1、1.2）。

決まった範囲を自分で持つ。ipaddress.is_private は Python の版で範囲が違うので使わない。
要求の Host も、家のアドレスか家の名前（localhost・点のない名前・.local）だけを受ける
（DNS の付け替えで、外のサイトの名前から家のロボットに届かないように）。
"""

import re
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from typing import Final

HOME_NETWORKS: Final[tuple[IPv4Network | IPv6Network, ...]] = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
    ip_network("fe80::/10"),
)

_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PORT: Final = re.compile(r"[0-9]{1,5}")
_LOCAL_LABEL: Final = "local"


def is_home_address(remote_addr: str | None) -> bool:
    """接続の相手のアドレスが家のネットワークの範囲なら True。読めない値と範囲の外は False。"""
    address = _parse(remote_addr)
    if address is None:
        return False
    return any(address in network for network in HOME_NETWORKS)


def _parse(remote_addr: str | None) -> IPv4Address | IPv6Address | None:
    """アドレスを読む。スコープを除き、IPv4 を写した IPv6 は IPv4 に直す。読めないと None。"""
    if not remote_addr:
        return None
    host, sep, scope = remote_addr.partition("%")
    if sep and not scope:
        return None
    try:
        address = ip_address(host)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_home_host(host: str | None) -> bool:
    """要求の Host（ポート付きでもよい）が、家のアドレスか家の名前なら True。

    家の名前は localhost、点のない 1 つの名前（数字だけは除く）、.local で終わる名前。
    IPv6 は角かっこで囲む。読めない値は False。
    """
    name = _host_name(host)
    if name is None:
        return False
    if name.startswith("["):
        return is_home_address(name[1:-1])
    try:
        ip_address(name)
    except ValueError:
        pass
    else:
        return is_home_address(name)
    labels = name.lower().split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        return False
    if len(labels) == 1:
        return not labels[0].isdigit()
    return labels[-1] == _LOCAL_LABEL


def _host_name(host: str | None) -> str | None:
    """Host からポートを除く。IPv6 は角かっこのまま返す。形が違えば None。"""
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return None
        name, rest = host[: end + 1], host[end + 1 :]
        if rest and not (rest.startswith(":") and _PORT.fullmatch(rest[1:])):
            return None
        return name
    name, sep, port = host.partition(":")
    if sep and not _PORT.fullmatch(port):
        return None
    return name or None
