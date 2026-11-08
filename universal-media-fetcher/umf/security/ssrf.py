"""Outbound URL policy: the SSRF boundary.

Everything in Universal Media Fetcher that touches the network goes through
this module. A service that fetches user-supplied URLs is, by default, an SSRF
engine aimed at its own private network: the cloud metadata endpoint at
169.254.169.254 hands out credentials, and every service mesh, database and
admin panel answers to a private address.

Validating the URL string is not enough, and most naive implementations stop
there. Three classes of bypass defeat a string check alone:

  1. **Numeric and alternate encodings.** ``http://2130706433/`` and
     ``http://0x7f.0.0.1/`` are both 127.0.0.1. Only resolution reveals them.
  2. **DNS rebinding.** A hostname resolves to a public address when validated
     and to 127.0.0.1 microseconds later, when the connection is made.
  3. **Redirects.** A public URL that answers ``302 Location:
     http://169.254.169.254/`` is the canonical bypass and defeats any check
     performed once, at the start.

This module closes all three, and the tests in ``tests/test_ssrf.py`` assert
against a corpus of exactly these attacks.

The layering is deliberate:

    validate_url()        syntax, scheme, port, credentials, IP literals
    resolve_public_ips()  every DNS answer must be public
    check_outbound()      both of the above
    RedirectGuard         re-runs the checks on every hop

Phase 0 wires no fetcher; the guard is exercised against injected transports.
Connection pinning — connecting to the validated IP rather than re-resolving —
belongs to the fetcher in Phase 1, and until it exists the guard fails closed
by requiring an explicit fetcher.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from umf.errors import AppError, ErrorCode

# --------------------------------------------------------------------- policy
ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

#: Ports an operator may enable explicitly. Everything else is refused: a URL
#: fetch has no business reaching SSH, Redis or Postgres.
ALLOWED_PORTS: frozenset[int] = frozenset({80, 443, 8080, 8443})

#: Hostnames that resolve only inside a platform's own network.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "local",
        "internal",
        "intranet",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "169.254.169.254.nip.io",
    }
)

BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".intranet", ".home.arpa", ".lan")

#: Every range that is not publicly routable. `ipaddress` already exposes the
#: important ones by attribute, but spelling them out keeps the list auditable
#: and catches the odd reserved blocks that have no attribute.
_BLOCKED_V4 = (
    "0.0.0.0/8",         # "this network"
    "10.0.0.0/8",        # RFC 1918
    "100.64.0.0/10",     # carrier-grade NAT
    "127.0.0.0/8",       # loopback
    "169.254.0.0/16",    # link-local, incl. cloud metadata
    "172.16.0.0/12",     # RFC 1918
    "192.0.0.0/24",      # IETF protocol assignments
    "192.0.2.0/24",      # TEST-NET-1
    "192.88.99.0/24",    # 6to4 relay anycast
    "192.168.0.0/16",    # RFC 1918
    "198.18.0.0/15",     # benchmarking
    "198.51.100.0/24",   # TEST-NET-2
    "203.0.113.0/24",    # TEST-NET-3
    "224.0.0.0/4",       # multicast
    "240.0.0.0/4",       # reserved
    "255.255.255.255/32",
)
_BLOCKED_V6 = (
    "::/128",            # unspecified
    "::1/128",           # loopback
    "fc00::/7",          # unique local
    "fe80::/10",         # link-local
    "ff00::/8",          # multicast
    "2001:db8::/32",     # documentation
    "64:ff9b::/96",      # NAT64
    "100::/64",          # discard-only
)
BLOCKED_NETWORKS = tuple(ipaddress.ip_network(n) for n in _BLOCKED_V4 + _BLOCKED_V6)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_HOSTNAME_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*\.?$")

#: Hosts that are entirely numeric are IP addresses in one of the alternate
#: notations resolvers accept: ``127.1``, ``2130706433``, ``0x7f.0.0.1``.
#: ``ipaddress`` rejects all of these, but ``getaddrinfo`` resolves them, so a
#: URL that parses as "a hostname" can still reach loopback. Anything matching
#: this shape must be a canonical IP literal or it is refused.
_NUMERIC_HOST_RE = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\.?$")

_MAX_URL_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    """A URL that passed syntax policy. Resolution is a separate step."""

    raw: str
    normalized: str
    scheme: str
    host: str
    port: int
    path: str

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"


def _reject(url: str, code: ErrorCode, reason: str) -> AppError:
    return AppError(code, reason, detail={"url": url[:512]})


def is_blocked_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when an address is not publicly routable.

    IPv4-mapped IPv6 addresses (``::ffff:127.0.0.1``) are unwrapped first;
    otherwise ``::ffff:169.254.169.254`` would slip through as "IPv6".
    """
    try:
        addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    except ValueError:
        return True  # unparseable is not public

    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped

    if isinstance(addr, ipaddress.IPv6Address) and addr.scope_id:
        addr = ipaddress.ip_address(addr.packed)

    if any(addr.is_private for _ in (0,)) or addr.is_loopback or addr.is_link_local:
        return True
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True
    return any(addr in network for network in BLOCKED_NETWORKS if network.version == addr.version)


