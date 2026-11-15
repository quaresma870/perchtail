"""Where each audit entry came from, and the events added with it: the
client address behind trusted proxies, refused requests (access.denied),
sign-outs, revoked sessions, SSO failures, searches, folder listings and
audit log reads (app/request_context.py, app/api/errors.py)."""

import uuid

import pytest
from app import request_context
from app.auth.models import AuditLog, Capability, Role, RoleGrant, ScopeType, User
from app.auth.sessions import create_session, hash_token
from app.config import get_settings, parse_networks
from app.db import get_session
from app.main import app
from app.models import Customer, PatternKind, Protocol, Rule, RuleType, Source
from app.request_context import resolve_client, session_ref
from fastapi.testclient import TestClient
from sqlmodel import select
from structlog.testing import capture_logs

TRUSTED = parse_networks("127.0.0.0/8, ::1, 10.0.0.0/24")


# --- resolving the client address ---------------------------------------------


def test_a_request_from_an_untrusted_peer_keeps_its_address_and_ignores_the_header():
    assert resolve_client("203.0.113.9", ["198.51.100.1"], TRUSTED) == "203.0.113.9"


def test_behind_a_trusted_proxy_the_client_is_the_last_untrusted_hop():
    # The client wrote "1.2.3.4" itself; the proxy appended who it really saw.
    assert resolve_client("10.0.0.5", ["1.2.3.4, 198.51.100.7"], TRUSTED) == "198.51.100.7"


def test_trusted_hops_are_skipped_from_the_right():
    hops = ["198.51.100.7, 10.0.0.9", "10.0.0.8"]
    assert resolve_client("127.0.0.1", hops, TRUSTED) == "198.51.100.7"


def test_a_malformed_hop_stops_at_the_trusted_proxy_that_passed_it_on():
    assert resolve_client("10.0.0.5", ["198.51.100.7, for=evil"], TRUSTED) == "10.0.0.5"


def test_no_header_and_ipv4_mapped_peers():
    assert resolve_client("127.0.0.1", [], TRUSTED) == "127.0.0.1"
    assert resolve_client("::ffff:10.0.0.5", ["198.51.100.7"], TRUSTED) == "198.51.100.7"
    assert resolve_client("testclient", ["198.51.100.7"], TRUSTED) == "testclient"
    assert resolve_client(None, [], TRUSTED) is None


def test_only_loopback_is_trusted_by_default():
    networks = parse_networks(get_settings().trusted_proxies)
    assert resolve_client("127.0.0.1", ["198.51.100.7"], networks) == "198.51.100.7"
    assert resolve_client("10.0.0.5", ["198.51.100.7"], networks) == "10.0.0.5"


def test_a_malformed_trusted_proxy_stops_startup():
    from app.config import Settings

    with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
        Settings(trusted_proxies="10.0.0.0/8, proxy.local")


# --- through the app ----------------------------------------------------------


