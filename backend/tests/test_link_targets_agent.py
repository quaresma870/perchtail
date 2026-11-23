"""Links on push-agent sources (links/1): the agent says where each path
really is, and reads only once the server asked for that target, so the
rules are checked on where a link leads, as for local and SSH sources. An
agent that doesn't say so is never asked for content: it must be updated
(audited, with a message that only asks for the update); listing goes on.

The fake agent below runs on a real folder with real symlinks and answers
as agent/internal/agent does (links.go)."""

import base64
import io
import os
import uuid
import zipfile

import pytest
from app.agent_registry import FEATURE_LINKS, AgentCommandError, get_agent_registry
from app.auth.models import AuditLog, Capability, Role, RoleGrant, ScopeType, User
from app.auth.sessions import create_session
from app.collectors import agent as agent_module
from app.collectors.base import LinkTargetHiddenError
from app.collectors.registry import CONNECTORS
from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Alert, Customer, Protocol, RuleType, Source
from app.scratch import get_scratch_store
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.test_link_targets import RULES, _rule


@pytest.fixture()
def tree(tmp_path):
    """The same tree as tests/test_link_targets.py, on the agent's host."""
    base = tmp_path / "agent" / "base"
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
    return base


class FsAgent:
    """An agent on `base` that announced links/1 (or not, `features`)."""

    def __init__(self, base, features=(FEATURE_LINKS,)):
        self.base = os.path.realpath(base)
        self.features = frozenset(features)
        self.commands: list[tuple[str, dict]] = []
        # Paths whose link moves after the first command (a race).
        self.moves: dict[str, str] = {}

    def _real(self, path: str) -> str | None:
        real = os.path.realpath(os.path.join(self.base, path))
        if real == self.base:
            return ""
        if not real.startswith(self.base + os.sep):
            return None
        return os.path.relpath(real, self.base).replace(os.sep, "/")

    def __call__(
        self,
        source_id,
        command_type,
        timeout=30.0,
        *,
        sink=None,
        limit=None,
        archive=False,
        require_feature=None,
        command_id=None,
        **fields,
    ):
        self.commands.append((command_type, dict(fields)))
        path = fields["path"]
        if command_type == "list":
            return self._list(path)
        assert require_feature is not None and FEATURE_LINKS in (
            (require_feature,) if isinstance(require_feature, str) else require_feature
        ), "content asked for without links/1"
        real = self._real(path)
        error_type = f"{command_type}_error"
        if real is None:
            raise AgentCommandError(
                f"{path}: leads out of the base path",
                code="link_target",
                outside=True,
                message_type=error_type,
            )
        expect = fields.get("expect_real") or path
        if real != expect:
            raise AgentCommandError(
                f"{path}: really is {real}", code="link_target", real=real, message_type=error_type
            )
        if path in self.moves:
            # Swapped between the two commands: it now leads elsewhere.
            moved = self.moves.pop(path)
            raise AgentCommandError(
                f"{path}: really is {moved}",
                code="link_target",
                real=moved,
                message_type=error_type,
            )
        content = (self.base_path(real)).read_bytes()
        if command_type == "fetch":
            sink.write(content)
            return {"type": "fetch_result", "streamed": True, "chunks": 1, "size": len(content)}
        if command_type == "read_range":
            offset, length = fields["offset"], fields["length"]
            return {
                "size": len(content),
                "head_b64": base64.b64encode(content[: fields.get("head_length", 0)]).decode(),
                "data_b64": base64.b64encode(content[offset : offset + length]).decode(),
            }
        raise AssertionError(command_type)

    def base_path(self, real: str):
        from pathlib import Path

        return Path(self.base) / real

    def _list(self, path):
        directory = os.path.join(self.base, path)
        real_dir = self._real(path)
        if real_dir is None:
            raise AgentCommandError(
                f"{path}: leads out of the base path",
                code="link_target",
                outside=True,
                message_type="list_error",
            )
        entries = []
        for name in sorted(os.listdir(directory)):
            child = os.path.join(directory, name)
            child_path = f"{path}/{name}" if path else name
            real = self._real(child_path)
            if real is None or not os.path.exists(child):
                continue
            entry = {
                "name": name,
                "is_dir": os.path.isdir(child),
                "size": 0 if os.path.isdir(child) else os.path.getsize(child),
            }
            if FEATURE_LINKS in self.features and real != child_path:
                entry["target"] = real
            entries.append(entry)
        result = {"entries": entries}
        if FEATURE_LINKS in self.features and real_dir != path:
            result["real_dir"] = real_dir
        return result


