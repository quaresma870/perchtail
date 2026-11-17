"""Links are judged by where they lead: a symlink (the file itself, or a
folder above it) is listed and served only when its target is inside the
base path and the rules show both its own path and the target's. Refusals
answer 404 and are audited as `link_target_hidden`."""

import uuid

import pytest
from app import name_index
from app.auth.models import AuditLog, Capability, Role, RoleGrant, ScopeType, User
from app.auth.sessions import create_session
from app.collectors import local as local_module
from app.collectors.base import LinkTargetHiddenError
from app.collectors.registry import CONNECTORS
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Customer, PatternKind, Protocol, Rule, RuleType, Source
from app.scratch import get_scratch_store
from fastapi.testclient import TestClient
from sqlmodel import select


def _rule(order, type_, pattern, kind=PatternKind.glob, source_id=1) -> Rule:
    return Rule(source_id=source_id, order=order, type=type_, pattern=pattern, pattern_kind=kind)


RULES = [
    _rule(0, RuleType.include, "**/*"),
    _rule(1, RuleType.exclude, "secret/**"),
    _rule(2, RuleType.exclude, "archive/**"),
]


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    """base/
    app-2026.log, current.log -> app-2026.log (visible target)
    sneaky.log -> secret/key.log (hidden target)
    outside.log -> ../outside/x.log (out of base)
    secret/key.log, archive/2026/old.log
    logs -> archive/2026 (hidden folder)
    mirror -> public (visible folder)
    public/p.log, public/inner.log -> ../secret/key.log
    """
    base = tmp_path / "allowed" / "base"
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.log").write_text("outside")
    (base / "app-2026.log").write_text("today\n")
    (base / "secret").mkdir()
    (base / "secret" / "key.log").write_text("TOP SECRET\n")
    (base / "archive" / "2026").mkdir(parents=True)
    (base / "archive" / "2026" / "old.log").write_text("old\n")
    (base / "public").mkdir()
    (base / "public" / "p.log").write_text("public\n")
    (base / "current.log").symlink_to("app-2026.log")
    (base / "sneaky.log").symlink_to("secret/key.log")
    (base / "outside.log").symlink_to(outside / "x.log")
    (base / "logs").symlink_to("archive/2026", target_is_directory=True)
    (base / "mirror").symlink_to("public", target_is_directory=True)
    (base / "public" / "inner.log").symlink_to("../secret/key.log")
    monkeypatch.setattr(get_settings(), "local_source_roots", str(tmp_path / "allowed"))
    return base


def _source(base) -> Source:
    return Source(id=1, name="s", protocol=Protocol.local, host="h", base_path=str(base))


# --- the connector -----------------------------------------------------------


def test_listing_shows_only_links_whose_target_is_visible(tree):
    entries = {e.name: e for e in local_module.list_directory(_source(tree), RULES)}
    assert "current.log" in entries
    assert entries["current.log"].target == "app-2026.log"
    assert entries["app-2026.log"].target is None
    assert "mirror" in entries and entries["mirror"].target == "public"
    # A link to a hidden file or folder, or out of the base, isn't listed.
    assert "sneaky.log" not in entries
    assert "logs" not in entries
    assert "outside.log" not in entries


def test_listing_through_a_linked_folder_checks_the_real_paths(tree):
    entries = {e.name: e for e in local_module.list_directory(_source(tree), RULES, "mirror")}
    assert entries["p.log"].path == "mirror/p.log"
    assert entries["p.log"].target == "public/p.log"
    # Its own link to a hidden file stays hidden.
    assert "inner.log" not in entries


def test_listing_a_linked_folder_whose_target_is_hidden_is_refused(tree):
    with pytest.raises(LinkTargetHiddenError):
        local_module.list_directory(_source(tree), RULES, "logs")


def test_a_read_through_a_link_needs_its_target_allowed(tree):
    source = _source(tree)
    connector = CONNECTORS[Protocol.local]
    assert connector.read_range(source, "current.log", 0, 100, rules=RULES).data == b"today\n"
    for path in ("sneaky.log", "public/inner.log", "logs/old.log", "outside.log"):
        with pytest.raises(LinkTargetHiddenError):
            connector.read_range(source, path, 0, 100, rules=RULES)
    # Without the rules, no link is followed at all (fail closed).
    with pytest.raises(LinkTargetHiddenError):
        connector.read_range(source, "current.log", 0, 100)
    # A file that isn't reached through a link needs nothing more.
    assert connector.read_range(source, "app-2026.log", 0, 100).data == b"today\n"


def test_fetch_and_local_copy_check_the_target(tree, tmp_path):
    source = _source(tree)
    connector = CONNECTORS[Protocol.local]
    with pytest.raises(LinkTargetHiddenError):
        connector.fetch_file(source, "sneaky.log", tmp_path / "out", rules=RULES)
    with pytest.raises(LinkTargetHiddenError):
        with connector.local_copy(source, "sneaky.log", rules=RULES):
            pass
    with connector.local_copy(source, "current.log", rules=RULES) as path:
        assert path.read_text() == "today\n"


def test_the_link_name_must_be_visible_too(tree):
    # A link whose own name the rules hide isn't listed, even when it leads
    # to a visible file.
    (tree / "alias.log").symlink_to("app-2026.log")
    rules = [*RULES, _rule(3, RuleType.exclude, "alias.log")]
    names = {e.name for e in local_module.list_directory(_source(tree), rules)}
    assert "alias.log" not in names
    assert "current.log" in names


