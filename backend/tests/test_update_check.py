"""#215: the release check, the version comparison and the Updates API."""

from datetime import timedelta

import httpx
import pytest
from app import update_check
from app.api.auth import get_current_active_user
from app.api.updates import router as updates_router
from app.auth.models import Role, User
from app.db import get_session
from app.models import UpdateCheckState
from app.timeutils import utcnow
from app.versions import is_final, is_newer, parse_version
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

RELEASE = {
    "tag_name": "v0.6.0",
    "name": "v0.6.0",
    "html_url": "https://github.com/quaresma870/perchtail/releases/tag/v0.6.0",
    "published_at": "2026-12-01T10:00:00Z",
}


# --- versions --------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate, current, newer",
    [
        ("0.5.3", "0.5.3-rc.1", True),  # final after its rc
        ("0.5.3-rc.2", "0.5.3-rc.1", True),
        ("0.5.3-rc.1", "0.5.2", True),
        ("0.5.3", "0.5.3", False),
        ("0.5.2", "0.5.3", False),  # a server newer than the latest release (dev build)
        ("0.10.0", "0.9.9", True),  # numeric, not lexical
        ("v0.6.0", "0.5.3", True),
        ("0.6.0", "dev", False),  # unknown versions are never compared
        ("garbage", "0.5.3", False),
        (None, "0.5.3", False),
        # Betas sort before release candidates, which sort before the final.
        ("0.7.0-beta.2", "0.7.0-beta.1", True),
        ("0.7.0-beta.10", "0.7.0-beta.2", True),  # numeric, not lexical
        ("0.7.0-rc.1", "0.7.0-beta.10", True),
        ("0.7.0", "0.7.0-beta.10", True),
        ("0.7.0-beta.1", "0.7.0-rc.1", False),
        ("0.7.0-beta.1", "0.7.0", False),
        ("0.7.0-beta.1", "0.6.0", True),
        ("0.7.0-beta.1", "0.6.1-rc.1", True),
        ("v0.7.0-beta.1", "0.7.0-beta.1", False),
        ("0.7.0-beta.1", "v0.7.0-beta.1", False),
        ("0.7.0-beta.1", "dev", False),
    ],
)
def test_is_newer(candidate, current, newer):
    assert is_newer(candidate, current) is newer


def test_versions_sort_beta_then_rc_then_final():
    order = ["0.6.0", "0.6.1-rc.1", "0.7.0-beta.1", "0.7.0-beta.2", "0.7.0-beta.10"]
    order += ["0.7.0-rc.1", "0.7.0"]
    for i, a in enumerate(order):
        for j, b in enumerate(order):
            assert is_newer(a, b) is (i > j), (a, b)


@pytest.mark.parametrize(
    "value, parsed",
    [
        ("0.7.0-beta.3", (0, 7, 0, 0, 3)),
        ("v0.7.0-beta.3", (0, 7, 0, 0, 3)),
        ("0.7.0-rc.2", (0, 7, 0, 1, 2)),
        ("0.7.0", (0, 7, 0, 2, 0)),
        ("v1.2.3", (1, 2, 3, 2, 0)),
    ],
)
def test_parse_version(value, parsed):
    assert parse_version(value) == parsed


def test_parse_version_rejects_anything_else():
    for value in (
        "0.5",
        "0.5.3-beta",
        "0.5.3-rc",
        "0.5.3.1",
        " 0.5.3",
        "0.5.3\n",
        "0.7.0-alpha.1",
        "0.7.0-beta",
        "0.7.0-beta.",
        "0.7.0-beta.1.2",
        "0.7.0-BETA.1",
        "0.7.0-beta.12345",
        "0.7.0-rc.1-beta.1",
        "0.7.0-beta.1\n",
        "dev",
        "",
        None,
    ):
        assert parse_version(value) is None
        assert not is_final(value)


def test_is_final():
    assert is_final("0.7.0") and is_final("v0.7.0")
    for value in ("0.7.0-beta.1", "v0.7.0-rc.1", "dev", None):
        assert not is_final(value)


