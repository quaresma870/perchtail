"""SSRF guard for Alert.webhook_url (see app/alerts.py, app/api/alerts.py).

A webhook URL is user-supplied but fetched by the server itself
(app.alerts.send_webhook), so an attacker can point it at loopback,
link-local, or other internal addresses to probe or reach infrastructure
that would otherwise be unreachable from outside. Resolve the hostname and
reject anything that resolves to a non-public address.

Call assert_webhook_url_is_safe() at alert create/update time for fast
feedback to the user. That alone is NOT enough at send time: a hostname
validated when the alert was saved can resolve to something completely
different (DNS rebinding) by the time it's actually dispatched, including
between one resolution and the very next one a moment later -- checking
the same hostname twice in a row, right before connecting, narrows that
window but does not close it, since httpx (like any HTTP client handed a
plain hostname) performs its own independent resolution when it opens the
connection, with no memory of whatever assert_webhook_url_is_safe() just
saw. resolve_pinned_webhook_target() is the fix used at send time: it
resolves and validates once, then hands back a URL rewritten to connect to
that *specific already-validated IP* directly, with the original hostname
preserved as the Host header and TLS SNI/server_hostname (so an https URL
still gets certificate hostname verification against the real hostname,
not the IP literal) -- nothing resolves the hostname a second time between
validation and connection, which is what actually closes the gap.

The system SMTP server (app/email_delivery.py) goes through the same
resolution with a looser policy, resolve_validated_smtp_ips(): it's set by a
super admin and is often an internal relay, so private addresses are
allowed, but loopback, link-local (cloud metadata included), multicast,
unspecified and reserved addresses are not. The caller connects to the IP it
returns, never to the hostname, for the same rebinding reason.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse


class UnsafeWebhookURLError(ValueError):
    """Raised when a webhook URL's host resolves to a disallowed address."""


class UnsafeSmtpHostError(ValueError):
    """Raised when the SMTP server's host can't be resolved or resolves to
    an address the SMTP policy refuses."""


Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _unmapped(ip: Address) -> Address:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_disallowed(ip: Address) -> bool:
    """Anything not publicly routable is refused -- an allowlist (is_global),
    not a list of known-bad ranges, so carrier-grade NAT, documentation and
    benchmarking ranges and the like are covered too."""
    ip = _unmapped(ip)
    return not ip.is_global or ip.is_multicast


def _is_disallowed_for_smtp(ip: Address) -> bool:
    """Private and global addresses are allowed (an internal relay is the
    usual SMTP server); the host itself, link-local (which holds cloud
    metadata services), multicast, unspecified and reserved addresses
    aren't."""
    ip = _unmapped(ip)
    return (
        ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
    )


def _resolve(hostname: str, *, disallowed, error, label: str) -> list[str]:
    """Resolves hostname and returns every address it maps to, raising
    `error` if resolution fails or ANY resolved address is `disallowed` --
    shared by the save-time checks and the send-time pinning, so they can
    never drift into checking different things."""
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise error(f"{label} hostname could not be resolved: {hostname!r}") from exc

    ips = []
    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if disallowed(ip):
            raise error(f"{label} {hostname!r} resolves to a disallowed address: {ip}")
        ips.append(sockaddr[0])
    if not ips:
        raise error(f"{label} hostname could not be resolved: {hostname!r}")
    return ips


def _resolve_validated_ips(hostname: str) -> list[str]:
    """The webhook policy: every resolved address must be public."""
    return _resolve(
        hostname, disallowed=_is_disallowed, error=UnsafeWebhookURLError, label="webhook_url"
    )


def resolve_validated_smtp_ips(hostname: str) -> list[str]:
    """The SMTP policy (see _is_disallowed_for_smtp): every address the host
    resolves to, raising UnsafeSmtpHostError when it can't be resolved or
    any address is refused. Used when the settings are saved and again
    before every send, which then connects to the first returned IP."""
    return _resolve(
        hostname,
        disallowed=_is_disallowed_for_smtp,
        error=UnsafeSmtpHostError,
        label="The SMTP server",
    )


def assert_webhook_url_is_safe(url: str) -> None:
    """Raises UnsafeWebhookURLError if url has no hostname, its hostname
    can't be resolved, or any of its resolved addresses is loopback,
    link-local, private, reserved, multicast, or unspecified."""
    hostname = urlparse(url).hostname
    if not hostname:
        raise UnsafeWebhookURLError(f"webhook_url has no hostname: {url!r}")
    _resolve_validated_ips(hostname)


@dataclass(frozen=True)
class PinnedWebhookTarget:
    """The result of resolve_pinned_webhook_target() below -- connect to
    `url` (host replaced with the already-validated IP literal) and pass
    `host_header`/`sni_hostname` through so the request still looks correct
    to the receiving server and to certificate verification."""

    url: str
    host_header: str
    sni_hostname: str


def resolve_pinned_webhook_target(url: str) -> PinnedWebhookTarget:
    """Resolves and validates url's hostname exactly like
    assert_webhook_url_is_safe, then returns a target rewritten to connect
    directly to one of the validated IPs -- see this module's docstring for
    why this, not a second call to assert_webhook_url_is_safe, is what
    actually closes the DNS-rebinding gap at send time. Raises
    UnsafeWebhookURLError under the same conditions as
    assert_webhook_url_is_safe."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeWebhookURLError(f"webhook_url has no hostname: {url!r}")

    validated_ips = _resolve_validated_ips(hostname)
    pinned_ip = validated_ips[0]

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ip_and_port = f"[{pinned_ip}]:{port}" if ":" in pinned_ip else f"{pinned_ip}:{port}"
    # Preserve a URL-embedded user:pass@ (httpx applies it as Basic Auth
    # automatically from the URL when no explicit `auth` is passed) --
    # replacing the whole netloc with just the pinned IP would silently
    # drop it and turn every request unauthenticated.
    userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    netloc = f"{userinfo}{ip_and_port}"
    pinned_url = urlunparse(parsed._replace(netloc=netloc))

    host_header = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    return PinnedWebhookTarget(url=pinned_url, host_header=host_header, sni_hostname=hostname)
