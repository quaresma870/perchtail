"""#254: incremental follow -- GET /sources/{id}/follow returns only what was
appended, and tells the viewer when the file was rotated or truncated."""

import pytest
from app.api.archive import FOLLOW_HEAD_BYTES
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.auth.models import AuditLog, Role, User
from app.collectors.base import RANGE_MAX_BYTES, RangeNotSupportedError
from app.db import get_session
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select


@pytest.fixture()
def follow(session, tmp_path):
    """(client, source, log path): a local source over tmp_path whose rules
    show *.log and *.gz, opened by a super admin."""
    app = FastAPI()
    app.include_router(archive_router)
    app.dependency_overrides[get_session] = lambda: session
    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    source = Source(
        name="local-logs", protocol=Protocol.local, host="localhost", base_path=str(tmp_path)
    )
    session.add(source)
    session.commit()
    for order, pattern in enumerate(("*.log", "*.gz", "*.zip")):
        session.add(
            Rule(
                source_id=source.id,
                order=order,
                type=RuleType.include,
                pattern=pattern,
                pattern_kind=PatternKind.glob,
            )
        )
    session.commit()
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app), source, tmp_path / "app.log"


def _poll(client, source, offset, head=None, **extra):
    params = {"path": "app.log", "offset": offset, **extra}
    if head is not None:
        params["head"] = head
    return client.get(f"/sources/{source.id}/follow", params=params)


def test_only_the_appended_bytes_come_back(follow):
    client, source, log = follow
    log.write_bytes(b"first line\n")

    response = _poll(client, source, 11)
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["x-file-size"] == "11"
    assert response.headers["x-follow-offset"] == "11"
    head = response.headers["x-follow-head"]

    with log.open("ab") as f:
        f.write(b"second line\nthird")
    response = _poll(client, source, 11, head)
    assert response.content == b"second line\nthird"
    assert response.headers["x-follow-offset"] == "28"
    assert "x-follow-more" not in response.headers
    assert "x-follow-reset" not in response.headers


def test_a_large_append_comes_in_pieces(follow):
    client, source, log = follow
    log.write_bytes(b"x" * (RANGE_MAX_BYTES + 100))

    response = _poll(client, source, 0)
    assert len(response.content) == RANGE_MAX_BYTES
    assert response.headers["x-follow-more"] == "1"
    response = _poll(client, source, RANGE_MAX_BYTES, response.headers["x-follow-head"])
    assert len(response.content) == 100
    assert "x-follow-more" not in response.headers


def test_a_truncated_file_resets(follow):
    client, source, log = follow
    log.write_bytes(b"a long first version of the log\n")
    head = _poll(client, source, 32).headers["x-follow-head"]

    log.write_bytes(b"short\n")
    response = _poll(client, source, 32, head)
    assert response.headers["x-follow-reset"] == "1"
    assert response.content == b""


def test_a_rotated_file_that_grew_past_the_offset_resets(follow):
    client, source, log = follow
    log.write_bytes(b"2026-11-06 old file, line one\n")
    head = _poll(client, source, 30).headers["x-follow-head"]

    # Rotated, and the new file is already longer than what the viewer had.
    log.write_bytes(b"2026-11-07 new file, line one\n" * 3)
    response = _poll(client, source, 30, head)
    assert response.headers["x-follow-reset"] == "1"


def test_a_short_file_growing_past_the_head_doesnt_reset(follow):
    # The head identifies the file by the bytes the viewer has; a file
    # shorter than FOLLOW_HEAD_BYTES that grows isn't a different file.
    client, source, log = follow
    log.write_bytes(b"tiny\n")
    head = _poll(client, source, 5).headers["x-follow-head"]
    with log.open("ab") as f:
        f.write(b"y" * (FOLLOW_HEAD_BYTES * 2))
    response = _poll(client, source, 5, head)
    assert "x-follow-reset" not in response.headers
    head = response.headers["x-follow-head"]
    with log.open("ab") as f:
        f.write(b"more\n")
    response = _poll(client, source, int(response.headers["x-follow-offset"]), head)
    assert "x-follow-reset" not in response.headers
    assert response.content == b"more\n"


