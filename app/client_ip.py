"""Visitor address behind the production chain: Cloudflare edge -> cloudflared on the host -> 127.0.0.1:8790
-> docker-proxy -> container.

docker-proxy connects from the bridge gateway (172.20.0.1 on luomoapi-hub_default, verified 2026-10-18), so the
container sees that one address for every visitor.  Only peers on the exact allow-list may supply the visitor
address through ``CF-Connecting-IP`` (Cloudflare overwrites that header on every request).  Other containers on
shared Docker networks are not on the list, so they cannot choose an identity.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass

from fastapi import Request


def _trusted_peers() -> frozenset:
    raw = os.getenv("TRUSTED_PROXY_PEERS", "127.0.0.1,::1,172.20.0.1")
    peers = set()
    for item in raw.split(","):
        try:
            peers.add(ipaddress.ip_address(item.strip()))
        except ValueError:
            continue
    return frozenset(peers)


TRUSTED_PROXY_PEERS = _trusted_peers()


@dataclass(frozen=True)
class ClientIdentity:
    address: str | None
    verified: bool  # True when the address names the visitor rather than a shared proxy hop


def _ip(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def client_identity(request: Request) -> ClientIdentity:
    peer = request.client.host if request.client else None
    peer_ip = _ip(peer)
    if peer_ip is not None and peer_ip in TRUSTED_PROXY_PEERS:
        visitor = _ip(request.headers.get("cf-connecting-ip"))
        if visitor is not None:
            return ClientIdentity(str(visitor), True)
        return ClientIdentity(peer, False)
    if peer_ip is not None and not (peer_ip.is_private or peer_ip.is_loopback):
        return ClientIdentity(peer, True)
    # A private peer that is not an allowed proxy (or no usable peer): we cannot tell visitors apart.
    return ClientIdentity(peer, False)


def client_address(request: Request) -> str | None:
    return client_identity(request).address


def forwarded_https(request: Request) -> bool:
    """True when an allow-listed proxy reports that the visitor reached Cloudflare over HTTPS.

    uvicorn only trusts X-Forwarded-Proto from 127.0.0.1, but production traffic arrives from the Docker bridge
    gateway, so without this the app believes every request is plain HTTP (e.g. /v1 -> http://.../v1/).
    """
    peer_ip = _ip(request.client.host if request.client else None)
    if peer_ip is None or peer_ip not in TRUSTED_PROXY_PEERS:
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    visitor = request.headers.get("cf-visitor", "").replace(" ", "").lower()
    return proto == "https" or '"scheme":"https"' in visitor
