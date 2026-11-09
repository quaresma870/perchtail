"""Who may call the monitoring endpoints (#366).

- The probes, GET /healthz and GET /readyz, answer a caller on loopback
  that came through no proxy (the Docker HEALTHCHECK runs inside the
  container), a caller in PROBE_ALLOWED_NETWORKS, or a caller with the
  monitoring token. Anyone else gets a bare 401 that says nothing about
  the instance's health.
- GET /monitoring/health and GET /metrics always need the monitoring token
  (app/api/monitoring.py); when MONITORING_ALLOWED_NETWORKS is set, the
  caller's address must be in it too.

The caller's address is the TCP peer as uvicorn reports it: uvicorn only
replaces it with a proxy header's value when the request came from an
address in FORWARDED_ALLOW_IPS, so a client can't choose it."""

import hashlib
import ipaddress
import secrets

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.config import Network, get_settings, parse_networks
from app.db import get_session
from app.models import MonitoringToken

# Headers a reverse proxy adds: a request carrying any of them came through
# one, so its loopback peer address says nothing about the real caller.
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_address(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    # An IPv4 caller on a dual-stack socket shows up as ::ffff:a.b.c.d.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def in_networks(request: Request, networks: tuple[Network, ...]) -> bool:
    address = client_address(request)
    return address is not None and any(address in network for network in networks)


def is_local_unproxied(request: Request) -> bool:
    """A caller on this machine that came through no reverse proxy."""
    address = client_address(request)
    if address is None or not address.is_loopback:
        return False
    return not any(header in request.headers for header in PROXY_HEADERS)


def bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is None or not authorization.startswith("Bearer "):
        return None
    return authorization.removeprefix("Bearer ").strip() or None


def token_matches(token: str, session: Session) -> bool | None:
    """Whether `token` is the monitoring token; None when none is set."""
    row = session.exec(select(MonitoringToken)).first()
    if row is None:
        return None
    return secrets.compare_digest(hash_token(token), row.token_hash)


def _refused() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def require_probe_access(request: Request, session: Session = Depends(get_session)) -> None:
    """For GET /healthz and GET /readyz: see the module docstring."""
    if is_local_unproxied(request):
        return
    if in_networks(request, parse_networks(get_settings().probe_allowed_networks)):
        return
    token = bearer_token(request)
    if token is not None and token_matches(token, session):
        return
    raise _refused()


def require_monitoring_network(request: Request) -> None:
    """For GET /monitoring/health and GET /metrics, on top of the token:
    when MONITORING_ALLOWED_NETWORKS is set, only callers in it."""
    networks = parse_networks(get_settings().monitoring_allowed_networks)
    if networks and not in_networks(request, networks):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
