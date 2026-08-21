"""SSRF guard for Alert.webhook_url (see app/alerts.py, app/api/alerts.py).

A webhook URL is user-supplied but fetched by the server itself
(app.alerts.send_webhook), so an attacker can point it at loopback,
link-local, or other internal addresses to probe or reach infrastructure
that would otherwise be unreachable from outside. Resolve the hostname and
reject anything that resolves to a non-public address.

Call assert_webhook_url_is_safe() at two points, not just one: alert
create/update time (fast feedback to the user) and again immediately
before send_webhook() actually connects. Checking only at create/update
time leaves a DNS-rebinding gap -- the hostname can resolve to something
public when the alert is saved and to something private by the time it's
actually dispatched.
"""

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeWebhookURLError(ValueError):
    """Raised when a webhook URL's host resolves to a disallowed address."""


def _is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_webhook_url_is_safe(url: str) -> None:
    """Raises UnsafeWebhookURLError if url has no hostname, its hostname
    can't be resolved, or any of its resolved addresses is loopback,
    link-local, private, reserved, multicast, or unspecified."""
    hostname = urlparse(url).hostname
    if not hostname:
        raise UnsafeWebhookURLError(f"webhook_url has no hostname: {url!r}")

    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeWebhookURLError(
            f"webhook_url hostname could not be resolved: {hostname!r}"
        ) from exc

    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_disallowed(ip):
            raise UnsafeWebhookURLError(
                f"webhook_url {hostname!r} resolves to a disallowed address: {ip}"
            )
