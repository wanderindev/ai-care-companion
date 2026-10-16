"""Download a PDF from a lab portal link, defensively (#16).

The URL comes from a chat message, so this is an SSRF surface. Rules, checked
on the first URL and again on **every redirect hop** (at most
``MAX_REDIRECTS``):

- ``https`` only, no user info in the URL, a host that matches the
  ``LAB_LINK_HOSTS`` allow-list (``host_allowed``);
- the host is resolved here and **every** address it resolves to must be
  public (``is_public_ip``: no private, loopback, link-local — incl. the
  169.254.169.254 metadata address —, shared, reserved, multicast or
  unspecified ranges, in IPv4, IPv6, IPv4-mapped IPv6 or NAT64 form);
- the connection is pinned to the validated address (the request goes to the
  IP, with the original ``Host`` header and TLS SNI/certificate check against
  the host name), so a second DNS answer cannot redirect it;
- connect/read timeouts and a total deadline, the body streamed up to
  ``LAB_PDF_MAX_MB`` (``Accept-Encoding: identity``), no cookies, no
  credentials, no proxy from the environment, a plain User-Agent;
- the result is a PDF only if its **bytes** start with ``%PDF-`` (the
  Content-Type is not trusted).

``fetch_pdf`` returns a ``FetchResult`` (the bytes, or a short reason code
why not) or raises ``TransientFetchError`` (timeouts, connection errors, DNS
failures, 429/5xx), which the job retries. It never logs the URL.
"""

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from urllib.parse import urljoin, urlsplit

import httpx2 as httpx

from app.services.attachments import PDF, sniff

MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
TOTAL_SECONDS = 60.0
USER_AGENT = "n16chat-lab-fetch/1.0"
READ_CHUNK = 64 * 1024
MB = 1024 * 1024
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

# Reason codes (stored in ``lab_links.reason``; the UI maps them to Spanish).
BLOCKED = "blocked"  # a fetch rule refused the URL or one of its redirects
NOT_PDF = "not_pdf"  # the answer was not a PDF (a login page, an HTML viewer…)
HTTP_ERROR = "http_error"  # 4xx, or a redirect without a target
TOO_LARGE = "too_large"
TOO_MANY_REDIRECTS = "too_many_redirects"

Resolver = Callable[[str, int], list[str]]


class TransientFetchError(Exception):
    """Worth retrying later (network, DNS, 429/5xx). Its text is never stored."""


@dataclass(frozen=True)
class FetchResult:
    data: bytes | None = None
    reason: str | None = None


def host_allowed(host: str, patterns: tuple[str, ...]) -> bool:
    """``host`` (lowercase, no port) matches one of the allow-list globs."""
    host = host.lower().rstrip(".")
    return bool(host) and any(fnmatchcase(host, p) for p in patterns)


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not (
        ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_link_local
    )


def resolve_host(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (the worker's resolver)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise TransientFetchError("dns") from None
    return [info[4][0] for info in infos]


@dataclass(frozen=True)
class _Target:
    url: httpx.URL  # with the validated IP as host
    host_header: str
    host: str


def _check(url: str, patterns: tuple[str, ...], resolver: Resolver) -> _Target | None:
    """The pinned target for ``url``, or None when a rule refuses it."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password:
        return None
    if not host.isascii() or not host_allowed(host, patterns):
        return None
    addresses = resolver(host, port or 443)
    if not addresses or not all(is_public_ip(a) for a in addresses):
        return None
    try:
        pinned = httpx.URL(url).copy_with(host=addresses[0].split("%", 1)[0], fragment=None)
    except Exception:
        return None
    host_header = host if port in (None, 443) else f"{host}:{port}"
    return _Target(pinned, host_header, host)


def _read_capped(response: httpx.Response, cap: int, deadline: float) -> bytes | None:
    """The body, or None when it is over ``cap`` bytes."""
    length = response.headers.get("content-length")
    if length and length.isdigit() and int(length) > cap:
        return None
    body = bytearray()
    for chunk in response.iter_raw(READ_CHUNK):
        body.extend(chunk)
        if len(body) > cap:
            return None
        if time.monotonic() > deadline:
            raise TransientFetchError("deadline")
    return bytes(body)


def fetch_pdf(
    url: str,
    patterns: tuple[str, ...],
    max_bytes: int,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> FetchResult:
    """Follow ``url`` (and up to ``MAX_REDIRECTS`` redirects) under the rules above."""
    resolver = resolver or resolve_host
    deadline = time.monotonic() + TOTAL_SECONDS
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        target = _check(current, patterns, resolver)
        if target is None:
            return FetchResult(reason=BLOCKED)
        request = httpx.Request(
            "GET",
            target.url,
            headers={
                "Host": target.host_header,
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,*/*;q=0.5",
                "Accept-Encoding": "identity",
            },
            extensions={"sni_hostname": target.host},
        )
        # A fresh client per hop: no cookie or connection carried across hops.
        with httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0),
            trust_env=False,
            follow_redirects=False,
            timeout=TIMEOUT,
        ) as client:
            try:
                response = client.send(request, stream=True)
            except httpx.TransportError:  # timeouts, connection and protocol errors
                raise TransientFetchError("network") from None
            try:
                status = response.status_code
                if status in REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location:
                        return FetchResult(reason=HTTP_ERROR)
                    current = urljoin(current, location)
                    continue
                if status == 429 or status >= 500:
                    raise TransientFetchError("status")
                if status != 200:
                    return FetchResult(reason=HTTP_ERROR)
                try:
                    body = _read_capped(response, max_bytes, deadline)
                except httpx.TransportError:
                    raise TransientFetchError("network") from None
            finally:
                response.close()
        if body is None:
            return FetchResult(reason=TOO_LARGE)
        if sniff(body) is not PDF:
            return FetchResult(reason=NOT_PDF)
        return FetchResult(data=body)
    return FetchResult(reason=TOO_MANY_REDIRECTS)
