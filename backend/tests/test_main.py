from app.main import app
from fastapi.testclient import TestClient


def test_healthz():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_sets_request_id_header():
    client = TestClient(app)
    response = client.get("/healthz")
    assert "x-request-id" in response.headers
