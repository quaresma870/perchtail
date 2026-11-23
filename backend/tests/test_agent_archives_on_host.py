"""Archives on an agent that lists and extracts them on its host
(redaction/1), when redaction doesn't apply to the source: browsing a
.zip or .tar.gz lists it on the agent (list_archive), and opening or
downloading a member fetches only that member (fetch with `member`, no
`redact` block), never the whole archive. The rules are still checked on
the server, member by member, and the refusals are those of the redacted
path. The whole archive, the folder ZIP and a plain .gz file are still
fetched whole, and an agent without redaction/1 keeps the copy."""

import gzip
import io
import tarfile
import zipfile

import pytest
from app import system_settings
from app.agent_registry import (
    FEATURE_LINKS,
    FEATURE_REDACTION,
    AgentCommandError,
    get_agent_registry,
)
from app.archives import normalize_member_name
from app.collectors import agent as agent_collector
from app.collectors.base import SourceError
from app.models import Protocol, Source

from tests import test_redaction_agent
from tests.test_redaction_agent import (
    FakeAgent,
    _archive_entries,
    _audit,
    _scratch_files,
    _tar_gz_bytes,
    _zip_bytes,
)
from tests.test_redaction_downloads import _mark_encrypted

# The fixtures of tests/test_redaction_agent.py: an agent source (everything
# but `secret*` visible) and the scratch store.
agent_source = test_redaction_agent.agent_source
scratch_store = test_redaction_agent.scratch_store

APP_LOG = b"2026-11-23 10:00:00 INFO started\n2026-11-23 10:00:01 ERROR failed\n"


def _unreadable(message_type: str) -> AgentCommandError:
    return AgentCommandError(
        "zip: not a valid zip file", code="archive_unreadable", message_type=message_type
    )