# --- the check -------------------------------------------------------------


class FakeGitHub:
    """A transport that answers like GitHub's releases/latest and records
    the requests."""

    def __init__(self, status=200, body=None, etag='"abc"', error=None):
        self.status, self.body, self.etag, self.error = status, body, etag, error
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error:
                raise self.error
            headers = {"ETag": self.etag} if self.etag else {}
            return httpx.Response(self.status, json=self.body, headers=headers)

        return httpx.MockTransport(handle)


@pytest.fixture()
def running_053(monkeypatch):
    monkeypatch.setattr(update_check, "APP_VERSION", "0.5.3")


def test_a_newer_release_is_recorded_and_offered(session, running_053):
    github = FakeGitHub(body=RELEASE)
    state = update_check.check_now(session, transport=github.transport())

    assert (state.latest_version, state.latest_name) == ("0.6.0", "v0.6.0")
    assert state.latest_url == RELEASE["html_url"]
    assert state.latest_published_at.isoformat() == "2026-12-01T10:00:00"
    assert state.etag == '"abc"' and state.last_error is None
    assert update_check.update_available(state)
    request = github.requests[0]
    assert str(request.url) == update_check.LATEST_RELEASE_URL
    assert request.headers["User-Agent"] == "PerchTail"
    assert "If-None-Match" not in request.headers


def test_a_second_check_sends_the_etag_and_keeps_the_release_on_304(session, running_053):
    update_check.check_now(session, transport=FakeGitHub(body=RELEASE).transport())
    github = FakeGitHub(status=304, body=None)
    state = update_check.check_now(session, transport=github.transport())

    assert github.requests[0].headers["If-None-Match"] == '"abc"'
    assert state.latest_version == "0.6.0" and state.last_error is None


@pytest.mark.parametrize(
    "github",
    [
        FakeGitHub(status=500, body={"message": "boom"}),
        FakeGitHub(error=httpx.ConnectError("no route to host")),
        FakeGitHub(body={**RELEASE, "tag_name": "<script>"}),
        FakeGitHub(body=["not", "an", "object"]),
    ],
)
def test_a_failure_is_recorded_without_losing_the_last_good_answer(session, running_053, github):
    update_check.check_now(session, transport=FakeGitHub(body=RELEASE).transport())
    good = session.exec(select(UpdateCheckState)).one().last_success_at

    state = update_check.check_now(session, transport=github.transport())

    assert state.last_error
    assert state.latest_version == "0.6.0"
    assert state.last_success_at == good


def test_proxy_credentials_in_an_error_are_not_recorded(session, running_053):
    error = httpx.ProxyError("can't reach http://alice:hunter2@proxy.example:3128")
    state = update_check.check_now(session, transport=FakeGitHub(error=error).transport())
    assert "hunter2" not in state.last_error and "alice" not in state.last_error
    assert "//***@proxy.example:3128" in state.last_error


def test_the_state_is_one_row(session, running_053):
    for _ in range(2):
        update_check.check_now(session, transport=FakeGitHub(body=RELEASE).transport())
        session.commit()
    rows = session.exec(select(UpdateCheckState)).all()
    assert [row.id for row in rows] == [update_check.STATE_ID]


def test_a_link_outside_the_repository_is_dropped(session, running_053):
    body = {**RELEASE, "html_url": "https://evil.example/phish", "name": "x" * 500}
    state = update_check.check_now(session, transport=FakeGitHub(body=body).transport())
    assert state.latest_url is None
    assert len(state.latest_name) == 120


def test_nothing_is_offered_when_this_is_the_latest(session, running_053):
    body = {**RELEASE, "tag_name": "v0.5.3"}
    state = update_check.check_now(session, transport=FakeGitHub(body=body).transport())
    assert not update_check.update_available(state)


