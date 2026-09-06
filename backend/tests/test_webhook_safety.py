import socket

import pytest
from app.webhook_safety import UnsafeWebhookURLError, assert_webhook_url_is_safe


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