def _is_blocked_hostname(host: str) -> bool:
    host = host.rstrip(".").lower()
    if not host:
        return True
    if host in BLOCKED_HOSTNAMES:
        return True
    return any(host.endswith(suffix) for suffix in BLOCKED_SUFFIXES)


def validate_url(url: str, *, allowed_ports: Iterable[int] | None = None) -> ValidatedUrl:
    """Check a URL against syntax policy. Raises :class:`AppError` on refusal.

    This performs no DNS lookup, so it is cheap enough to run on every hop of a
    redirect chain, and it is deterministic, so it can be unit-tested.
    """
    if not isinstance(url, str):
        raise _reject(str(url), ErrorCode.INVALID_URL, "URL must be a string")

    raw = url
    url = url.strip()
    if not url:
        raise _reject(raw, ErrorCode.INVALID_URL, "URL is empty")
    if len(url) > _MAX_URL_LENGTH:
        raise _reject(raw, ErrorCode.INVALID_URL, "URL is too long")
    if _CONTROL_CHARS.search(url):
        # Browsers strip these; libraries do not, and a \r\n here is classic
        # header injection material.
        raise _reject(raw, ErrorCode.INVALID_URL, "URL contains control characters")
    if "\\" in url:
        raise _reject(raw, ErrorCode.INVALID_URL, "URL contains a backslash")

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise _reject(raw, ErrorCode.INVALID_URL, f"URL could not be parsed: {exc}") from exc

    scheme = parts.scheme.lower()
    if not scheme:
        raise _reject(raw, ErrorCode.INVALID_URL, "URL has no scheme (expected http or https)")
    if scheme not in ALLOWED_SCHEMES:
        # file:// reads the host filesystem, gopher:// speaks arbitrary
        # protocols, ftp:// is unauthenticated. None of them are media sources.
        raise _reject(raw, ErrorCode.INVALID_URL, f"scheme '{scheme}' is not allowed; use http or https")

    if parts.username or parts.password:
        # Credentials in a URL leak into logs and into the Host header dance;
        # they are also used to disguise the real host (`http://trusted.com@evil`).
        raise _reject(raw, ErrorCode.INVALID_URL, "URL must not contain credentials")

    host = (parts.hostname or "").strip().lower()
    if not host:
        raise _reject(raw, ErrorCode.INVALID_URL, "URL has no host")

    try:
        port = parts.port
    except ValueError as exc:
        raise _reject(raw, ErrorCode.INVALID_URL, "URL has an invalid port") from exc
    if port is None:
        port = DEFAULT_PORTS[scheme]

    permitted = set(allowed_ports) if allowed_ports is not None else set(ALLOWED_PORTS)
    if port not in permitted:
        raise _reject(
            raw,
            ErrorCode.INVALID_URL,
            f"port {port} is not allowed; permitted ports are {sorted(permitted)}",
        )

    # An IP literal goes straight to the range check -- there is nothing to
    # resolve, and `http://127.0.0.1/` must never reach a socket.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if is_blocked_ip(literal):
            raise _reject(raw, ErrorCode.INVALID_URL, f"address {host} is not publicly routable")
    else:
        if _NUMERIC_HOST_RE.match(host):
            raise _reject(
                raw,
                ErrorCode.INVALID_URL,
                f"host '{host}' is a non-canonical numeric address",
            )
        if _is_blocked_hostname(host):
            raise _reject(raw, ErrorCode.INVALID_URL, f"host '{host}' is not a public hostname")
        if not _HOSTNAME_RE.match(host):
            raise _reject(raw, ErrorCode.INVALID_URL, f"host '{host}' is malformed")

    normalized = urlunsplit(
        (scheme, f"{host}:{port}" if port != DEFAULT_PORTS[scheme] else host, parts.path or "/", parts.query, "")
    )
    return ValidatedUrl(
        raw=raw,
        normalized=normalized,
        scheme=scheme,
        host=host,
        port=port,
        path=parts.path or "/",
    )


