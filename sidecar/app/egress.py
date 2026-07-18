"""SSRF egress guard for server-side fetches.

The reader fetches attacker-influenced URLs server-side: every article <img> is
rewritten to /proxy/image?url=... (extractor._rewrite_image_srcs), and article
extraction fetches entry URLs. Unchecked, either would fetch
http://169.254.169.254/, localhost services, or internal hosts. This module is
the single policy point:

- check_scheme_host — cheap, DNS-free: http/https only, hostname present, an
  IP-literal host must be publicly routable. This is the ONLY check applied to
  fetches routed through a remote HTTP proxy (Brightdata): DNS and the TCP
  connect happen at the proxy, not here, so an IP-range check on our side would
  be meaningless for those.
- check_public — for DIRECT fetches: resolves the hostname and refuses any
  answer in a loopback/link-local/private/CGNAT/reserved/multicast range
  (v4 and v6, including v6-mapped v4).
- guarded_get — a direct GET that re-validates every redirect hop
  (follow_redirects forced off; each Location is checked before it is fetched).

DNS rebinding: we resolve-then-check rather than pinning the connection to the
vetted IP — pinning needs a custom transport with Host/SNI overrides, which is
not worth the residual risk for a single-user reader behind a network boundary
(the attacker must control an authoritative DNS server and win a TTL race).

Kill switch: EGRESS_GUARD=0 disables every check (local debugging only).
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from app.config import EGRESS_GUARD

_ALLOWED_SCHEMES = ("http", "https")
_MAX_REDIRECTS = 10


class EgressBlockedError(Exception):
    """The egress policy refused this URL (scheme, host, or resolved address)."""


def _forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address that isn't plainly global-routable: loopback,
    link-local, RFC1918, CGNAT, reserved, multicast, unspecified — with
    v6-mapped v4 unwrapped first so ::ffff:10.0.0.1 can't slip through."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global


def check_scheme_host(url: str) -> None:
    """DNS-free sanity: http/https, a hostname, and no non-public IP literal.
    Raises EgressBlockedError. The whole policy for proxy-routed fetches."""
    if not EGRESS_GUARD:
        return
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise EgressBlockedError(f"scheme {parsed.scheme!r} not allowed")
    host = parsed.hostname
    if not host:
        raise EgressBlockedError("URL has no hostname")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a name — check_public resolves it for direct fetches
    if _forbidden(ip):
        raise EgressBlockedError(f"IP {host} is not publicly routable")


async def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to. A separate function so tests stub
    DNS here instead of doing network I/O."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return [info[4][0] for info in infos]


async def check_public(url: str) -> None:
    """Full guard for DIRECT fetches: scheme/host sanity, then resolve the
    hostname and refuse if ANY answer is non-public. Raises EgressBlockedError.
    Residual risk: resolve-then-check (no IP pinning) — see module docstring."""
    if not EGRESS_GUARD:
        return
    check_scheme_host(url)
    parsed = urlparse(url)
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
        return  # IP literal — already vetted by check_scheme_host
    except ValueError:
        pass
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addrs = await _resolve(host, port)
    except OSError as exc:
        raise EgressBlockedError(f"cannot resolve {host}: {exc}") from exc
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise EgressBlockedError(f"unparseable address for {host}: {addr}") from exc
        if _forbidden(ip):
            raise EgressBlockedError(f"{host} resolves to non-public {addr}")


async def guarded_get(client_kwargs: dict, url: str) -> httpx.Response:
    """Direct GET with the egress guard applied to every redirect hop.

    Redirects are walked here (follow_redirects forced off) so a public URL
    can't bounce the fetch into a private range via a 30x Location. The
    response body is already read when this returns.
    """
    kwargs = {**client_kwargs, "follow_redirects": False}
    async with httpx.AsyncClient(**kwargs) as client:
        for _ in range(_MAX_REDIRECTS):
            await check_public(url)
            r = await client.get(url)
            if not r.has_redirect_location:
                r.raise_for_status()
                return r
            url = str(r.next_request.url)
    raise EgressBlockedError(f"more than {_MAX_REDIRECTS} redirects for {url}")
