"""A folder (or a link to one) asked for as a file from a push agent: the
agent answers `is_directory` with where it really is (an agent before that
code answers with a message ending in ": is a directory", once asked for
where the path really is), and the server answers exactly as for a missing
path, audited `not_a_file`; a link to a folder the rules hide is
`link_target_hidden` (P1 of the 0.7.0 preview retest)."""

import os

import pytest
from app.agent_registry import FEATURE_LINKS, AgentCommandError, get_agent_registry
from app.collectors.base import LinkTargetHiddenError, SourceError, SourceNotAFileError
from app.collectors.errors import translate_agent
from app.collectors.registry import CONNECTORS
from app.models import Protocol, RuleType

from tests.test_link_targets import RULES, _rule
from tests.test_link_targets_agent import FsAgent, _audit, _source, api, tree  # noqa: F401


class FolderAgent(FsAgent):
    """FsAgent answering a folder as agent/internal/agent/links.go does:
    `is_directory` with the real path before the link check (`old`: no
    code, once the path is where expect_real says, as 0.7.0-rc.3 did), and
    a missing file as "no such file or directory"."""

    def __init__(self, base, old=False):
        super().__init__(base)
        self.old = old

    def __call__(self, source_id, command_type, timeout=30.0, **fields):
        path = fields.get("path", "")
        real = self._real(path)
        if command_type != "list" and real is not None:
            full = os.path.join(self.base, real)
            error_type = f"{command_type}_error"
            if not os.path.exists(full):
                self.commands.append((command_type, dict(fields)))
                raise AgentCommandError(
                    f"open {path}: no such file or directory", message_type=error_type
                )
            expect = fields.get("expect_real") or path
            if os.path.isdir(full) and (not self.old or real == expect):
                self.commands.append((command_type, dict(fields)))
                if self.old:
                    raise AgentCommandError(f"{path}: is a directory", message_type=error_type)
                raise AgentCommandError(
                    f"{path}: is a directory",
                    code="is_directory",
                    real=real,
                    message_type=error_type,
                )
        return super().__call__(source_id, command_type, timeout, **fields)


@pytest.fixture(params=[False, True], ids=["agent", "old-agent"])
def agent(request, tree, monkeypatch):  # noqa: F811
    (tree / "aponta").symlink_to("secret", target_is_directory=True)
    fake = FolderAgent(tree, old=request.param)
    registry = get_agent_registry()
    monkeypatch.setattr(registry, "send_command_sync", fake)
    monkeypatch.setattr(registry, "is_connected", lambda source_id: True)
    monkeypatch.setattr(registry, "features", lambda source_id: fake.features)
    return fake


# --- the connector -----------------------------------------------------------


@pytest.mark.parametrize("path", ["public", "mirror", "archive/2026"])
def test_reads_of_a_folder_are_not_a_file(agent, tmp_path, path):
    connector = CONNECTORS[Protocol.agent]
    with pytest.raises(SourceNotAFileError):
        connector.read_range(_source(), path, 0, 100, rules=RULES)
    with pytest.raises(SourceNotAFileError):
        connector.fetch_file(_source(), path, tmp_path / "out", rules=RULES)
    with pytest.raises(SourceNotAFileError):
        connector.call("list_archive", _source(), path, rules=RULES)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("path", ["aponta", "logs"])
def test_reads_of_a_link_to_a_hidden_folder_are_link_target_hidden(agent, tmp_path, path):
    connector = CONNECTORS[Protocol.agent]
    with pytest.raises(LinkTargetHiddenError):
        connector.read_range(_source(), path, 0, 100, rules=RULES)
    with pytest.raises(LinkTargetHiddenError):
        connector.fetch_file(_source(), path, tmp_path / "out", rules=RULES)


def test_a_link_to_a_folder_without_rules_is_not_followed(agent):
    with pytest.raises(LinkTargetHiddenError):
        CONNECTORS[Protocol.agent].read_range(_source(), "mirror", 0, 100)
    with pytest.raises(SourceNotAFileError):
        CONNECTORS[Protocol.agent].read_range(_source(), "public", 0, 100)