@pytest.mark.parametrize("tag", ["v0.7.0-rc.1", "v0.7.0-beta.1"])
def test_a_pre_release_is_never_offered(session, running_053, tag):
    update_check.check_now(session, transport=FakeGitHub(body=RELEASE).transport())

    body = {**RELEASE, "tag_name": tag}
    state = update_check.check_now(session, transport=FakeGitHub(body=body).transport())

    assert state.latest_version == "0.6.0"
    assert state.last_error.startswith("the latest release is a pre-release:")
    assert tag in state.last_error


@pytest.mark.parametrize("latest, available", [("v0.6.0", False), ("v0.7.0", True)])
def test_a_beta_server_is_offered_only_its_final_release(session, monkeypatch, latest, available):
    monkeypatch.setattr(update_check, "APP_VERSION", "0.7.0-beta.1")
    body = {**RELEASE, "tag_name": latest}
    state = update_check.check_now(session, transport=FakeGitHub(body=body).transport())
    assert state.last_error is None
    assert update_check.update_available(state) is available


def test_a_new_release_is_logged_once(session, running_053, monkeypatch):
    logged = []
    monkeypatch.setattr(update_check.logger, "info", lambda event, **kw: logged.append((event, kw)))
    for _ in range(2):
        update_check.check_now(session, transport=FakeGitHub(body=RELEASE).transport())
    assert [event for event, _ in logged] == ["update_check.available"]


# --- the scheduled job -----------------------------------------------------


@pytest.fixture()
def job(session, monkeypatch):
    """run_update_check against the test session, with check_now recorded."""
    calls = []
    monkeypatch.setattr("app.db.engine", session.get_bind())
    monkeypatch.setattr(update_check, "check_now", lambda s: calls.append(1))
    return calls


def test_the_job_checks_when_on_and_a_day_has_passed(session, job):
    update_check.run_update_check()
    assert job == [1]


def test_the_job_waits_a_day_between_checks(session, job):
    state = update_check.get_state(session)
    state.last_checked_at = utcnow() - timedelta(hours=23)
    session.commit()
    update_check.run_update_check()
    assert job == []


def test_the_job_makes_no_request_when_turned_off(session, job, monkeypatch):
    from app import system_settings

    system_settings.set_bool(session, "update_check_enabled", False)
    session.commit()
    update_check.run_update_check()
    assert job == []

    system_settings.set_bool(session, "update_check_enabled", True)
    session.commit()
    monkeypatch.setattr(update_check.get_settings(), "update_check", False)
    update_check.run_update_check()
    assert job == []


# --- API -------------------------------------------------------------------


def _user(session, *, super_admin: bool) -> User:
    role = Role(name=f"role-{super_admin}-{utcnow().timestamp()}", is_super_admin=super_admin)
    session.add(role)
    session.commit()
    user = User(username=f"u{role.id}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _client(session, user) -> TestClient:
    app = FastAPI()
    app.include_router(updates_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def test_the_updates_api_is_for_super_admins_only(session):
    client = _client(session, _user(session, super_admin=False))
    assert client.get("/system/update").status_code == 403
    assert client.post("/system/update/check").status_code == 403


def test_the_updates_api_reports_the_running_version_and_install(session, monkeypatch):
    monkeypatch.setattr(update_check.get_settings(), "perchtail_install", "docker")
    body = _client(session, _user(session, super_admin=True)).get("/system/update").json()
    assert body["running_version"] and body["install_method"] == "docker"
    assert body["enabled"] is True and body["disabled_by_env"] is False
    assert body["latest_version"] is None and body["update_available"] is False


def test_check_now_runs_the_check_and_is_refused_when_off(session, monkeypatch):
    calls = []
    monkeypatch.setattr(update_check, "check_now", lambda s: calls.append(1))
    client = _client(session, _user(session, super_admin=True))

    assert client.post("/system/update/check").status_code == 200
    assert calls == [1]

    monkeypatch.setattr(update_check.get_settings(), "update_check", False)
    response = client.post("/system/update/check")
    assert response.status_code == 409 and calls == [1]
    assert client.get("/system/update").json()["disabled_by_env"] is True