@pytest.fixture()
def agent(tree, monkeypatch):
    fake = FsAgent(tree)
    registry = get_agent_registry()
    monkeypatch.setattr(registry, "send_command_sync", fake)
    monkeypatch.setattr(registry, "is_connected", lambda source_id: True)
    monkeypatch.setattr(registry, "features", lambda source_id: fake.features)
    return fake


def _source() -> Source:
    return Source(id=1, name="a", protocol=Protocol.agent, host="a", base_path="/var/log")


# --- the connector -----------------------------------------------------------


def test_listing_shows_only_links_whose_target_is_visible(agent):
    entries = {e.name: e for e in CONNECTORS[Protocol.agent].list_directory(_source(), RULES)}
    assert entries["current.log"].target == "app-2026.log"
    assert entries["app-2026.log"].target is None
    assert entries["mirror"].target == "public"
    assert not {"sneaky.log", "logs", "outside.log"} & set(entries)


def test_listing_through_a_linked_folder_checks_the_real_paths(agent):
    entries = {
        e.name: e for e in CONNECTORS[Protocol.agent].list_directory(_source(), RULES, "mirror")
    }
    assert entries["p.log"].path == "mirror/p.log"
    assert entries["p.log"].target == "public/p.log"
    assert "inner.log" not in entries


def test_listing_a_linked_folder_whose_target_is_hidden_is_refused(agent):
    with pytest.raises(LinkTargetHiddenError):
        CONNECTORS[Protocol.agent].list_directory(_source(), RULES, "logs")


def test_a_read_through_a_link_asks_again_for_the_target_the_rules_show(agent):
    connector = CONNECTORS[Protocol.agent]
    assert connector.read_range(_source(), "current.log", 0, 100, rules=RULES).data == b"today\n"
    assert [fields["expect_real"] for _, fields in agent.commands] == [
        "current.log",
        "app-2026.log",
    ]
    # A file that isn't reached through a link: one command.
    agent.commands.clear()
    assert connector.read_range(_source(), "app-2026.log", 0, 100, rules=RULES).data == b"today\n"
    assert len(agent.commands) == 1


@pytest.mark.parametrize(
    "path", ["sneaky.log", "public/inner.log", "logs/old.log", "outside.log", "mirror/inner.log"]
)
def test_a_read_through_a_link_to_a_hidden_target_is_never_asked_again(agent, path, tmp_path):
    connector = CONNECTORS[Protocol.agent]
    with pytest.raises(LinkTargetHiddenError):
        connector.read_range(_source(), path, 0, 100, rules=RULES)
    with pytest.raises(LinkTargetHiddenError):
        connector.fetch_file(_source(), path, tmp_path / "out", rules=RULES)
    assert not (tmp_path / "out").exists()
    # Only the first command of each, which read nothing.
    assert [fields["expect_real"] for _, fields in agent.commands] == [path, path]


def test_without_the_rules_no_link_is_followed(agent):
    with pytest.raises(LinkTargetHiddenError):
        CONNECTORS[Protocol.agent].read_range(_source(), "current.log", 0, 100)
    assert len(agent.commands) == 1


def test_a_link_that_moves_between_the_two_commands_is_refused(agent):
    agent.moves["current.log"] = "secret/key.log"
    with pytest.raises(LinkTargetHiddenError):
        CONNECTORS[Protocol.agent].read_range(_source(), "current.log", 0, 100, rules=RULES)
    assert len(agent.commands) == 2


