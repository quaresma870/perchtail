"""Accounts and sessions: common and context passwords refused, MFA for
local accounts on by default for new installs, the idle session timeout,
admins ending sessions, and a sign-in ending the browser's previous
session."""

import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from app import password_policy, system_settings
from app.api.auth import SESSION_COOKIE_NAME
from app.api.auth import router as auth_router
from app.api.errors import install as install_error_handlers
from app.api.system_settings import router as system_settings_router
from app.api.users import router as users_router
from app.auth.models import AuditLog, AuthSession, GlobalCapability, Role, User
from app.auth.providers.local import create_local_user
from app.auth.sessions import create_session, get_user_by_token, hash_token
from app.config import get_settings
from app.db import get_session
from app.models import SystemSetting
from app.password_policy import PasswordPolicy, PasswordPolicyError, check_password
from app.timeutils import utcnow
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

BACKEND = Path(__file__).resolve().parents[1]
GOOD = "Unrelated-Horse-9-Battery"


# -- passwords --------------------------------------------------------------------


@pytest.mark.parametrize("password", ["iloveyou", "SUNSHINE", "Password1", "princess1"])
def test_a_common_or_leaked_password_is_refused_in_any_case(password):
    with pytest.raises(PasswordPolicyError) as refused:
        check_password(password, PasswordPolicy(min_length=8))
    assert refused.value.rule == "common"
    assert str(refused.value).startswith("Too simple")


def test_the_common_list_is_shipped_and_only_has_long_entries():
    common = password_policy.common_passwords()
    assert len(common) > 30_000
    assert min(len(entry) for entry in common) >= 8
    assert "iloveyou" in common and all(entry == entry.lower() for entry in common)


def test_a_good_password_still_passes():
    check_password(GOOD, PasswordPolicy())


@pytest.mark.parametrize("password", ["MyPerchTailPass-77", "perchtail-is-great-1"])
def test_perchtail_is_always_refused(password):
    # Even with the simple and personal rules turned off.
    policy = PasswordPolicy(reject_simple=False, reject_personal=False)
    with pytest.raises(PasswordPolicyError) as refused:
        check_password(password, policy)
    assert refused.value.rule == "context_word"


def test_a_context_word_is_refused_in_any_case():
    policy = PasswordPolicy(context_words=("northwind",))
    with pytest.raises(PasswordPolicyError) as refused:
        check_password("Welcome-NorthWind-2027!", policy)
    assert refused.value.rule == "context_word"
    check_password("Welcome-Southwind-2027!", policy)


def test_the_policy_reads_the_context_words(session):
    system_settings.set_list(session, "password_context_words", ["Northwind", "Contoso"])
    session.commit()
    assert password_policy.get_policy(session).context_words == ("northwind", "contoso")


# -- the settings API ---------------------------------------------------------------


def _user(session, *, super_admin=False, capabilities=(), mfa=False) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=super_admin,
        global_capabilities=list(capabilities),
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    user = User(username=f"user-{uuid.uuid4().hex}@example.com", role_id=role.id)
    if mfa:
        # Administrators must use MFA: one with it on works without signing in.
        user.mfa_enabled = True
        user.mfa_secret_encrypted = "x"
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def api(session, monkeypatch):
    """(client, act_as): the auth, users and system-settings routers; act_as
    gives the client a real session cookie for a user."""
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(system_settings_router)
    install_error_handlers(app)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)

    def act_as(user: User) -> str:
        _, token = create_session(session, user)
        client.cookies.set(SESSION_COOKIE_NAME, token)
        return token

    yield client, act_as
    get_settings.cache_clear()


def test_context_words_are_trimmed_deduped_and_bounded(session, api):
    client, act_as = api
    act_as(_user(session, super_admin=True, mfa=True))
    answer = client.patch(
        "/system-settings",
        json={"password_context_words": [" Northwind ", "northwind", "Contoso", ""]},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["password_context_words"] == ["Northwind", "Contoso"]
    for words in (
        ["ab"],
        ["x" * 65],
        [f"word{n:03d}" for n in range(51)],
        ["ab\ncd"],
        ["abc\rdef"],
    ):
        assert (
            client.patch("/system-settings", json={"password_context_words": words}).status_code
            == 422
        )
    assert client.get("/system-settings").json()["password_context_words"] == [
        "Northwind",
        "Contoso",
    ]


@pytest.mark.parametrize("minutes,status", [(0, 200), (5, 200), (720, 200), (4, 422), (721, 422)])
def test_the_idle_timeout_is_off_or_5_to_720_minutes(session, api, minutes, status):
    client, act_as = api
    act_as(_user(session, super_admin=True, mfa=True))
    assert client.get("/system-settings").json()["session_idle_minutes"] == 60
    assert (
        client.patch("/system-settings", json={"session_idle_minutes": minutes}).status_code
        == status
    )


def test_mfa_for_local_accounts_is_on_by_default_and_warned_about_when_off(session, api):
    client, act_as = api
    row = session.get(SystemSetting, "require_mfa_local_accounts")
    if row is not None:
        session.delete(row)
        session.commit()
    act_as(_user(session, super_admin=True, mfa=True))
    assert client.get("/system-settings").json()["require_mfa_local_accounts"] is True
    codes = {w["code"] for w in client.get("/system-settings/warnings").json()}
    assert "mfa_local_accounts_off" not in codes
    turned_off = client.patch("/system-settings", json={"require_mfa_local_accounts": False})
    assert turned_off.status_code == 200
    codes = {w["code"] for w in client.get("/system-settings/warnings").json()}
    assert "mfa_local_accounts_off" in codes


def _alembic(data_dir: Path, *args: str) -> None:
    env = {**os.environ, "DATA_DIR": str(data_dir)}
    env.pop("DATABASE_URL", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
    )


def _stored_mfa(db: Path):
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT value FROM system_setting WHERE key = 'require_mfa_local_accounts'"
        ).fetchone()
    return row[0] if row else None


