"""SSRF policy: the hostile corpus.

Each case here is a real bypass technique, not a hypothetical. If any of them
passes validation, the service can be used to read cloud credentials or reach
an internal admin panel, so these tests are the security boundary rather than a
coverage exercise.
"""
from __future__ import annotations

import pytest

from umf.errors import AppError, ErrorCode
from umf.security.ssrf import (
    RedirectGuard,
    check_outbound,
    is_blocked_ip,
    resolve_public_ips,
    validate_url,
)


def _code(url: str, **kwargs) -> str:
    with pytest.raises(AppError) as caught:
        (check_outbound(url, **kwargs) if "resolver" in kwargs else validate_url(url))
    return caught.value.code.value


# ---------------------------------------------------------------- IP literals
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1/",
        "http://0.0.0.0/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://100.64.0.1/",
        "http://224.0.0.1/",
        "http://255.255.255.255/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
    ],
)
def test_private_ip_literals_are_refused(url: str) -> None:
    assert _code(url) == ErrorCode.INVALID_URL.value


def test_ipv4_mapped_ipv6_is_unwrapped_before_the_range_check() -> None:
    """`::ffff:169.254.169.254` is a valid IPv6 literal that reaches metadata."""
    assert is_blocked_ip("::ffff:169.254.169.254") is True
    assert is_blocked_ip("::ffff:8.8.8.8") is False


def test_public_ip_literal_is_allowed() -> None:
    assert validate_url("http://8.8.8.8/").host == "8.8.8.8"


# ------------------------------------------------------------------ hostnames
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://localhost:8080/",
        "http://LOCALHOST/",
        "http://foo.local/",
        "http://printer.lan/",
        "http://metadata.google.internal/",
        "http://metadata/",
        "http://instance-data/",
        "http://service.internal/",
        "http://db.home.arpa/",
        "http://localhost./",
    ],
)
def test_internal_hostnames_are_refused(url: str) -> None:
    assert _code(url) == ErrorCode.INVALID_URL.value


# --------------------------------------------------------------------- scheme
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x.mp4",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "dict://example.com:11211/",
        "  ",
        "",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    assert _code(url) == ErrorCode.INVALID_URL.value


def test_scheme_is_case_insensitive() -> None:
    assert validate_url("HTTPS://example.com/v.mp4").scheme == "https"


# ----------------------------------------------------------------- credentials
def test_credentials_in_url_are_refused() -> None:
    # `http://trusted.com@evil.com/` reads as trusted.com to a human and
    # connects to evil.com; the userinfo is also a log-leak vector.
    assert _code("http://user:pass@example.com/") == ErrorCode.INVALID_URL.value
    assert _code("https://trusted.com@evil.com/v.mp4") == ErrorCode.INVALID_URL.value


# ----------------------------------------------------------------------- ports
@pytest.mark.parametrize("url", ["http://example.com:22/", "http://example.com:6379/", "http://example.com:5432/"])
def test_non_web_ports_are_refused(url: str) -> None:
    assert _code(url) == ErrorCode.INVALID_URL.value


def test_explicit_allowed_port_is_kept() -> None:
    validated = validate_url("https://example.com:8443/v.mp4")
    assert validated.port == 8443
    assert ":8443" in validated.normalized


# ------------------------------------------------------------------ injection
def test_control_characters_are_refused() -> None:
    assert _code("http://example.com/a\r\nHost: evil.com") == ErrorCode.INVALID_URL.value


def test_backslashes_are_refused() -> None:
    # `http://example.com\@evil.com` is parsed differently by different clients.
    assert _code("http://example.com\\@evil.com/") == ErrorCode.INVALID_URL.value


def test_over_long_url_is_refused() -> None:
    assert _code("http://example.com/" + "a" * 5000) == ErrorCode.INVALID_URL.value


# ------------------------------------------------------- numeric IP encodings
def test_decimal_and_hex_encodings_are_caught_at_resolution() -> None:
    """`http://2130706433/` is 127.0.0.1 and `0x7f000001` is too.

    A string check cannot see this; only resolution can. The resolver is faked
    here so the test states the expectation without needing real DNS.
    """

    def decimal_resolver(host: str, port: int) -> list[str]:
        assert host == "2130706433"
        return ["127.0.0.1"]

    with pytest.raises(AppError) as caught:
        resolve_public_ips("2130706433", 80, resolver=decimal_resolver)
    assert caught.value.code is ErrorCode.INVALID_URL