def test_an_unusable_real_path_from_the_agent_is_refused(monkeypatch):
    for real in ("../etc/passwd", "/etc/passwd", "", "a//b", None):

        def answer(*args, real=real, **fields):
            raise AgentCommandError("x", code="link_target", real=real)

        registry = get_agent_registry()
        monkeypatch.setattr(registry, "send_command_sync", answer)
        monkeypatch.setattr(registry, "is_connected", lambda source_id: True)
        monkeypatch.setattr(registry, "features", lambda source_id: frozenset({FEATURE_LINKS}))
        with pytest.raises(LinkTargetHiddenError):
            CONNECTORS[Protocol.agent].read_range(
                _source(), "current.log", 0, 100, rules=[_rule(0, RuleType.include, "**")]
            )


def test_the_name_index_leaves_out_links_to_hidden_files(agent):
    from app import name_index

    files, result = name_index._walk(CONNECTORS[Protocol.agent], _source(), RULES)
    assert result.complete
    assert "current.log" in files and "mirror/p.log" in files
    for hidden in ("sneaky.log", "outside.log", "mirror/inner.log", "public/inner.log"):
        assert hidden not in files


# --- an agent without links/1 --------------------------------------------------


@pytest.fixture()
def old_agent(agent):
    agent.features = frozenset({"redaction/1"})
    return agent


def test_an_agent_without_links_is_never_asked_for_content(old_agent, tmp_path):
    connector = CONNECTORS[Protocol.agent]
    source = _source()
    for read in (
        lambda: connector.fetch_file(source, "app-2026.log", tmp_path / "out", rules=RULES),
        lambda: connector.read_range(source, "app-2026.log", 0, 10, rules=RULES),
        lambda: connector.local_copy(source, "app-2026.log", rules=RULES).__enter__(),
        lambda: connector.call("list_archive", source, "logs.zip", rules=RULES),
        lambda: connector.call(
            "fetch_member", source, "logs.zip", "a.log", tmp_path / "m", rules=RULES
        ),
        lambda: connector.call(
            "read_range_redacted", source, "app-2026.log", 0, 10, {"digest": "d"}, rules=RULES
        ),
        lambda: connector.call(
            "fetch_redacted", source, "app-2026.log", tmp_path / "r", {"digest": "d"}, rules=RULES
        ),
        lambda: connector.call(
            "fetch_archive", source, "a.zip", ["x"], tmp_path, {"digest": "d"}, rules=RULES
        ),
    ):
        with pytest.raises(agent_module.AgentUpdateRequiredError):
            read()
    assert old_agent.commands == []
    # Listing goes on, as before.
    names = {e.name for e in connector.list_directory(source, RULES)}
    assert "app-2026.log" in names


def test_an_agent_that_reconnected_without_links_is_refused_by_the_registry(monkeypatch, tmp_path):
    """The registry checks the live connection again before it sends."""

    def refuse(*args, require_feature=None, **fields):
        raise AgentCommandError(
            "the agent doesn't support links/1", code="links_missing", message_type="feature_error"
        )

    registry = get_agent_registry()
    monkeypatch.setattr(registry, "send_command_sync", refuse)
    monkeypatch.setattr(registry, "is_connected", lambda source_id: True)
    monkeypatch.setattr(registry, "features", lambda source_id: frozenset({FEATURE_LINKS}))
    with pytest.raises(agent_module.AgentUpdateRequiredError):
        agent_module.fetch_file(_source(), "app.log", tmp_path / "out")


def test_an_agent_that_isnt_connected_fails_as_before(monkeypatch, tmp_path):
    from app.collectors.base import SourceUnreachableError

    with pytest.raises(SourceUnreachableError):
        CONNECTORS[Protocol.agent].fetch_file(_source(), "app.log", tmp_path / "out")