def _user(session, *, super_admin=False, capabilities=None, grants=None) -> User:
    role = Role(
        name=f"role-{uuid.uuid4().hex}",
        is_super_admin=super_admin,
        global_capabilities=capabilities or [],
    )
    session.add(role)
    session.flush()
    for scope_type, scope_id, caps in grants or []:
        session.add(
            RoleGrant(role_id=role.id, scope_type=scope_type, scope_id=scope_id, capabilities=caps)
        )
    user = User(username=f"user-{uuid.uuid4().hex}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture()
def real_app(session):
    app.dependency_overrides[get_session] = lambda: session
    yield
    app.dependency_overrides.pop(get_session, None)


def _client(session, user, address=("127.0.0.1", 50000)) -> tuple[TestClient, str]:
    _, token = create_session(session, user, user_agent="pytest-agent")
    client = TestClient(app, client=address)
    client.cookies.set("perchtail_session", token)
    return client, token


def _entries(session, action) -> list[AuditLog]:
    session.expire_all()
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def test_an_entry_records_the_client_behind_a_trusted_proxy(session, real_app, monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxies", "10.0.0.5")
    user = _user(session)
    client, token = _client(session, user, address=("10.0.0.5", 443))

    response = client.post(
        "/auth/logout",
        headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.7", "User-Agent": "Browser/1.0"},
    )
    assert response.status_code == 204
    [entry] = _entries(session, "user.logout")
    assert entry.user_id == user.id
    assert entry.client_ip == "198.51.100.7"
    assert entry.user_agent == "Browser/1.0"
    assert entry.session_id == session_ref(hash_token(token))
    assert entry.hash_format == 2


def test_a_forwarded_header_from_an_untrusted_peer_is_ignored(session, real_app):
    user = _user(session)
    client, _ = _client(session, user, address=("203.0.113.9", 443))
    client.post("/auth/logout", headers={"X-Forwarded-For": "198.51.100.7"})
    [entry] = _entries(session, "user.logout")
    assert entry.client_ip == "203.0.113.9"


def test_a_forwarded_header_from_an_untrusted_peer_warns_once(session, real_app):
    user = _user(session)
    trusted_client, _ = _client(session, user, address=("127.0.0.1", 50000))
    with capture_logs() as logs:
        # From a trusted proxy, or with no forwarding header: nothing to say.
        trusted_client.get("/healthz", headers={"X-Forwarded-For": "198.51.100.7"})
        trusted_client.get("/healthz")
        assert not request_context.trusted_proxies_suspect()

        client, _ = _client(session, user, address=("203.0.113.9", 443))
        client.get("/healthz", headers={"Forwarded": "for=198.51.100.7"})
        client.get("/healthz", headers={"X-Forwarded-For": "198.51.100.8"})

    assert request_context.trusted_proxies_suspect()
    warnings = [e for e in logs if e["event"] == "client_address.untrusted_forwarded_header"]
    assert warnings == [
        {
            "event": "client_address.untrusted_forwarded_header",
            "log_level": "warning",
            "peer": "203.0.113.9",
        }
    ]


def test_the_user_agent_is_truncated(session, real_app):
    user = _user(session)
    client, _ = _client(session, user)
    client.post("/auth/logout", headers={"User-Agent": "x" * 1000})
    [entry] = _entries(session, "user.logout")
    assert len(entry.user_agent) == 256


def test_a_stale_cookie_signs_no_one_out_and_writes_nothing(session, real_app):
    client = TestClient(app)
    client.cookies.set("perchtail_session", "not-a-session")
    assert client.post("/auth/logout").status_code == 204
    assert _entries(session, "user.logout") == []


def test_revoking_a_session_is_audited(session, real_app):
    user = _user(session)
    client, _ = _client(session, user)
    other, other_token = create_session(session, user)

    assert client.delete(f"/auth/sessions/{other.id}").status_code == 204
    [entry] = _entries(session, "session.revoke")
    assert entry.user_id == user.id
    assert entry.event_metadata["revoked_session_id"] == session_ref(hash_token(other_token))
    assert entry.event_metadata["count"] == 1


# --- access.denied --------------------------------------------------------------


@pytest.fixture()
def source(session, tmp_path):
    customer = Customer(name=f"c-{uuid.uuid4().hex}")
    session.add(customer)
    session.flush()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "app.log").write_text("hello\n")
    row = Source(
        name="app01",
        customer_id=customer.id,
        protocol=Protocol.local,
        host="localhost",
        base_path=str(tmp_path),
    )
    session.add(row)
    session.flush()
    session.add(
        Rule(
            source_id=row.id,
            order=0,
            type=RuleType.include,
            pattern="**",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    session.refresh(row)
    return row


def _denials(session) -> list[AuditLog]:
    return _entries(session, "access.denied")


@pytest.mark.parametrize(
    ("method", "path", "body", "target_type"),
    [
        ("GET", "/sources/{sid}/open?path=sub/app.log", None, "source"),
        ("GET", "/sources/{sid}/download?path=sub/app.log", None, "source"),
        ("PATCH", "/sources/{sid}", {"name": "renamed"}, "source"),
        ("DELETE", "/sources/{sid}", None, "source"),
        ("POST", "/sources/{sid}/rules", {"type": "include", "pattern": "*.log"}, "source"),
        ("POST", "/roles", {"name": "sneaky"}, None),
        ("POST", "/users", {"username": "someone", "role_id": 1}, None),
    ],
)
def test_a_refused_request_is_audited_as_access_denied(
    session, real_app, source, method, path, body, target_type
):
    user = _user(session)
    client, token = _client(session, user, address=("198.51.100.7", 1))

    response = client.request(method, path.format(sid=source.id), json=body)
    assert response.status_code == 403
    # The answer stays neutral: no capability names, nothing about the target.
    assert "missing" not in response.text

    [entry] = _denials(session)
    assert entry.user_id == user.id
    assert entry.target_type == target_type
    if target_type == "source":
        assert entry.target_id == source.id
    assert entry.event_metadata["attempted"].startswith(method)
    assert entry.event_metadata["reason"]
    assert entry.client_ip == "198.51.100.7"
    assert entry.session_id == session_ref(hash_token(token))


def test_the_reason_names_the_missing_capability(session, real_app, source):
    user = _user(session, grants=[(ScopeType.customer, source.customer_id, [Capability.view])])
    client, _ = _client(session, user)

    assert client.get(f"/sources/{source.id}/download?path=sub/app.log").status_code == 403
    [entry] = _denials(session)
    assert entry.event_metadata["reason"] == "missing_capability:download"
    assert entry.event_metadata["attempted"] == "GET /sources/{source_id}/download"


def test_a_refusal_on_a_role_names_the_role(session, real_app):
    target = _user(session).role
    user = _user(session)
    client, _ = _client(session, user)

    assert client.patch(f"/roles/{target.id}", json={"name": "x"}).status_code == 403
    [entry] = _denials(session)
    assert (entry.target_type, entry.target_id) == ("role", target.id)
    assert entry.event_metadata["reason"] == "missing_global_capability:manage_roles"


def test_a_refusal_on_a_user_names_the_user(session, real_app):
    target = _user(session)
    user = _user(session)
    client, _ = _client(session, user)

    assert client.delete(f"/users/{target.id}").status_code == 403
    [entry] = _denials(session)
    assert (entry.target_type, entry.target_id) == ("user", target.id)


def test_a_refused_whole_archive_is_audited_once(session, real_app, source, tmp_path):
    import tarfile

    with tarfile.open(tmp_path / "sub" / "bundle.tar.gz", "w:gz") as archive:
        archive.add(tmp_path / "sub" / "app.log", arcname="secret.key")
    session.add(
        Rule(
            source_id=source.id,
            order=1,
            type=RuleType.exclude,
            pattern="**/secret.key",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    admin = _user(session, super_admin=True)
    client, _ = _client(session, admin)

    response = client.get(f"/sources/{source.id}/download?path=sub/bundle.tar.gz")
    assert response.status_code == 403
    assert len(_entries(session, "file.download_denied")) == 1
    assert _denials(session) == []


def test_a_forced_password_change_is_not_a_refused_permission(session, real_app):
    user = _user(session)
    user.must_change_password = True
    session.add(user)
    session.commit()
    client, _ = _client(session, user)

    assert client.get("/sources").status_code == 403
    assert _denials(session) == []


def test_an_unauthenticated_request_is_not_an_access_denied(session, real_app, source):
    assert TestClient(app).get(f"/sources/{source.id}/open?path=sub/app.log").status_code == 401
    assert _denials(session) == []


# --- searches, folder listings, audit reads ----------------------------------------


def test_a_search_is_audited_with_its_query(session, real_app):
    user = _user(session)
    client, _ = _client(session, user)

    assert client.get("/files", params={"q": "error.log"}).status_code == 200
    [entry] = _entries(session, "files.search")
    assert entry.user_id == user.id
    assert entry.event_metadata["query"] == "error.log"
    assert entry.event_metadata["regex"] is False


def test_listing_a_folder_below_the_root_is_audited(session, real_app, source):
    admin = _user(session, super_admin=True)
    client, _ = _client(session, admin)

    assert client.get(f"/sources/{source.id}/browse").status_code == 200
    assert _entries(session, "source.browse") == []
    assert len(_entries(session, "source.open")) == 1

    assert client.get(f"/sources/{source.id}/browse", params={"path": "sub"}).status_code == 200
    [entry] = _entries(session, "source.browse")
    assert (entry.target_type, entry.target_id) == ("source", source.id)
    assert entry.event_metadata["path"] == "sub"

    # The explorer's periodic refresh isn't audited again.
    client.get(f"/sources/{source.id}/browse", params={"path": "sub", "refresh": True})
    assert len(_entries(session, "source.browse")) == 1


def test_folder_listings_are_not_audited_when_the_setting_is_off(session, real_app, source):
    from app import system_settings

    system_settings.set_bool(session, "audit_folder_listings", False)
    session.commit()
    admin = _user(session, super_admin=True)
    client, _ = _client(session, admin)

    assert client.get(f"/sources/{source.id}/browse", params={"path": "sub"}).status_code == 200
    assert _entries(session, "source.browse") == []


def test_reading_the_audit_log_is_audited_with_its_filters(session, real_app):
    admin = _user(session, super_admin=True)
    client, _ = _client(session, admin)

    response = client.get("/audit", params={"action": "user.login", "offset": 0})
    assert response.status_code == 200
    # Written after the page was read: not on the page it records.
    assert all(item["action"] != "audit.view" for item in response.json()["items"])
    [entry] = _entries(session, "audit.view")
    assert entry.event_metadata["action"] == ["user.login"]
    assert "offset" not in entry.event_metadata


def test_audit_times_and_origin_are_in_the_answer(session, real_app):
    admin = _user(session, super_admin=True)
    client, token = _client(session, admin, address=("198.51.100.7", 1))
    client.get("/audit")

    item = client.get("/audit").json()["items"][0]
    assert item["action"] == "audit.view"
    assert item["timestamp"].endswith("Z")
    assert item["client_ip"] == "198.51.100.7"
    assert item["user_agent"] == "testclient"
    assert item["session_id"] == session_ref(hash_token(token))
    integrity = client.post("/audit/integrity/verify").json()
    assert integrity["last_checked_at"].endswith("Z")


# --- SSO failures -------------------------------------------------------------------


def test_a_failed_sso_callback_is_audited_and_folded(session, real_app):
    client = TestClient(app, client=("198.51.100.7", 1))
    for _ in range(3):
        response = client.get(
            "/auth/sso/callback", params={"code": "c", "state": "s"}, follow_redirects=False
        )
        assert response.status_code == 302
    [entry] = _entries(session, "user.sso_login_failed")
    assert entry.user_id is None
    assert entry.event_metadata["reason"] == "state_cookie_mismatch"
    assert entry.client_ip == "198.51.100.7"
    assert "username" not in entry.event_metadata


def test_an_identity_provider_error_is_audited(session, real_app):
    client = TestClient(app)
    client.get("/auth/sso/callback", params={"error": "access_denied"}, follow_redirects=False)
    [entry] = _entries(session, "user.sso_login_failed")
    assert entry.event_metadata["reason"] == "idp_error"
    assert entry.event_metadata["error"] == "access_denied"
