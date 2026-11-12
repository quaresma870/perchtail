"""Every HTTP operation in the OpenAPI schema refuses an anonymous caller
(#376), except the closed list of public ones below.

A new endpoint that forgets its authentication dependency fails here. Adding
a public endpoint means adding it to PUBLIC_OPERATIONS with the reason."""

import re

import pytest
from app.db import get_session
from app.main import app
from fastapi.testclient import TestClient

PUBLIC_OPERATIONS = {
    # Signing in is how a caller gets a session in the first place.
    ("POST", "/auth/login"),
    # Clearing a session needs none: without a cookie it only expires the cookie.
    ("POST", "/auth/logout"),
    # Starts the SSO redirect for a caller who is not signed in yet.
    ("GET", "/auth/sso/login"),
    # The identity provider sends the not-yet-signed-in browser back here.
    ("GET", "/auth/sso/callback"),
    # The login page asks whether to show the SSO button before anyone signs in.
    ("GET", "/auth/sso/status"),
}

HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# Not loopback, in no allowed network, so the probes don't let it in either.
REMOTE_CLIENT = ("203.0.113.7", 1)


def _operations() -> list[tuple[str, str]]:
    paths = app.openapi()["paths"]
    return sorted(
        (method.upper(), path)
        for path, item in paths.items()
        for method in item
        if method in HTTP_METHODS
    )


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture()
def anonymous_client(session):
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app, client=REMOTE_CLIENT, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_public_operations_still_exist():
    missing = sorted(PUBLIC_OPERATIONS - set(_operations()))
    assert (
        not missing
    ), "PUBLIC_OPERATIONS lists operations the app no longer has; remove them: " + ", ".join(
        f"{method} {path}" for method, path in missing
    )


def test_every_operation_refuses_an_anonymous_caller(anonymous_client):
    answered = []
    for method, path in _operations():
        if (method, path) in PUBLIC_OPERATIONS:
            continue
        response = anonymous_client.request(method, _concrete(path))
        if response.status_code not in (401, 403):
            answered.append(f"{method} {path} -> {response.status_code}")
    assert not answered, (
        "These operations answered a caller with no session (expected 401 or 403); "
        "add the authentication dependency, or list them in PUBLIC_OPERATIONS with "
        "the reason:\n" + "\n".join(answered)
    )