def test_follow_is_audited_once_when_it_starts(session, follow):
    client, source, log = follow
    log.write_bytes(b"line\n")
    _poll(client, source, 5, start=True)
    _poll(client, source, 5)
    _poll(client, source, 5)
    events = session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all()
    assert len(events) == 1
    assert events[0].event_metadata["path"] == "app.log"


@pytest.mark.parametrize("name", ["app.log.gz", "bundle.zip"])
def test_compressed_files_cant_be_followed(follow, name):
    client, source, log = follow
    (log.parent / name).write_bytes(b"\x1f\x8b not really")
    response = client.get(f"/sources/{source.id}/follow", params={"path": name, "offset": 0})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "follow_unsupported"


def test_an_old_agent_falls_back_to_reloading(follow, monkeypatch):
    client, source, log = follow
    log.write_bytes(b"line\n")

    def refuse(*args, **kwargs):
        raise RangeNotSupportedError('unknown command type "read_range"')

    monkeypatch.setattr("app.collectors.local.read_range", refuse)
    response = _poll(client, source, 0)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "follow_unsupported"


def test_rules_and_paths_still_apply(follow):
    client, source, log = follow
    (log.parent / "secret.txt").write_bytes(b"not a log")
    hidden = client.get(f"/sources/{source.id}/follow", params={"path": "secret.txt", "offset": 0})
    assert hidden.status_code == 404
    escape = client.get(f"/sources/{source.id}/follow", params={"path": "../x.log", "offset": 0})
    assert escape.status_code in (400, 404)
    negative = client.get(f"/sources/{source.id}/follow", params={"path": "app.log", "offset": -1})
    assert negative.status_code == 422


def test_each_poll_is_one_read_of_the_source(follow, monkeypatch):
    # One round trip per poll (one SSH connection, one PowerShell call, one
    # agent command): the size, the head and the new bytes together.
    from app.collectors import local

    client, source, log = follow
    log.write_bytes(b"tiny\n")
    calls = []
    real = local.read_range

    def counting(*args, **kwargs):
        calls.append(args[2:])
        return real(*args, **kwargs)

    monkeypatch.setattr(local, "read_range", counting)
    head = _poll(client, source, 0).headers["x-follow-head"]
    with log.open("ab") as f:
        f.write(b"y" * 200)
    _poll(client, source, 5, head)
    assert len(calls) == 2


def test_a_read_from_the_start_is_always_audited(session, follow):
    client, source, log = follow
    log.write_bytes(b"line\n")
    _poll(client, source, 0)
    _poll(client, source, 0)
    events = session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all()
    assert len(events) == 2


def test_an_offset_an_agent_cant_take_is_refused(follow):
    client, source, log = follow
    log.write_bytes(b"line\n")
    assert _poll(client, source, 2**62).status_code == 422


def test_open_gives_the_head_the_first_poll_checks(follow):
    client, source, log = follow
    log.write_bytes(b"2026-11-06 old file, line one\n")
    opened = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    head = opened.headers["x-follow-head"]
    assert _poll(client, source, len(opened.content), head).headers.get("x-follow-reset") is None

    # Rotated between the open and the first poll, already past the offset.
    log.write_bytes(b"2026-11-07 new file, line one\n" * 3)
    response = _poll(client, source, len(opened.content), head)
    assert response.headers["x-follow-reset"] == "1"


def test_a_refresh_of_a_source_isnt_audited_as_opening_it(session, follow):
    # #272: the explorer re-lists open sources every few seconds.
    client, source, log = follow
    log.write_bytes(b"line\n")
    client.get(f"/sources/{source.id}/browse", params={"path": ""})
    client.get(f"/sources/{source.id}/browse", params={"path": "", "refresh": "true"})
    client.get(f"/sources/{source.id}/browse", params={"path": "", "refresh": "true"})
    events = session.exec(select(AuditLog).where(AuditLog.action == "source.open")).all()
    assert len(events) == 1
