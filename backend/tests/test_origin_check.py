import pytest
from app.main import OriginCheckMiddleware, _is_same_origin
from fastapi import FastAPI
from fastapi.testclient import TestClient

# TestClient's default base_url when none is given, and therefore the Host
# header every request in this file carries unless overridden.
EXPECTED_ORIGIN = "http://testserver"


@pytest.mark.parametrize(
    "candidate,expected_origin,same",
    [
        ("http://localhost:8080", "http://localhost:8080", True),
        ("http://localhost:8080/", "http://localhost:8080", True),  # trailing slash, path ignored
        ("http://localhost:8080/settings/sources", "http://localhost:8080", True),  # Referer-style
        ("https://localhost:8080", "http://localhost:8080", False),  # scheme differs
        ("http://localhost:9999", "http://localhost:8080", False),  # port differs
        ("http://evil.example", "http://localhost:8080", False),
        ("not-a-url", "http://localhost:8080", False),
        ("", "http://localhost:8080", False),
    ],
)
def test_is_same_origin(candidate, expected_origin, same):
    assert _is_same_origin(candidate, expected_origin) is same


@pytest.fixture()
def client():
    app = FastAPI()
    app.add_middleware(OriginCheckMiddleware)

    @app.get("/thing")
    def get_thing():
        return {"ok": True}

    @app.post("/thing")
    def post_thing():
        return {"ok": True}

    return TestClient(app)


def test_get_requests_are_never_checked(client):
    response = client.get("/thing", headers={"origin": "http://evil.example"})
    assert response.status_code == 200


def test_post_with_no_origin_or_referer_is_allowed(client):
    # Relies on SameSite=strict alone -- see OriginCheckMiddleware's
    # docstring for why an absent header isn't itself grounds to block.
    response = client.post("/thing")
    assert response.status_code == 200


def test_post_with_matching_origin_is_allowed(client):
    # Matches the request's own Host header (TestClient's "testserver"),
    # not a separately configured value -- see the middleware's docstring
    # for why it's self-referential.
    response = client.post("/thing", headers={"origin": EXPECTED_ORIGIN})
    assert response.status_code == 200


def test_post_with_mismatched_origin_is_blocked(client):
    response = client.post("/thing", headers={"origin": "http://evil.example"})
    assert response.status_code == 403
    assert "detail" in response.json()


def test_post_with_matching_referer_is_allowed_when_origin_absent(client):
    response = client.post("/thing", headers={"referer": f"{EXPECTED_ORIGIN}/settings/sources"})
    assert response.status_code == 200


def test_post_with_mismatched_referer_is_blocked_when_origin_absent(client):
    response = client.post("/thing", headers={"referer": "http://evil.example/attack"})
    assert response.status_code == 403


def test_origin_takes_precedence_over_a_mismatched_referer(client):
    # A real browser only ever sends one truthful Origin, but this pins the
    # precedence rule in case both happen to be present.
    response = client.post(
        "/thing",
        headers={"origin": EXPECTED_ORIGIN, "referer": "http://evil.example/attack"},
    )
    assert response.status_code == 200


def test_a_different_port_on_the_same_host_is_still_blocked(client):
    # Guards the exact local-dev shape this middleware has to get right:
    # Vite's dev server proxies API calls from its own port to a different
    # backend port (see vite.config.ts), so "same host, different port"
    # must NOT be treated as same-origin here -- vite.config.ts strips
    # Origin/Referer on proxied requests instead, falling into the
    # no-header-present case above.
    response = client.post("/thing", headers={"origin": "http://testserver:9999"})
    assert response.status_code == 403
