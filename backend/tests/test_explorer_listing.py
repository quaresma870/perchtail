"""#273: modification times in listings, per-user preferences and the
explorer's System settings."""

import base64
import io
import json
import os
import tarfile
import uuid
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.collectors.agent as agent_module
import app.collectors.local as local_module
import app.collectors.smb as smb_module
import app.collectors.winrm as winrm_module
import pytest
from app import preferences
from app.api.auth import get_current_active_user
from app.api.auth import router as auth_router
from app.api.system_settings import router as system_settings_router
from app.archives import list_members, tree_children
from app.auth.models import Role, User
from app.db import get_session
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient

WHEN = datetime(2026, 11, 6, 12, 0, tzinfo=UTC).timestamp()
SHOW_ALL = [
    Rule(source_id=1, order=0, type=RuleType.include, pattern="**", pattern_kind=PatternKind.glob)
]


# --- mtime from each connector ------------------------------------------------


def test_local_listing_has_mtimes(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("x")
    os.utime(log, (WHEN, WHEN))
    source = Source(name="l", protocol=Protocol.local, host="localhost", base_path=str(tmp_path))
    (entry,) = local_module.list_directory(source, SHOW_ALL)
    assert entry.mtime == WHEN


def test_smb_mtime_comes_from_the_listing():
    info = SimpleNamespace(last_write_time=datetime(2026, 11, 6, 12, 0, tzinfo=UTC))
    assert smb_module._mtime(info) == WHEN
    assert smb_module._mtime(SimpleNamespace()) is None


def test_winrm_listing_reads_unix_milliseconds(monkeypatch):
    listing = [{"Name": "app.log", "PSIsContainer": False, "Length": 3, "MTime": int(WHEN * 1000)}]
    encoded = base64.b64encode(json.dumps(listing).encode()).decode()
    monkeypatch.setattr(winrm_module, "_run_ps", lambda source, script: encoded)
    source = Source(name="w", protocol=Protocol.winrm, host="h", base_path="C:\\logs")
    (entry,) = winrm_module.list_directory(source, SHOW_ALL)
    assert entry.mtime == WHEN


def test_agent_listing_reads_mtime_and_tolerates_old_agents(monkeypatch):
    registry = MagicMock()
    registry.send_command_sync.return_value = {
        "entries": [
            {"name": "new.log", "is_dir": False, "size": 1, "mtime_ms": int(WHEN * 1000)},
            {"name": "old.log", "is_dir": False, "size": 1},
        ]
    }
    monkeypatch.setattr(agent_module, "get_agent_registry", lambda: registry)
    source = Source(id=3, name="a", protocol=Protocol.agent, host="h", base_path="/x")
    new, old = agent_module.list_directory(source, SHOW_ALL)
    assert (new.mtime, old.mtime) == (WHEN, None)


def test_archive_members_keep_their_times(tmp_path):
    zipped = tmp_path / "logs.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        zf.writestr(zipfile.ZipInfo("a.log", date_time=(2026, 11, 6, 12, 0, 0)), "x")
    (member,) = tree_children(list_members(zipped, "logs.zip"), "")
    assert member.mtime == WHEN

    tarred = tmp_path / "logs.tar.gz"
    with tarfile.open(tarred, "w:gz") as tf:
        info = tarfile.TarInfo("b.log")
        info.size, info.mtime = 1, WHEN
        tf.addfile(info, io.BytesIO(b"y"))
    (member,) = tree_children(list_members(tarred, "logs.tar.gz"), "")
    assert member.mtime == WHEN


# --- per-user preferences -----------------------------------------------------


def _user(session, **kwargs) -> User:
    role = Role(name=f"role-{uuid.uuid4().hex}", **kwargs)
    session.add(role)
    session.commit()
    user = User(username=f"u-{uuid.uuid4().hex[:8]}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def client_as(session):
    def make(user, router):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_current_active_user] = lambda: user
        return TestClient(app)

    return make


def test_a_user_sets_and_clears_their_preferences(session, client_as):
    user = _user(session)
    client = client_as(user, auth_router)

    body = client.patch("/auth/preferences", json={"explorer_sort": "mtime_desc"}).json()
    assert body["preferences"] == {"explorer_sort": "mtime_desc"}
    body = client.patch("/auth/preferences", json={"connections_view": "tree"}).json()
    assert body["preferences"] == {"explorer_sort": "mtime_desc", "connections_view": "tree"}

    body = client.patch("/auth/preferences", json={"explorer_sort": None}).json()
    assert body["preferences"] == {"connections_view": "tree"}
    session.refresh(user)
    assert user.preferences == {"connections_view": "tree"}


@pytest.mark.parametrize("payload", [{"explorer_sort": "random"}, {"connections_view": "grid"}])
def test_unknown_values_are_refused(session, client_as, payload):
    client = client_as(_user(session), auth_router)
    assert client.patch("/auth/preferences", json=payload).status_code == 422


def test_stored_junk_is_ignored_and_defaults_fill_the_rest(session):
    user = _user(session)
    user.preferences = {"explorer_sort": "bogus", "theme": "pink", "connections_view": "tree"}
    assert preferences.own_choices(user) == {"connections_view": "tree"}
    assert preferences.effective(session, user) == {
        "explorer_sort": "name_asc",
        "connections_view": "tree",
    }


# --- System settings ------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, status",
    [
        ({"explorer_refresh_seconds": 0}, 200),
        ({"explorer_refresh_seconds": 5}, 200),
        ({"explorer_refresh_seconds": 3}, 422),
        ({"explorer_refresh_seconds": 3601}, 422),
        ({"explorer_sort_default": "mtime_desc"}, 200),
        ({"explorer_sort_default": "random"}, 422),
    ],
)
def test_explorer_settings_are_bounded(session, client_as, payload, status):
    client = client_as(_user(session, is_super_admin=True), system_settings_router)
    response = client.patch("/system-settings", json=payload)
    assert response.status_code == status
    if status == 200:
        key, value = next(iter(payload.items()))
        assert response.json()[key] == value