# ------------------------------------------------------------------ resolution
Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    """Resolve every A and AAAA answer for ``host``."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in seen:
            seen.append(address)
    return seen


def resolve_public_ips(host: str, port: int = 443, *, resolver: Resolver | None = None) -> tuple[str, ...]:
    """Resolve ``host`` and require **every** answer to be publicly routable.

    Requiring all answers rather than the first is the DNS-rebinding defence.
    An attacker who controls a nameserver can return a public address for the
    checks and a private one for the connection; refusing unless *all* answers
    are public removes the ambiguity instead of racing it.

    Rebinding is fully closed only when the caller then connects to the address
    returned here rather than re-resolving. That pinning lives in the fetcher
    (Phase 1).
    """
    resolver = resolver or system_resolver
    try:
        addresses = resolver(host, port)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise AppError(
            ErrorCode.NETWORK_ERROR,
            f"could not resolve '{host}'",
            detail={"host": host, "error": str(exc)},
        ) from exc

    if not addresses:
        raise AppError(ErrorCode.NETWORK_ERROR, f"'{host}' did not resolve to any address", detail={"host": host})

    blocked = [address for address in addresses if is_blocked_ip(address)]
    if blocked:
        raise AppError(
            ErrorCode.INVALID_URL,
            f"'{host}' resolves to a non-public address",
            detail={"host": host, "blocked": blocked},
        )
    return tuple(addresses)


def check_outbound(url: str, *, resolver: Resolver | None = None) -> ValidatedUrl:
    """Full pre-flight check: syntax policy, then resolution policy."""
    validated = validate_url(url)
    resolve_public_ips(validated.host, validated.port, resolver=resolver)
    return validated


# ------------------------------------------------------------------- redirects
@dataclass(slots=True)
class Hop:
    url: str
    status: int
    location: str | None = None


class RedirectGuard:
    """Follow redirects manually, re-validating every hop.

    ``http`` clients follow redirects internally, which is precisely why the
    standard bypass works: the policy is applied to the URL the user supplied
    and never to the one the server selects. Here each ``Location`` is resolved
    against the current URL, re-checked, and only then fetched.
    """

    def __init__(self, fetcher, *, max_redirects: int = 5, resolver: Resolver | None = None) -> None:
        if fetcher is None:
            raise ValueError("RedirectGuard requires an explicit fetcher")
        self._fetch = fetcher
        self.max_redirects = max_redirects
        self._resolver = resolver
        self.hops: list[Hop] = []

    def _fetch_hop(self, url: str):
        result = self._fetch(url)
        if hasattr(result, "__await__"):
            raise TypeError("RedirectGuard.fetch must be synchronous; wrap async fetchers")
        return result

    def get(self, url: str):
        self.hops = []
        current = url
        for _ in range(self.max_redirects + 1):
            validated = check_outbound(current, resolver=self._resolver)
            response = self._fetch_hop(validated.normalized)
            status = int(getattr(response, "status_code", 0) or 0)
            location = None
            if status in (301, 302, 303, 307, 308):
                headers = getattr(response, "headers", {}) or {}
                location = headers.get("location") or headers.get("Location")
                self.hops.append(Hop(current, status, location))
                if not location:
                    raise AppError(
                        ErrorCode.NETWORK_ERROR,
                        f"redirect from '{current}' had no Location header",
                    )
                # Relative Locations are normal and must resolve against the
                # URL that produced them.
                current = urljoin(validated.normalized, location)
                continue
            self.hops.append(Hop(current, status))
            return response
        raise AppError(
            ErrorCode.NETWORK_ERROR,
            f"more than {self.max_redirects} redirects",
            detail={"hops": [h.url for h in self.hops]},
        )