def test_a_link_to_a_folder_visible_only_as_a_folder_is_not_a_file(agent):
    """With `**/*.log`, the folder `public` is shown (a file below it could
    be), but the name `public` as a file isn't: the rules are checked on
    the target as a folder. An older agent answers link_target first, which
    is checked as a file, so it stays a hidden link."""
    rules = [_rule(0, RuleType.include, "**/*.log")]
    expected = LinkTargetHiddenError if agent.old else SourceNotAFileError
    with pytest.raises(expected):
        CONNECTORS[Protocol.agent].read_range(_source(), "mirror", 0, 100, rules=rules)


@pytest.mark.parametrize("real", ["", "../etc", "/etc", "a//b"])
def test_an_unusable_real_path_of_a_folder_is_a_hidden_link(monkeypatch, real):
    def answer(*args, **fields):
        raise AgentCommandError("x: is a directory", code="is_directory", real=real)

    registry = get_agent_registry()
    monkeypatch.setattr(registry, "send_command_sync", answer)
    monkeypatch.setattr(registry, "is_connected", lambda source_id: True)
    monkeypatch.setattr(registry, "features", lambda source_id: frozenset({FEATURE_LINKS}))
    with pytest.raises(LinkTargetHiddenError):
        CONNECTORS[Protocol.agent].read_range(
            _source(), "x", 0, 100, rules=[_rule(0, RuleType.include, "**")]
        )


def test_the_agent_folder_answers_are_translated_and_other_errors_are_not():
    for exc in (
        AgentCommandError("app: is a directory", code="is_directory", real="app"),
        AgentCommandError("app: is a directory"),
    ):
        assert isinstance(translate_agent(exc), SourceNotAFileError)
    # Other errors keep their answer.
    unchanged = translate_agent(AgentCommandError("app: something else"))
    assert type(unchanged) is SourceError
    assert not isinstance(
        translate_agent(AgentCommandError("x is a directory listing", code="other")),
        SourceNotAFileError,
    )
    missing = translate_agent(AgentCommandError("open x: no such file or directory"))
    assert not isinstance(missing, SourceNotAFileError) and missing.status_code == 404


# --- the endpoints -------------------------------------------------------------

_ENDPOINTS = [
    ("open", {}, "file.open_denied", {}),
    ("follow", {"offset": 0}, "file.open_denied", {"follow": True}),
    ("download", {}, "file.download_denied", {}),
]


def _comparable(response) -> tuple:
    varying = {"x-request-id", "date"}
    headers = {k: v for k, v in response.headers.items() if k.lower() not in varying}
    return response.status_code, response.content, headers


@pytest.mark.parametrize(("endpoint", "params", "action", "extra"), _ENDPOINTS)
@pytest.mark.parametrize("path", ["public", "mirror"])
def test_a_folder_read_from_an_endpoint_is_an_audited_404(
    api, session, endpoint, params, action, extra, path  # noqa: F811
):
    client, source = api
    url = f"/sources/{source.id}/{endpoint}"
    response = client.get(url, params={"path": path, **params})
    assert response.status_code == 404
    [entry] = _audit(session, action)
    assert entry.event_metadata["path"] == path
    assert entry.event_metadata["reason"] == "not_a_file"
    for name, value in extra.items():
        assert entry.event_metadata[name] == value
    assert _audit(session, "access.denied") == []
    # The same answer as a path that isn't there.
    missing = client.get(url, params={"path": "public/missing.log", **params})
    assert _comparable(response) == _comparable(missing)


@pytest.mark.parametrize(("endpoint", "params", "action", "extra"), _ENDPOINTS)
@pytest.mark.parametrize("path", ["aponta", "logs"])
def test_a_link_to_a_hidden_folder_read_from_an_endpoint_is_link_target_hidden(
    api, session, endpoint, params, action, extra, path  # noqa: F811
):
    client, source = api
    response = client.get(f"/sources/{source.id}/{endpoint}", params={"path": path, **params})
    assert response.status_code == 404
    [entry] = _audit(session, action)
    assert entry.event_metadata["path"] == path
    assert entry.event_metadata["reason"] == "link_target_hidden"
    response = client.get(f"/sources/{source.id}/browse", params={"path": path})
    assert response.status_code == 404


def test_files_and_links_to_files_still_open(api):  # noqa: F811
    client, source = api
    for path, content in (("current.log", "today\n"), ("mirror/p.log", "public\n")):
        response = client.get(f"/sources/{source.id}/open", params={"path": path})
        assert response.status_code == 200
        assert response.text == content