def test_the_migration_keeps_mfa_off_on_an_existing_install(tmp_path):
    _alembic(tmp_path, "upgrade", "a7c3e9d2f514")
    db = tmp_path / "perchtail.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO role (name, is_builtin, is_super_admin, global_capabilities)"
            " VALUES ('r', 0, 0, '[]')"
        )
        role_id = conn.execute("SELECT id FROM role").fetchone()[0]
        # Every required column of that revision's user table, with a value of
        # its type.
        values: dict = {"username": "old@example.com", "role_id": role_id}
        for _cid, name, kind, notnull, default, pk in conn.execute('PRAGMA table_info("user")'):
            if name in values or not notnull or default is not None or pk:
                continue
            kind = kind.upper()
            if "INT" in kind or "BOOL" in kind:
                values[name] = 1 if name == "active" else 0
            elif "DATE" in kind or "TIME" in kind:
                values[name] = "2026-01-01 00:00:00"
            elif "JSON" in kind:
                values[name] = "{}"
            else:
                values[name] = "LOCAL" if name == "auth_provider" else ""
        conn.execute(
            f'INSERT INTO "user" ({", ".join(values)}) VALUES ({", ".join("?" * len(values))})',
            list(values.values()),
        )
    _alembic(tmp_path, "upgrade", "head")
    assert _stored_mfa(db) == "false"


def test_the_migration_leaves_a_new_install_with_the_new_default(tmp_path):
    _alembic(tmp_path, "upgrade", "head")
    assert _stored_mfa(tmp_path / "perchtail.db") is None


# -- the idle timeout -----------------------------------------------------------------


def _session_row(session, token: str) -> AuthSession | None:
    return session.exec(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).first()


@pytest.mark.parametrize("idle,minutes,valid", [(59, 60, True), (61, 60, False), (600, 0, True)])
def test_a_session_unused_past_the_timeout_ends(session, idle, minutes, valid):
    system_settings.set_int(session, "session_idle_minutes", minutes)
    row, token = create_session(session, _user(session))
    row.last_seen_at = utcnow() - timedelta(minutes=idle)
    session.add(row)
    session.commit()
    assert (get_user_by_token(session, token) is not None) is valid
    assert (_session_row(session, token) is not None) is valid


def test_use_moves_the_last_seen_time_on(session):
    row, token = create_session(session, _user(session))
    row.last_seen_at = utcnow() - timedelta(minutes=30)
    session.add(row)
    session.commit()
    assert get_user_by_token(session, token) is not None
    assert utcnow() - _session_row(session, token).last_seen_at < timedelta(minutes=1)


# -- admins ending sessions -------------------------------------------------------------


def _audits(session, reason):
    return [
        row
        for row in session.exec(select(AuditLog).where(AuditLog.action == "session.revoke")).all()
        if (row.event_metadata or {}).get("reason") == reason
    ]


def test_an_admin_ends_every_session_of_a_user(session, api):
    client, act_as = api
    target = _user(session)
    create_session(session, target)
    create_session(session, target)
    act_as(_user(session, super_admin=True, mfa=True))
    answer = client.post(f"/users/{target.id}/sessions/revoke")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"count": 2}
    assert session.exec(select(AuthSession).where(AuthSession.user_id == target.id)).all() == []
    [entry] = _audits(session, "admin")
    assert entry.target_id == target.id and entry.event_metadata["count"] == 2


def test_a_delegated_admin_cant_end_a_peer_admins_sessions(session, api):
    client, act_as = api
    peer = _user(session, capabilities=[GlobalCapability.manage_users], mfa=True)
    create_session(session, peer)
    act_as(_user(session, capabilities=[GlobalCapability.manage_users], mfa=True))
    assert client.post(f"/users/{peer.id}/sessions/revoke").status_code == 403
    assert session.exec(select(AuthSession).where(AuthSession.user_id == peer.id)).all() != []
    assert _audits(session, "admin") == []