def test_the_update_message_only_asks_for_the_update():
    message = agent_module.AgentUpdateRequiredError.public_message.lower()
    assert "update" in message
    assert "link" not in message and "secur" not in message


def test_update_required_is_shown_once_the_agent_connected_without_links():
    from app.timeutils import utcnow

    never = Source(name="n", protocol=Protocol.agent, host="n", base_path="/")
    assert not agent_module.agent_update_required(never)
    old = Source(
        name="o",
        protocol=Protocol.agent,
        host="o",
        base_path="/",
        agent_last_seen_at=utcnow(),
        agent_features="redaction/1",
    )
    assert agent_module.agent_update_required(old)
    old.agent_features = None
    assert agent_module.agent_update_required(old)
    old.agent_features = "links/1,redaction/1"
    assert not agent_module.agent_update_required(old)


def test_the_agent_link_error_is_a_hidden_link():
    from app.collectors.errors import translate_agent

    mapped = translate_agent(AgentCommandError("x: leads out", code="link_target", outside=True))
    assert isinstance(mapped, LinkTargetHiddenError)


# --- the endpoints -------------------------------------------------------------


@pytest.fixture()
def api(session, agent, tmp_path, monkeypatch):
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    get_settings.cache_clear()
    get_scratch_store.cache_clear()
    customer = Customer(name="c")
    session.add(customer)
    session.flush()
    source = Source(
        name="a", customer_id=customer.id, protocol=Protocol.agent, host="a", base_path="/var/log"
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


def _fields(entry: AuditLog, *names) -> dict:
    return {name: entry.event_metadata.get(name) for name in names}


@pytest.mark.parametrize("path", ["sneaky.log", "outside.log", "public/inner.log"])
def test_open_of_a_link_to_a_hidden_file_is_an_audited_404(api, session, path):
    client, source = api
    response = client.get(f"/sources/{source.id}/open", params={"path": path})
    assert response.status_code == 404
    assert "SECRET" not in response.text
    [entry] = _audit(session, "file.open_denied")
    assert _fields(entry, "path", "reason") == {"path": path, "reason": "link_target_hidden"}
    assert _audit(session, "access.denied") == []


def test_open_of_a_link_to_a_visible_file_still_works(api):
    client, source = api
    for path, content in (("current.log", "today\n"), ("mirror/p.log", "public\n")):
        response = client.get(f"/sources/{source.id}/open", params={"path": path})
        assert response.status_code == 200
        assert response.text == content


def test_follow_and_download_of_a_link_to_a_hidden_file_are_audited_404s(api, session):
    client, source = api
    response = client.get(
        f"/sources/{source.id}/follow", params={"path": "sneaky.log", "offset": 1}
    )
    assert response.status_code == 404
    [entry] = _audit(session, "file.open_denied")
    assert entry.event_metadata["reason"] == "link_target_hidden"
    assert entry.event_metadata["follow"] is True
    response = client.get(f"/sources/{source.id}/download", params={"path": "sneaky.log"})
    assert response.status_code == 404
    [entry] = _audit(session, "file.download_denied")
    assert _fields(entry, "path", "reason") == {
        "path": "sneaky.log",
        "reason": "link_target_hidden",
    }


def test_browse_of_a_linked_folder_whose_target_is_hidden_is_an_audited_404(api, session):
    client, source = api
    response = client.get(f"/sources/{source.id}/browse", params={"path": "logs"})
    assert response.status_code == 404
    [entry] = _audit(session, "access.denied")
    assert entry.event_metadata["reason"] == "link_target_hidden"
    assert entry.event_metadata["path"] == "logs"


def test_folder_zip_leaves_out_links_to_hidden_files(api):
    client, source = api
    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "mirror"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["p.log"]


@pytest.fixture()
def old_api(api, agent):
    agent.features = frozenset({"redaction/1"})
    return api


@pytest.mark.parametrize(
    ("endpoint", "params", "action", "extra"),
    [
        ("open", {"path": "app-2026.log"}, "file.open_denied", {}),
        ("follow", {"path": "app-2026.log", "offset": 0}, "file.open_denied", {"follow": True}),
        ("download", {"path": "app-2026.log"}, "file.download_denied", {}),
    ],
)
def test_content_from_an_agent_to_update_is_refused_and_audited(
    old_api, agent, session, endpoint, params, action, extra
):
    client, source = old_api
    response = client.get(f"/sources/{source.id}/{endpoint}", params=params)
    assert response.status_code == 409
    assert response.json()["error_code"] == "agent_update_required"
    assert "Settings → Agents" in response.json()["detail"]
    [entry] = _audit(session, action)
    assert _fields(entry, "path", "reason", *extra) == {
        "path": "app-2026.log",
        "reason": "agent_update_required",
        **extra,
    }
    assert _audit(session, "access.denied") == []
    assert [kind for kind, _ in agent.commands] == []


@pytest.mark.parametrize("features", [{"redaction/1"}, set()])
def test_a_redacted_download_from_an_agent_to_update_is_refused_and_audited(
    old_api, agent, session, features
):
    """With redaction_downloads on, on the agent (redaction/1) or on the
    server (no redaction/1): file.download_denied, as without redaction."""
    from app import system_settings
    from app.models import RedactionRule
    from app.redaction_rules import seed_builtin_redaction_rules

    seed_builtin_redaction_rules(session)
    for rule in session.exec(select(RedactionRule)).all():
        rule.enabled = True
        session.add(rule)
    system_settings.set_bool(session, "redaction_enabled", True)
    system_settings.set_bool(session, "redaction_downloads", True)
    session.commit()
    agent.features = frozenset(features)
    client, source = old_api
    response = client.get(f"/sources/{source.id}/download", params={"path": "app-2026.log"})
    assert response.status_code == 409
    assert response.json()["error_code"] == "agent_update_required"
    [entry] = _audit(session, "file.download_denied")
    assert _fields(entry, "path", "reason") == {
        "path": "app-2026.log",
        "reason": "agent_update_required",
    }
    assert _audit(session, "access.denied") == []
    assert agent.commands == []


def test_a_folder_zip_from_an_agent_to_update_is_refused_before_it_starts(old_api, agent, session):
    client, source = old_api
    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "public"})
    assert response.status_code == 409
    [entry] = _audit(session, "access.denied")
    assert entry.event_metadata["reason"] == "agent_update_required"
    assert entry.event_metadata["path"] == "public"
    assert agent.commands == []


