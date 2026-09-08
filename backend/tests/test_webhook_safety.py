import socket

import pytest
from app.webhook_safety import (
    UnsafeWebhookURLError,
    assert_webhook_url_is_safe,
    resolve_pinned_webhook_target,
)


def test_allows_a_url_resolving_to_a_public_address():
    assert_webhook_url_is_safe("https://example.com/hook")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://127.0.0.1:8000/hook",
        "http://[::1]/hook",
        "http://169.254.169.254/hook",  # cloud metadata endpoint
        "http://10.0.0.5/hook",
        "http://172.16.0.5/hook",
        "http://192.168.1.5/hook",
        "http://0.0.0.0/hook",
        "http://224.0.0.1/hook",  # multicast
    ],
)
def test_rejects_urls_resolving_to_non_public_addresses(url):
    with pytest.raises(UnsafeWebhookURLError):
        assert_webhook_url_is_safe(url)


def test_rejects_a_url_with_no_hostname():
    with pytest.raises(UnsafeWebhookURLError, match="no hostname"):
        assert_webhook_url_is_safe("file:///etc/passwd")


def test_rejects_a_hostname_that_does_not_resolve(monkeypatch):
    def _raise(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)

    with pytest.raises(UnsafeWebhookURLError, match="could not be resolved"):
        assert_webhook_url_is_safe("https://this-does-not-resolve.invalid/hook")


def test_rejects_when_any_resolved_address_is_disallowed(monkeypatch):
    # Simulates a hostname with multiple A/AAAA records where only one is
    # internal -- an attacker doesn't need every record to be private for
    # this to be exploitable, so a single disallowed hit is enough to block.
    def _mixed(host, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _mixed)

    with pytest.raises(UnsafeWebhookURLError):
        assert_webhook_url_is_safe("https://multi-record.example/hook")


# --- resolve_pinned_webhook_target -----------------------------------------
#
# assert_webhook_url_is_safe() alone doesn't close the DNS-rebinding gap at
# send time (see webhook_safety.py's module docstring) -- send_webhook()
# uses resolve_pinned_webhook_target() instead, which must connect to the
# *specific* IP it just validated, not hand a plain hostname back to httpx
# for a second, independent resolution.


def test_resolve_pinned_webhook_target_rewrites_url_to_the_validated_ip(monkeypatch):
    def _fake(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)

    target = resolve_pinned_webhook_target("https://example.com/hook?x=1")

    assert target.url == "https://93.184.216.34:443/hook?x=1"
    assert target.host_header == "example.com"
    assert target.sni_hostname == "example.com"


def test_resolve_pinned_webhook_target_preserves_url_embedded_credentials(monkeypatch):
    # httpx applies user:pass@ from the URL as Basic Auth automatically when
    # no explicit `auth` is passed -- replacing the whole netloc with just
    # the pinned IP (instead of splicing the IP in after the userinfo)
    # would silently turn every request unauthenticated.
    def _fake(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)

    target = resolve_pinned_webhook_target("https://alertuser:secret123@example.com/hook")

    assert target.url == "https://alertuser:secret123@93.184.216.34:443/hook"
    assert target.host_header == "example.com"
    assert target.sni_hostname == "example.com"


def test_resolve_pinned_webhook_target_preserves_an_explicit_port(monkeypatch):
    def _fake(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)

    target = resolve_pinned_webhook_target("http://example.com:8080/hook")

    assert target.url == "http://93.184.216.34:8080/hook"
    assert target.host_header == "example.com:8080"


def test_resolve_pinned_webhook_target_brackets_ipv6_addresses(monkeypatch):
    def _v6(host, *args, **kwargs):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _v6)

    target = resolve_pinned_webhook_target("https://example.com/hook")

    assert target.url == "https://[2606:2800:220:1:248:1893:25c8:1946]:443/hook"


def test_resolve_pinned_webhook_target_rejects_no_hostname():
    with pytest.raises(UnsafeWebhookURLError, match="no hostname"):
        resolve_pinned_webhook_target("file:///etc/passwd")


def test_resolve_pinned_webhook_target_rejects_a_disallowed_address(monkeypatch):
    def _private(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _private)

    with pytest.raises(UnsafeWebhookURLError):
        resolve_pinned_webhook_target("https://rebinds-to-internal.example/hook")


def test_pinned_target_survives_dns_flipping_to_private_on_a_later_lookup(monkeypatch):
    """The actual DNS-rebind regression test: simulates an attacker's
    authoritative DNS server answering the first query (the one
    resolve_pinned_webhook_target itself issues) with a public address and
    every subsequent query with a private one. The old vulnerable pattern
    (assert_webhook_url_is_safe() followed by httpx.post(plain_url)) would
    pass validation on the first lookup and then connect using whatever a
    *second* lookup returns. Confirms resolve_pinned_webhook_target only
    ever resolves once, and the target it returns is already a numeric IP
    literal -- there is nothing left for a later lookup to affect."""
    calls = {"n": 0}

    def _flip(host, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _flip)

    target = resolve_pinned_webhook_target("https://rebinds-to-internal.example/hook")

    assert calls["n"] == 1
    assert target.url == "https://93.184.216.34:443/hook"
    assert target.sni_hostname == "rebinds-to-internal.example"