def test_a_super_admin_ends_every_other_session(session, api):
    client, act_as = api
    for _ in range(2):
        create_session(session, _user(session))
    own = act_as(_user(session, super_admin=True, mfa=True))
    answer = client.post("/users/sessions/revoke-all")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"count": 2}
    assert [row.token_hash for row in session.exec(select(AuthSession)).all()] == [hash_token(own)]
    [entry] = _audits(session, "admin_all")
    assert entry.event_metadata["count"] == 2


def test_only_a_super_admin_ends_every_session(session, api):
    client, act_as = api
    create_session(session, _user(session))
    act_as(_user(session, capabilities=[GlobalCapability.manage_users], mfa=True))
    assert client.post("/users/sessions/revoke-all").status_code == 403
    assert _audits(session, "admin_all") == []


# -- a sign-in ends the browser's previous session ----------------------------------------


def test_signing_in_ends_the_session_the_browser_had(session, api):
    client, act_as = api
    system_settings.set_bool(session, "require_mfa_local_accounts", False)
    session.commit()
    old = act_as(_user(session))
    role = Role(name=f"r-{uuid.uuid4().hex}")
    session.add(role)
    session.commit()
    create_local_user(
        session, actor_user_id=None, username="second@example.com", password=GOOD, role_id=role.id
    )
    answer = client.post("/auth/login", json={"username": "second@example.com", "password": GOOD})
    assert answer.status_code == 200, answer.text
    assert _session_row(session, old) is None
    assert len(session.exec(select(AuthSession)).all()) == 1
    [entry] = _audits(session, "signed_in_again")
    second = session.exec(select(User).where(User.username == "second@example.com")).one()
    assert entry.user_id == second.id and entry.target_id != second.id


def test_an_expired_previous_session_is_dropped_without_an_audit_entry(session, api):
    client, act_as = api
    system_settings.set_bool(session, "require_mfa_local_accounts", False)
    session.commit()
    old = act_as(_user(session))
    row = _session_row(session, old)
    row.expires_at = utcnow() - timedelta(minutes=1)
    session.add(row)
    session.commit()
    role = Role(name=f"r-{uuid.uuid4().hex}")
    session.add(role)
    session.commit()
    create_local_user(
        session, actor_user_id=None, username="third@example.com", password=GOOD, role_id=role.id
    )
    answer = client.post("/auth/login", json={"username": "third@example.com", "password": GOOD})
    assert answer.status_code == 200, answer.text
    assert _session_row(session, old) is None
    assert _audits(session, "signed_in_again") == []


# -- requests the interface makes on its own ------------------------------------------------


def test_a_background_request_works_but_does_not_count_as_use(session, api):
    client, act_as = api
    token = act_as(_user(session, super_admin=True, mfa=True))
    row = _session_row(session, token)
    earlier = utcnow() - timedelta(minutes=30)
    row.last_seen_at = earlier
    session.add(row)
    session.commit()
    answer = client.get("/auth/me", headers={"X-PerchTail-Background": "1"})
    assert answer.status_code == 200, answer.text
    session.refresh(row)
    assert row.last_seen_at == earlier
    assert client.get("/auth/me").status_code == 200
    session.refresh(row)
    assert utcnow() - row.last_seen_at < timedelta(minutes=1)


def test_background_requests_alone_let_the_session_end(session, api):
    client, act_as = api
    token = act_as(_user(session, super_admin=True, mfa=True))
    row = _session_row(session, token)
    row.last_seen_at = utcnow() - timedelta(minutes=61)
    session.add(row)
    session.commit()
    answer = client.get("/auth/me", headers={"X-PerchTail-Background": "1"})
    assert answer.status_code == 401
    assert _session_row(session, token) is None


# -- who may change the idle timeout --------------------------------------------------------


@pytest.mark.parametrize(
    "current,new,allowed",
    [(60, 30, True), (60, 120, False), (60, 0, False), (0, 30, True), (30, 30, True)],
)
def test_a_delegated_admin_only_makes_the_idle_timeout_stricter(
    session, api, current, new, allowed
):
    client, act_as = api
    system_settings.set_int(session, "session_idle_minutes", current)
    session.commit()
    act_as(_user(session, capabilities=[GlobalCapability.manage_system_settings], mfa=True))
    answer = client.patch("/system-settings", json={"session_idle_minutes": new})
    assert answer.status_code == (200 if allowed else 403), answer.text
    assert system_settings.get_int(session, "session_idle_minutes") == (new if allowed else current)


def test_a_super_admin_sets_any_idle_timeout(session, api):
    client, act_as = api
    act_as(_user(session, super_admin=True, mfa=True))
    for minutes in (720, 0, 60):
        answer = client.patch("/system-settings", json={"session_idle_minutes": minutes})
        assert answer.status_code == 200, answer.text