def test_an_archive_on_an_agent_to_update_isnt_browsed(old_api, agent, session, tree):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("a.log", "a\n")
    (tree / "logs.zip").write_bytes(buffer.getvalue())
    client, source = old_api
    response = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip"})
    assert response.status_code == 409
    [entry] = _audit(session, "access.denied")
    assert entry.event_metadata["reason"] == "agent_update_required"
    # Listing a folder goes on.
    response = client.get(f"/sources/{source.id}/browse")
    assert response.status_code == 200
    assert "app-2026.log" in {e["name"] for e in response.json()}


def test_alerts_wait_for_an_agent_to_update_and_say_so_once(agent, monkeypatch):
    from app import alerts

    logged = []
    monkeypatch.setattr(alerts.logger, "warning", lambda event, **fields: logged.append(event))
    alert = Alert(id=99, user_id=1, name="x", query="q")
    source = _source()
    agent.features = frozenset({"redaction/1"})
    assert alerts._reads_from(alert, source) is False
    assert alerts._reads_from(alert, source) is False
    assert logged == ["alert.agent_update_required"]
    agent.features = frozenset({FEATURE_LINKS})
    assert alerts._reads_from(alert, source) is True
    agent.features = frozenset()
    assert alerts._reads_from(alert, source) is False
    assert logged == ["alert.agent_update_required"] * 2
    assert agent.commands == []