class PlainAgent:
    """An agent with `files` on its host that lists and extracts archives
    itself (redaction/1), asked for content without a `redact` block: it
    sends what is stored, as the Go agent does (redaction.go,
    withContent). Every command is recorded with the feature it needed."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.commands: list[tuple[str, dict]] = []
        # Answered instead of the content, once the command is recorded.
        self.refuse: AgentCommandError | None = None
        # Sent past what the member holds, as a misbehaving agent.
        self.extra = b""

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
        self.commands.append(
            (command_type, {**fields, "require_feature": require_feature, "limit": limit})
        )
        assert "redact" not in fields
        if command_type == "list":
            return FakeAgent._list(self, fields["path"])
        if self.refuse is not None:
            raise self.refuse
        if command_type == "list_archive":
            data = self.files[fields["path"]]
            try:
                entries = _archive_entries(fields["path"], data)
            except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
                raise _unreadable("list_archive_error") from exc
            return {
                "members": [
                    {"name": n, "is_dir": d, "size": s, "mtime_ms": m} for n, d, s, m, _ in entries
                ]
            }
        assert command_type == "fetch", command_type
        content = self.files[fields["path"]]
        member = fields.get("member")
        if member is not None:
            content = self._member(fields["path"], content, member)
            if len(content) > fields["max_decompressed"]:
                raise AgentCommandError(
                    f"over the {fields['max_decompressed']}-byte limit",
                    code="too_large",
                    size=len(content),
                    limit="decompressed",
                    message_type="fetch_error",
                )
        sink.write(content + self.extra)
        if limit is not None and len(content) + len(self.extra) > limit:
            # What the registry does with a stream past its `limit`.
            raise AgentCommandError(
                "file is over the fetch limit", code="too_large", size=len(content)
            )
        return {"type": "fetch_result", "streamed": True, "chunks": 1, "size": len(content)}

    @staticmethod
    def _member(path: str, content: bytes, member: str) -> bytes:
        name = path.rsplit("/", 1)[-1]
        try:
            entries = _archive_entries(name, content)
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
            raise _unreadable("fetch_error") from exc
        if name.lower().endswith(".zip"):
            infos = zipfile.ZipFile(io.BytesIO(content)).infolist()
            if any(
                normalize_member_name(i.filename) == member and i.flag_bits & 0x1 for i in infos
            ):
                raise AgentCommandError(
                    "member is encrypted", code="member_encrypted", message_type="fetch_error"
                )
        last = {normalize_member_name(n): (d, read) for n, d, _, _, read in entries}
        if member not in last or last[member][0]:
            raise AgentCommandError(
                "member not found", code="not_found", message_type="fetch_error"
            )
        return last[member][1]()


@pytest.fixture()
def plain_agent(session, agent_source, monkeypatch):
    """(client, source, agent): the agent source of test_redaction_agent
    (everything but `secret*` visible) with redaction off, and an agent
    that lists and extracts archives on its host."""
    client, source, _ = agent_source
    system_settings.set_bool(session, "redaction_enabled", False)
    session.commit()
    agent = PlainAgent({})
    monkeypatch.setattr(get_agent_registry(), "send_command_sync", agent)
    return client, source, agent


def _kinds(agent: PlainAgent) -> list[str]:
    return [kind for kind, _ in agent.commands]


def _members(name: str):
    nested = _zip_bytes({"inner.log": APP_LOG})
    members = {
        "app.log": APP_LOG,
        "sub/old.log": b"old\n",
        "secret.log": b"hidden\n",
        "sub/secret.key": b"hidden too\n",
        "other.zip": nested,
    }
    return (_zip_bytes if name.endswith(".zip") else _tar_gz_bytes)(members)


ARCHIVES = ["logs.zip", "logs.tar.gz", "logs.tgz"]


# -- browsing -----------------------------------------------------------------------


@pytest.mark.parametrize("name", ARCHIVES)
@pytest.mark.parametrize(
    "params", [{}, {"all": "true"}, {"sub": True}], ids=["root", "all", "folder"]
)
def test_browsing_an_archive_lists_it_on_the_agent_as_from_the_copy(
    plain_agent, monkeypatch, name, params
):
    """Without redaction too, the archive is listed on the host: the same
    names, sizes and hidden members as when the server lists its copy."""
    client, source, agent = plain_agent
    agent.files[name] = _members(name)
    path = f"{name}/sub" if params.pop("sub", False) else name

    on_host = client.get(f"/sources/{source.id}/browse", params={"path": path, **params})
    assert on_host.status_code == 200, on_host.text
    assert _kinds(agent) == ["list_archive"]
    _, fields = agent.commands[0]
    assert fields["path"] == name and fields["expect_real"] == name
    assert fields["require_feature"] == (FEATURE_REDACTION, FEATURE_LINKS)

    # The same archive listed from a copy, by an agent without redaction/1.
    monkeypatch.setattr(get_agent_registry(), "redacts_on_host", lambda source_id: False)
    copied = client.get(f"/sources/{source.id}/browse", params={"path": path, **params})
    assert copied.status_code == 200, copied.text
    assert _kinds(agent)[1:] == ["fetch"]
    assert "member" not in agent.commands[1][1]

    assert on_host.json() == copied.json()
    names = {entry["path"] for entry in on_host.json()}
    assert not any("secret" in entry for entry in names)


def test_an_archive_the_agent_cant_list_is_refused_as_on_the_server(plain_agent):
    client, source, agent = plain_agent
    agent.files["broken.zip"] = b"just some text\n" * 10
    response = client.get(f"/sources/{source.id}/browse", params={"path": "broken.zip"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "archive_unreadable"
    assert _kinds(agent) == ["list_archive"]


# -- opening and downloading a member ------------------------------------------------


@pytest.mark.parametrize("name", ARCHIVES)
@pytest.mark.parametrize(
    ("endpoint", "action"), [("open", "file.open"), ("download", "file.download")]
)
def test_a_member_is_extracted_on_the_agent_and_only_it_travels(
    session, plain_agent, scratch_store, name, endpoint, action
):
    client, source, agent = plain_agent
    agent.files[name] = _members(name)

    response = client.get(
        f"/sources/{source.id}/{endpoint}", params={"path": name, "member": "sub/old.log"}
    )

    assert response.status_code == 200, response.text
    assert response.content == b"old\n"
    # One fetch, of the member only, without a redact block: nothing of the
    # whole archive was asked for.
    assert agent.commands == [
        (
            "fetch",
            {
                "path": name,
                "member": "sub/old.log",
                "chunk_size": agent_collector.FETCH_CHUNK_BYTES,
                "max_bytes": agent_collector.collector_base.max_fetch_bytes(),
                "max_decompressed": agent_collector._max_decompressed_bytes(),
                "expect_real": name,
                "require_feature": (FEATURE_REDACTION, FEATURE_LINKS),
                "limit": agent_collector._max_decompressed_bytes(),
            },
        )
    ]
    event = _audit(session, action)
    assert event.event_metadata["path"] == name
    assert event.event_metadata["member"] == "sub/old.log"
    assert "redaction" not in event.event_metadata
    if endpoint == "open":
        closed = client.post(
            f"/sources/{source.id}/close", json={"path": name, "member": "sub/old.log"}
        )
        assert closed.status_code == 204, closed.text
    assert _scratch_files(scratch_store) == []


def test_reopening_a_member_fetches_it_again_on_the_agent(plain_agent):
    """Following a member reloads it whole (follow_unsupported): each load
    is the member again, never the archive."""
    client, source, agent = plain_agent
    agent.files["logs.zip"] = _members("logs.zip")
    params = {"path": "logs.zip", "member": "app.log"}
    first = client.get(f"/sources/{source.id}/open", params=params)
    follow = client.get(f"/sources/{source.id}/follow", params={**params, "offset": 0})
    second = client.get(f"/sources/{source.id}/open", params=params)
    assert first.content == second.content == APP_LOG
    assert follow.status_code == 409
    assert follow.json()["detail"]["error_code"] == "follow_unsupported"
    assert [(k, f.get("member")) for k, f in agent.commands] == [
        ("fetch", "app.log"),
        ("fetch", "app.log"),
    ]


@pytest.mark.parametrize("endpoint", ["open", "download"])
def test_a_member_the_rules_hide_is_never_asked_for(session, plain_agent, endpoint):
    client, source, agent = plain_agent
    agent.files["logs.zip"] = _members("logs.zip")
    response = client.get(
        f"/sources/{source.id}/{endpoint}", params={"path": "logs.zip", "member": "secret.log"}
    )
    assert response.status_code == 404
    assert agent.commands == []


@pytest.mark.parametrize(
    ("endpoint", "action"), [("open", "file.open_denied"), ("download", "file.download_denied")]
)
def test_a_member_of_an_archive_the_agent_cant_read_is_refused_and_audited(
    session, plain_agent, scratch_store, endpoint, action
):
    client, source, agent = plain_agent
    agent.files["broken.zip"] = b"just some text\n" * 10
    response = client.get(
        f"/sources/{source.id}/{endpoint}", params={"path": "broken.zip", "member": "app.log"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "archive_unreadable"
    event = _audit(session, action)
    assert event.event_metadata["path"] == "broken.zip/app.log"
    assert event.event_metadata["reason"] == "archive_unreadable"
    assert _kinds(agent) == ["fetch"]
    assert _scratch_files(scratch_store) == []


@pytest.mark.parametrize(
    ("endpoint", "action"), [("open", "file.open_denied"), ("download", "file.download_denied")]
)
def test_an_encrypted_member_on_the_agent_is_refused_and_audited(
    session, plain_agent, scratch_store, endpoint, action
):
    client, source, agent = plain_agent
    agent.files["logs.zip"] = _mark_encrypted(
        _zip_bytes({"big.log": b"locked", "app.log": APP_LOG}), "big.log"
    )
    response = client.get(
        f"/sources/{source.id}/{endpoint}", params={"path": "logs.zip", "member": "big.log"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "archive_member_unreadable"
    event = _audit(session, action)
    assert event.event_metadata["path"] == "logs.zip/big.log"
    assert event.event_metadata["reason"] == "member_encrypted"
    assert _scratch_files(scratch_store) == []


@pytest.mark.parametrize("member", ["missing.log", "sub"])
def test_a_member_not_there_or_not_a_file_is_not_found(plain_agent, scratch_store, member):
    """A member that isn't in the archive, or a folder in it: the agent
    answers not found, a 404 as on the redacted path."""
    client, source, agent = plain_agent
    agent.files["logs.tar.gz"] = _members("logs.tar.gz")
    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.tar.gz", "member": member}
    )
    assert response.status_code == 404, response.text
    assert _kinds(agent) == ["fetch"]
    assert _scratch_files(scratch_store) == []


def test_an_archive_that_is_a_folder_is_not_a_file(session, plain_agent, scratch_store):
    client, source, agent = plain_agent
    agent.refuse = AgentCommandError(
        "logs.zip: is a directory", code="is_directory", real="logs.zip", message_type="fetch_error"
    )
    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "not_found_on_source"
    assert _audit(session, "file.open_denied").event_metadata["reason"] == "not_a_file"
    assert _scratch_files(scratch_store) == []


def test_an_archive_behind_a_link_to_a_hidden_target_is_refused(
    session, plain_agent, scratch_store
):
    """The agent says the archive really is a file the rules hide: never
    asked again, 404 audited as link_target_hidden."""
    client, source, agent = plain_agent
    agent.refuse = AgentCommandError(
        "logs.zip: really is secret.zip",
        code="link_target",
        real="secret.zip",
        message_type="fetch_error",
    )
    response = client.get(
        f"/sources/{source.id}/download", params={"path": "logs.zip", "member": "app.log"}
    )
    assert response.status_code == 404
    event = _audit(session, "file.download_denied")
    assert event.event_metadata["reason"] == "link_target_hidden"
    assert _kinds(agent) == ["fetch"]
    assert _scratch_files(scratch_store) == []


def test_an_archive_behind_a_link_to_a_visible_target_is_read_there(plain_agent, monkeypatch):
    client, source, agent = plain_agent
    agent.files["real.zip"] = _members("logs.zip")
    moved = AgentCommandError(
        "logs.zip: really is real.zip", code="link_target", real="real.zip", message_type="x"
    )

    def answer(*args, **fields):
        if fields["expect_real"] == "logs.zip":
            agent.commands.append(("fetch", fields))
            raise moved
        return PlainAgent.__call__(agent, *args, **{**fields, "path": "real.zip"})

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", answer)
    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )
    assert response.status_code == 200, response.text
    assert response.content == APP_LOG
    assert [f["expect_real"] for _, f in agent.commands] == ["logs.zip", "real.zip"]


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        ("fetch", "fetch_too_large"),
        ("decompressed", None),
    ],
)
def test_a_member_over_a_limit_is_a_413_naming_it(plain_agent, scratch_store, limit, expected):
    client, source, agent = plain_agent
    agent.refuse = AgentCommandError(
        "too large", code="too_large", size=10**9, limit=limit, message_type="fetch_error"
    )
    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )
    assert response.status_code == 413, response.text
    detail = response.json()["detail"]
    if expected is None:
        assert detail == "File is too large once decompressed (MAX_DECOMPRESSED_MB)"
    else:
        assert detail["error_code"] == expected
    assert _scratch_files(scratch_store) == []


@pytest.fixture()
def distinct_limits(monkeypatch):
    """MAX_DECOMPRESSED_MB below MAX_FETCH_MB, so a 413 says which."""
    from app.config import get_settings

    monkeypatch.setenv("MAX_DECOMPRESSED_MB", "0.01")
    monkeypatch.setenv("MAX_FETCH_MB", "1")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("MAX_DECOMPRESSED_MB")
    monkeypatch.delenv("MAX_FETCH_MB")
    get_settings.cache_clear()


def test_an_agent_sending_more_than_the_decompressed_limit_is_a_413(
    plain_agent, scratch_store, distinct_limits
):
    """The registry stops a member past MAX_DECOMPRESSED_MB whatever the
    agent says: the decompressed 413, not the fetch one, and nothing of it
    is kept."""
    client, source, agent = plain_agent
    agent.files["logs.zip"] = _members("logs.zip")
    agent.extra = b"x" * (agent_collector._max_decompressed_bytes() + 1)
    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )
    assert response.status_code == 413, response.text
    assert response.json()["detail"] == "File is too large once decompressed (MAX_DECOMPRESSED_MB)"
    assert _scratch_files(scratch_store) == []


def test_an_archive_member_that_is_an_archive_is_checked_as_a_whole_archive(
    session, plain_agent, scratch_store
):
    """A nested archive is served whole, so the member fetched from the
    agent is checked as one: refused when the rules hide one of its
    members, as from the copy."""
    client, source, agent = plain_agent
    agent.files["logs.zip"] = _zip_bytes(
        {"inner.zip": _zip_bytes({"app.log": APP_LOG, "secret.log": b"hidden\n"})}
    )
    response = client.get(
        f"/sources/{source.id}/download", params={"path": "logs.zip", "member": "inner.zip"}
    )
    assert response.status_code == 403, response.text
    event = _audit(session, "file.download_denied")
    assert event.event_metadata["path"] == "logs.zip/inner.zip"
    assert [(k, f.get("member")) for k, f in agent.commands] == [("fetch", "inner.zip")]
    assert _scratch_files(scratch_store) == []


# -- what doesn't change ------------------------------------------------------------------


def test_a_whole_archive_is_still_fetched_whole_and_checked(session, plain_agent, scratch_store):
    client, source, agent = plain_agent
    agent.files["logs.zip"] = _members("logs.zip")
    agent.files["clean.zip"] = _zip_bytes({"app.log": APP_LOG})

    refused = client.get(f"/sources/{source.id}/download", params={"path": "logs.zip"})
    served = client.get(f"/sources/{source.id}/download", params={"path": "clean.zip"})

    assert refused.status_code == 403
    assert served.status_code == 200 and served.content == agent.files["clean.zip"]
    assert _kinds(agent) == ["fetch", "fetch"]
    assert all("member" not in fields for _, fields in agent.commands)
    assert all(fields["require_feature"] == FEATURE_LINKS for _, fields in agent.commands)


def test_a_folder_zip_still_fetches_each_archive_whole(plain_agent):
    client, source, agent = plain_agent
    agent.files["dir/clean.zip"] = _zip_bytes({"app.log": APP_LOG})
    agent.files["dir/app.log"] = APP_LOG
    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "dir"})
    assert response.status_code == 200, response.text
    fetched = [f for kind, f in agent.commands if kind == "fetch"]
    assert sorted(f["path"] for f in fetched) == ["dir/app.log", "dir/clean.zip"]
    assert all("member" not in f and "decompress" not in f for f in fetched)
    assert "list_archive" not in _kinds(agent)


def test_a_gz_file_is_still_fetched_compressed(plain_agent):
    client, source, agent = plain_agent
    agent.files["app.log.gz"] = gzip.compress(APP_LOG)
    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"})
    assert response.status_code == 200, response.text
    assert response.content == APP_LOG
    [(kind, fields)] = agent.commands
    assert kind == "fetch" and "decompress" not in fields and "member" not in fields


@pytest.mark.parametrize("endpoint", ["browse", "open"])
def test_an_agent_without_the_feature_keeps_the_copy(plain_agent, monkeypatch, endpoint):
    client, source, agent = plain_agent
    monkeypatch.setattr(get_agent_registry(), "redacts_on_host", lambda source_id: False)
    agent.files["logs.zip"] = _members("logs.zip")
    params = {"path": "logs.zip"}
    if endpoint == "open":
        params["member"] = "app.log"
    response = client.get(f"/sources/{source.id}/{endpoint}", params=params)
    assert response.status_code == 200, response.text
    if endpoint == "open":
        assert response.content == APP_LOG
    [(kind, fields)] = agent.commands
    assert kind == "fetch" and "member" not in fields
    assert fields["require_feature"] == FEATURE_LINKS


# -- the collector -------------------------------------------------------------------


def test_fetch_member_leaves_nothing_behind_when_it_fails(monkeypatch, tmp_path):
    source = Source(id=1, name="a", protocol=Protocol.agent, host="a", base_path="/")
    destination = tmp_path / "member"

    def failing(source_id, command_type, timeout=30.0, *, sink=None, **fields):
        sink.write(b"part of it")
        raise AgentCommandError("read failed", message_type="fetch_error")

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", failing)
    with pytest.raises(AgentCommandError):
        agent_collector.fetch_member(source, "logs.zip", "app.log", destination)
    assert not destination.exists()

    def not_streamed(source_id, command_type, timeout=30.0, *, sink=None, **fields):
        sink.write(b"something")
        return {"type": "fetch_result", "content_b64": ""}

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", not_streamed)
    with pytest.raises(SourceError):
        agent_collector.fetch_member(source, "logs.zip", "app.log", destination)
    assert not destination.exists()
