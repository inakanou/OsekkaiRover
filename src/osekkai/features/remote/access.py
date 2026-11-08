"""家のネットワークのアドレスかを判定する（要件 1.1、1.2）。

決まった範囲を自分で持つ。ipaddress.is_private は Python の版で範囲が違うので使わない。
"""

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