# ------------------------------------------------------------- DNS resolution
def test_resolution_rejects_when_any_answer_is_private() -> None:
    """DNS rebinding returns both a public and a private answer.

    Requiring every answer to be public removes the race: a client cannot pick
    the private one if the request never proceeds.
    """

    def rebinding_resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "127.0.0.1"]

    with pytest.raises(AppError) as caught:
        resolve_public_ips("rebind.example", 443, resolver=rebinding_resolver)
    assert caught.value.code is ErrorCode.INVALID_URL
    assert "127.0.0.1" in caught.value.detail["blocked"]


def test_resolution_accepts_all_public_answers() -> None:
    def public_resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]

    assert resolve_public_ips("example.com", 443, resolver=public_resolver) == (
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    )


def test_resolution_failure_is_a_network_error_not_an_invalid_url() -> None:
    import socket

    def failing_resolver(host: str, port: int) -> list[str]:
        raise socket.gaierror("name resolution failed")

    with pytest.raises(AppError) as caught:
        resolve_public_ips("nope.invalid", 443, resolver=failing_resolver)
    assert caught.value.code is ErrorCode.NETWORK_ERROR


def test_empty_resolution_is_rejected() -> None:
    with pytest.raises(AppError) as caught:
        resolve_public_ips("empty.example", 443, resolver=lambda h, p: [])
    assert caught.value.code is ErrorCode.NETWORK_ERROR


# ----------------------------------------------------------------- redirects
class _Response:
    def __init__(self, status_code: int, location: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {"location": location} if location else {}


def _public(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def test_redirect_to_metadata_endpoint_is_blocked() -> None:
    """The canonical bypass: a public URL that redirects to 169.254.169.254."""
    seen: list[str] = []

    def fetcher(url: str):
        seen.append(url)
        return _Response(302, "http://169.254.169.254/latest/meta-data/")

    guard = RedirectGuard(fetcher, resolver=_public)
    with pytest.raises(AppError) as caught:
        guard.get("https://example.com/start")
    assert caught.value.code is ErrorCode.INVALID_URL
    # The dangerous hop must never have been fetched.
    assert len(seen) == 1
    assert "169.254" not in seen[0]


def test_redirect_to_private_hostname_is_blocked() -> None:
    def fetcher(_url: str):
        return _Response(302, "http://localhost:8080/admin")

    with pytest.raises(AppError):
        RedirectGuard(fetcher, resolver=_public).get("https://example.com/")


def test_safe_redirect_chain_is_followed() -> None:
    hops = {"https://example.com/a": "https://example.com/b", "https://example.com/b": None}

    def fetcher(url: str):
        target = hops.get(url)
        return _Response(302, target) if target else _Response(200)

    guard = RedirectGuard(fetcher, resolver=_public)
    response = guard.get("https://example.com/a")
    assert response.status_code == 200
    assert [hop.status for hop in guard.hops] == [302, 200]


def test_relative_redirect_is_resolved_against_the_current_url() -> None:
    def fetcher(url: str):
        if url.endswith("/a"):
            return _Response(302, "/b")
        return _Response(200)

    guard = RedirectGuard(fetcher, resolver=_public)
    guard.get("https://example.com/a")
    assert guard.hops[-1].url == "https://example.com/b"


def test_redirect_loop_is_capped() -> None:
    def fetcher(_url: str):
        return _Response(302, "https://example.com/again")

    with pytest.raises(AppError) as caught:
        RedirectGuard(fetcher, resolver=_public, max_redirects=3).get("https://example.com/")
    assert "redirects" in caught.value.user_message


def test_redirect_without_location_is_an_error() -> None:
    def fetcher(_url: str):
        return _Response(302, None)

    with pytest.raises(AppError):
        RedirectGuard(fetcher, resolver=_public).get("https://example.com/")


def test_guard_requires_an_explicit_fetcher() -> None:
    with pytest.raises(ValueError):
        RedirectGuard(None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ allow list
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://archive.org/details/night_of_the_living_dead",
        "https://cdn.example.com/videos/clip.mp4",
        "https://example.com/live/stream.m3u8?token=abc",
    ],
)
def test_ordinary_media_urls_pass(url: str) -> None:
    validated = validate_url(url)
    assert validated.scheme in {"http", "https"}
    assert validated.host
