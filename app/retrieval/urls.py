"""URL canonicalisation and public-address validation."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


class URLValidationError(ValueError):
    """Raised when a target URL is not safe to retrieve."""


_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "oly_anon_id",
    "oly_enc_id",
    "ref_src",
    "spm",
    "vero_conv",
    "vero_id",
    "yclid",
}
_COMMON_TWO_PART_SUFFIXES = {
    "ac.in",
    "ac.lk",
    "ac.nz",
    "ac.uk",
    "co.in",
    "co.jp",
    "co.lk",
    "co.nz",
    "co.uk",
    "com.au",
    "com.br",
    "com.cn",
    "com.hk",
    "com.sg",
    "edu.au",
    "edu.in",
    "edu.lk",
    "edu.sg",
    "gov.au",
    "gov.in",
    "gov.lk",
    "gov.uk",
    "net.au",
    "net.cn",
    "org.au",
    "org.cn",
    "org.uk",
}
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}
_BLOCKED_SUFFIXES = (".localhost",)
_BLOCKED_SOURCE_HOSTS = frozenset({"licdn.com", "linkedin.cn", "linkedin.com", "lnkd.in"})
_BLOCKED_SOURCE_SUFFIXES = tuple(f".{host}" for host in sorted(_BLOCKED_SOURCE_HOSTS))
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}


def _host_to_ascii(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLValidationError("URL host is invalid") from exc


def _normalise_path(path: str) -> str:
    if not path:
        return "/"
    return quote(path, safe="/:@!$&'()*+,;=-._~%")


def _is_tracking_param(name: str) -> bool:
    lower = name.lower()
    return lower.startswith("utm_") or lower in _TRACKING_PARAMS


def canonicalise_url(url: str) -> str:
    """Return a stable URL key without fragments or common tracking query params."""

    raw_url = url.strip()
    if "\\" in raw_url or any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise URLValidationError("URL contains unsafe characters")
    if any(char.isspace() for char in raw_url):
        raise URLValidationError("URL contains unsafe whitespace")

    try:
        parsed = urlsplit(raw_url)
        if not parsed.scheme:
            parsed = urlsplit("https://" + raw_url)
    except ValueError as exc:
        raise URLValidationError("URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise URLValidationError("Only HTTP and HTTPS URLs are supported")
    if not parsed.hostname:
        raise URLValidationError("URL host is required")
    if parsed.username or parsed.password:
        raise URLValidationError("URL credentials are not allowed")

    scheme = parsed.scheme.lower()
    host = _host_to_ascii(parsed.hostname)
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("URL port is invalid") from exc
    include_port = port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443))
    bracketed_host = f"[{host}]" if ":" in host else host
    netloc = f"{bracketed_host}:{port}" if include_port else bracketed_host

    query_pairs = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, _normalise_path(parsed.path), query, ""))


def domain_key(url: str) -> str:
    """Return a conservative registrable-domain key for fetch concurrency."""

    host = _host_to_ascii(urlsplit(canonicalise_url(url)).hostname or "")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass

    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host

    suffix = ".".join(labels[-2:])
    if suffix in _COMMON_TWO_PART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_blocked_source_host(host: str) -> bool:
    """Return whether automated source access is forbidden for this exact host tree."""

    normalized = _host_to_ascii(host)
    return normalized in _BLOCKED_SOURCE_HOSTS or normalized.endswith(_BLOCKED_SOURCE_SUFFIXES)


def _reject_blocked_hostname(host: str) -> None:
    if is_blocked_source_host(host):
        raise URLValidationError("URL host is blocked by the automated-source policy")
    if host in _BLOCKED_HOSTS or any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        raise URLValidationError("URL host is not public")


def _validate_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if ip in _METADATA_IPS or not ip.is_global:
        raise URLValidationError("URL resolves to a non-public address")


def _resolve_addresses(host: str, port: int | None) -> set[str]:
    try:
        results = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLValidationError("URL host could not be resolved") from exc

    addresses = {item[4][0] for item in results}
    if not addresses:
        raise URLValidationError("URL host could not be resolved")
    return addresses


def resolve_public_addresses(host: str, port: int | None = None) -> set[str]:
    """Resolve a host once and return public IPs, rejecting mixed DNS answers."""

    host = _host_to_ascii(host)
    _reject_blocked_hostname(host)
    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        addresses = _resolve_addresses(host, port)
        public_addresses: set[str] = set()
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise URLValidationError("DNS returned an invalid address") from exc
            _validate_public_ip(ip)
            public_addresses.add(ip.compressed)
        return public_addresses

    _validate_public_ip(host_ip)
    return {host_ip.compressed}


async def validate_public_url(url: str, settings: object | None = None) -> str:
    """Canonicalise a target URL and reject unsupported or non-public destinations."""

    canonical = canonicalise_url(url)
    parsed = urlsplit(canonical)
    host = parsed.hostname or ""
    _reject_blocked_hostname(host)

    await asyncio.to_thread(resolve_public_addresses, host, parsed.port)

    return canonical
