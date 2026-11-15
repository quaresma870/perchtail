"""Where a request came from, for the audit log and the monitoring
allowlists.

ClientAddressMiddleware resolves the client's address once per request and
puts it in the ASGI scope, so everything downstream (request.client, the
monitoring allowlists in app/monitoring_access.py, the login throttle's
audit entries) sees the same answer. A request whose socket peer is in
TRUSTED_PROXIES has its X-Forwarded-For read from the right: every hop that
is itself a trusted proxy is skipped, and the first one that isn't is the
client. Entries to the left of that one were written by the client itself,
so they are never believed. A request from anywhere else keeps its socket
address, and its X-Forwarded-For is ignored.

It also starts the request's audit context (RequestContext): the client
address, the User-Agent, and, once the session cookie has been checked
(app/api/auth.py), the session and the user. app.audit.record_audit_event
reads it, so every entry written while handling a request says where it
came from without each caller passing it. Background jobs and the CLI run
outside any request, so their entries leave these fields empty.

A request that carries X-Forwarded-For or Forwarded from a peer outside
TRUSTED_PROXIES usually means PerchTail sits behind a proxy that isn't
listed there: every client then shows up with the proxy's address and
shares its per-address login lockout (app/login_throttle.py). The first such
request after startup logs a WARNING, and /monitoring/health reports
trusted_proxies_suspect until the next restart."""

import ipaddress
from contextvars import ContextVar
from dataclasses import dataclass

from app.config import Network, get_settings, parse_networks
from app.logging_config import get_logger

logger = get_logger(__name__)

# User-Agent values are recorded up to this many characters.
USER_AGENT_MAX = 256
# How much of the session token's SHA-256 identifies a session in the audit log.
SESSION_REF_LENGTH = 16


@dataclass
class RequestContext:
    client_ip: str | None = None
    user_agent: str | None = None
    # Set once the session cookie is checked (app/api/auth.py's
    # get_current_user). A mutable object, not separate context variables:
    # FastAPI runs sync dependencies in a copy of the context, so a value set
    # there would never reach the endpoint, while a change to this shared
    # object does.
    session_id: str | None = None
    user_id: int | None = None
    # Set when a 403 needs no access.denied entry from the central handler
    # (app/api/errors.py): the endpoint already audited the refusal itself
    # (the file.*_denied events), or it isn't a refused permission at all
    # (a forced password change).
    denial_handled: bool = False


_current: ContextVar[RequestContext | None] = ContextVar("perchtail_request_context", default=None)

# Set by the first request since startup that carried a forwarding header
# from a peer outside TRUSTED_PROXIES (see the module docstring).
_untrusted_forwarded_seen = False


def trusted_proxies_suspect() -> bool:
    """Whether a forwarding header has arrived from an untrusted peer since
    startup."""
    return _untrusted_forwarded_seen


def reset_for_tests() -> None:
    global _untrusted_forwarded_seen
    _untrusted_forwarded_seen = False


def _note_untrusted_forwarded(peer: str) -> None:
    global _untrusted_forwarded_seen
    if _untrusted_forwarded_seen:
        return
    _untrusted_forwarded_seen = True
    logger.warning("client_address.untrusted_forwarded_header", peer=peer)


def current() -> RequestContext | None:
    """The audit context of the request being handled, or None outside one."""
    return _current.get()


def session_ref(token_hash: str) -> str:
    """The short reference to a session recorded in the audit log: a prefix
    of the token's SHA-256, which is what the database already holds. Never
    the token."""
    return token_hash[:SESSION_REF_LENGTH]


def truncate_user_agent(value: str | None) -> str | None:
    if not value:
        return None
    return value[:USER_AGENT_MAX]


def _address(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    # An IPv4 caller on a dual-stack socket shows up as ::ffff:a.b.c.d.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _trusted(address, networks: tuple[Network, ...]) -> bool:
    return any(address in network for network in networks)


def resolve_client(peer: str | None, forwarded_for: list[str], trusted: tuple[Network, ...]):
    """The client's address for a request whose socket peer is `peer` and
    that carries these X-Forwarded-For header values, in order. Returns
    the address as a string, or `peer` unchanged when it isn't an address
    (or None)."""
    if peer is None:
        return None
    current_address = _address(peer)
    if current_address is None:
        return peer
    if not _trusted(current_address, trusted):
        return str(current_address)
    hops = [hop for value in forwarded_for for hop in value.split(",")]
    for hop in reversed(hops):
        address = _address(hop)
        if address is None:
            # Not an address: whoever wrote it can't be believed, so the
            # client is the trusted hop that passed it on.
            break
        current_address = address
        if not _trusted(address, trusted):
            break
    return str(current_address)


class ClientAddressMiddleware:
    """See the module docstring. Pure ASGI, so the context variable it sets
    is seen by everything the request runs, including the endpoint's thread
    pool."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        forwarded_for = [
            value.decode("latin-1") for name, value in headers if name == b"x-forwarded-for"
        ]
        user_agent = next(
            (value.decode("latin-1") for name, value in headers if name == b"user-agent"), None
        )
        client = scope.get("client")
        peer = client[0] if client else None
        trusted = parse_networks(get_settings().trusted_proxies)
        if peer is not None and not _untrusted_forwarded_seen:
            forwarded = forwarded_for or any(name == b"forwarded" for name, _ in headers)
            peer_address = _address(peer)
            if forwarded and (peer_address is None or not _trusted(peer_address, trusted)):
                _note_untrusted_forwarded(peer)
        resolved = resolve_client(peer, forwarded_for, trusted)
        if client and resolved != peer:
            scope = {**scope, "client": (resolved, 0)}
        token = _current.set(
            RequestContext(client_ip=resolved, user_agent=truncate_user_agent(user_agent))
        )
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
