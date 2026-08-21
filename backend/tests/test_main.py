import asyncio

import pytest
from app.config import get_settings
from app.main import app, lifespan
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


def test_lifespan_refuses_to_start_with_the_default_credential_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "changeme")
    get_settings.cache_clear()

    async def _enter_and_exit_lifespan():
        async with lifespan(app):
            pass

    try:
        with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
            asyncio.run(_enter_and_exit_lifespan())
    finally:
        get_settings.cache_clear()