def test_the_name_index_leaves_out_links_to_hidden_files(tree):
    files, result = name_index._walk(CONNECTORS[Protocol.local], _source(tree), RULES)
    assert result.complete
    assert "current.log" in files
    assert "mirror/p.log" in files
    for hidden in ("sneaky.log", "outside.log", "mirror/inner.log", "public/inner.log"):
        assert hidden not in files
    assert not any(path.startswith(("logs/", "secret/", "archive/")) for path in files)


def test_a_source_with_several_folders_checks_targets_on_virtual_paths(tree, tmp_path):
    other = tmp_path / "allowed" / "other"
    other.mkdir()
    (other / "b.log").write_text("b\n")
    source = Source(
        id=1,
        name="s",
        protocol=Protocol.local,
        host="h",
        base_path=str(tree),
        roots=[{"label": "one", "path": str(tree)}, {"label": "two", "path": str(other)}],
    )
    rules = [
        _rule(0, RuleType.include, "**/*"),
        _rule(1, RuleType.exclude, "one/secret/**"),
        _rule(2, RuleType.exclude, "one/archive/**"),
    ]
    connector = CONNECTORS[Protocol.local]
    names = {e.name for e in connector.list_directory(source, rules, "one")}
    assert "current.log" in names
    assert "sneaky.log" not in names and "logs" not in names
    with pytest.raises(LinkTargetHiddenError):
        connector.list_directory(source, rules, "one/logs")
    with pytest.raises(LinkTargetHiddenError):
        connector.read_range(source, "one/sneaky.log", 0, 100, rules=rules)
    assert connector.read_range(source, "one/current.log", 0, 100, rules=rules).data == b"today\n"


# --- the endpoints: refused as 404 and audited -------------------------------


@pytest.fixture()
def api(session, tree, tmp_path, monkeypatch):
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    get_settings.cache_clear()
    get_scratch_store.cache_clear()
    monkeypatch.setattr(get_settings(), "local_source_roots", str(tree.parent))
    customer = Customer(name="c")
    session.add(customer)
    session.flush()
    source = Source(
        name="s", customer_id=customer.id, protocol=Protocol.local, host="h", base_path=str(tree)
    )
    session.add(source)
    session.flush()
    for rule in RULES:
        session.add(_rule(rule.order, rule.type, rule.pattern, source_id=source.id))
    role = Role(name=f"r-{uuid.uuid4().hex}")
    session.add(role)
    session.flush()
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.customer,
            scope_id=customer.id,
            capabilities=[Capability.view, Capability.download],
        )
    )
    user = User(username=f"u-{uuid.uuid4().hex}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    app.dependency_overrides[get_session] = lambda: session
    _, token = create_session(session, user, user_agent="pytest")
    client = TestClient(app)
    client.cookies.set("perchtail_session", token)
    yield client, source
    app.dependency_overrides.pop(get_session, None)
    get_settings.cache_clear()
    get_scratch_store.cache_clear()


def _audit(session, action) -> list[AuditLog]:
    session.expire_all()
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


@pytest.mark.parametrize("path", ["sneaky.log", "outside.log", "public/inner.log"])
def test_open_of_a_link_to_a_hidden_file_is_an_audited_404(api, session, path):
    client, source = api
    response = client.get(f"/sources/{source.id}/open", params={"path": path})
    assert response.status_code == 404
    assert "SECRET" not in response.text
    [entry] = _audit(session, "file.open_denied")
    assert entry.event_metadata["path"] == path
    assert entry.event_metadata["reason"] == "link_target_hidden"
    assert _audit(session, "file.open") == []
    # Recorded once, not again as access.denied.
    assert _audit(session, "access.denied") == []


def test_open_of_a_link_to_a_visible_file_still_works(api, session):
    client, source = api
    response = client.get(f"/sources/{source.id}/open", params={"path": "current.log"})
    assert response.status_code == 200
    assert response.text == "today\n"
    response = client.get(f"/sources/{source.id}/open", params={"path": "mirror/p.log"})
    assert response.status_code == 200
    assert response.text == "public\n"


def test_download_of_a_link_to_a_hidden_file_is_an_audited_404(api, session):
    client, source = api
    response = client.get(f"/sources/{source.id}/download", params={"path": "sneaky.log"})
    assert response.status_code == 404
    [entry] = _audit(session, "file.download_denied")
    assert entry.event_metadata["reason"] == "link_target_hidden"
    assert entry.event_metadata["path"] == "sneaky.log"


def test_follow_of_a_link_to_a_hidden_file_is_an_audited_404(api, session):
    client, source = api
    response = client.get(
        f"/sources/{source.id}/follow", params={"path": "sneaky.log", "offset": 1}
    )
    assert response.status_code == 404
    assert response.content != b"TOP SECRET\n"[1:]
    [entry] = _audit(session, "file.open_denied")
    assert entry.event_metadata["reason"] == "link_target_hidden"
    assert entry.event_metadata["follow"] is True


def test_browse_of_a_linked_folder_whose_target_is_hidden_is_an_audited_404(api, session):
    client, source = api
    response = client.get(f"/sources/{source.id}/browse", params={"path": "logs"})
    assert response.status_code == 404
    [entry] = _audit(session, "access.denied")
    assert entry.event_metadata["reason"] == "link_target_hidden"
    assert entry.event_metadata["path"] == "logs"


def test_browse_lists_only_links_whose_target_is_visible(api):
    client, source = api
    names = {e["name"] for e in client.get(f"/sources/{source.id}/browse").json()}
    assert {"current.log", "mirror", "app-2026.log", "public"} <= names
    assert not names & {"sneaky.log", "outside.log", "logs", "secret", "archive"}


def test_folder_zip_leaves_out_links_to_hidden_files(api):
    import io
    import zipfile

    client, source = api
    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "mirror"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["p.log"]