def _enable_redaction(session) -> None:
    from app import system_settings
    from app.models import RedactionRule
    from app.redaction_rules import seed_builtin_redaction_rules

    seed_builtin_redaction_rules(session)
    for rule in session.exec(select(RedactionRule)).all():
        rule.enabled = True
        session.add(rule)
    system_settings.set_bool(session, "redaction_enabled", True)
    system_settings.set_bool(session, "redaction_downloads", True)
    session.commit()


def _record_fallbacks(monkeypatch) -> tuple[list[str], list[int]]:
    """(events the archive module logged, sources whose first-fallback flag
    was consumed)."""
    import app.api.archive as archive_module

    logged: list[str] = []
    consumed: list[int] = []
    real_logger = archive_module.logger

    class Recorder:
        def __getattr__(self, level):
            def log(event, **fields):
                logged.append(event)
                getattr(real_logger, level)(event, **fields)

            return log

    def first_redaction_fallback(source_id):
        consumed.append(source_id)
        return True

    monkeypatch.setattr(archive_module, "logger", Recorder())
    monkeypatch.setattr(get_agent_registry(), "first_redaction_fallback", first_redaction_fallback)
    return logged, consumed


@pytest.mark.parametrize(
    ("endpoint", "params"),
    [
        ("open", {"path": "app-2026.log"}),
        ("follow", {"path": "app-2026.log", "offset": 0}),
        ("download", {"path": "app-2026.log"}),
    ],
)
def test_a_refused_agent_to_update_logs_no_redaction_fallback(
    old_api, agent, session, monkeypatch, endpoint, params
):
    """An agent without links/1 that can't redact on its host either: the
    request is refused (agent_update_required) before anything decides the
    server redacts in its place, so agent.redaction_fallback isn't logged
    and the once-per-connection flag is kept for a request that reads."""
    _enable_redaction(session)
    agent.features = frozenset()
    logged, consumed = _record_fallbacks(monkeypatch)
    client, source = old_api
    response = client.get(f"/sources/{source.id}/{endpoint}", params=params)
    assert response.status_code == 409
    assert response.json()["error_code"] == "agent_update_required"
    assert "agent.redaction_fallback" not in logged
    assert consumed == []
    assert agent.commands == []


def test_alerts_on_an_agent_to_update_log_no_redaction_fallback(agent, session, monkeypatch):
    from app import alerts

    _enable_redaction(session)
    agent.features = frozenset()
    logged, consumed = _record_fallbacks(monkeypatch)
    source = _source()
    redaction = alerts.source_redaction(session, source)
    assert redaction is not None and redaction.block is None
    assert "agent.redaction_fallback" not in logged
    assert consumed == []


def test_the_agents_page_says_which_agents_need_updating(session):
    from app.agent_upgrade_policy import UpgradeSettings
    from app.api.agent_upgrades import _row
    from app.timeutils import utcnow

    customer = Customer(name="c2")
    session.add(customer)
    session.flush()
    source = Source(
        name="old",
        customer_id=customer.id,
        protocol=Protocol.agent,
        host="o",
        base_path="/",
        agent_last_seen_at=utcnow(),
        agent_features="redaction/1",
    )
    session.add(source)
    session.commit()
    settings = UpgradeSettings(policy="manual", window=None, failure_limit=3, concurrency=1)
    assert _row(source, settings, None, {}).update_required is True
    source.agent_features = "links/1,redaction/1"
    assert _row(source, settings, None, {}).update_required is False


def test_rules_keep_working_for_case_on_a_windows_agent(agent):
    """An exclude hides a link's target whatever its case (the rules are
    checked on the target)."""
    rules = [_rule(0, RuleType.include, "**/*"), _rule(1, RuleType.exclude, "SECRET/**")]
    entries = {e.name for e in CONNECTORS[Protocol.agent].list_directory(_source(), rules)}
    assert "sneaky.log" not in entries
    assert "current.log" in entries
