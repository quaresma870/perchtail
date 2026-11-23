"""Redaction in the viewer (#382, #249): /open and /follow send redacted
content, for every user, when redaction applies to the source; offsets and
the Tail's head stay about the original; nothing of a redacted value comes
out, whole or in part."""

import bz2
import gzip
import hashlib
import io
import lzma
import stat as stat_module
import tarfile
import zipfile
from pathlib import Path

import app.collectors.ssh as ssh_module
import pytest
from app import system_settings
from app.api.archive import FOLLOW_CONTEXT_BYTES, FOLLOW_WINDOW_BYTES
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.auth.models import AuditLog, Capability, Role, RoleGrant, ScopeType, User
from app.collectors.base import RANGE_MAX_BYTES
from app.config import get_settings
from app.crypto import follow_head_key
from app.db import get_session
from app.models import Customer, PatternKind, Protocol, RedactionRule, Rule, RuleType, Source
from app.redaction import DETECTOR_NAMES, head_digest, long_line_label, redact_bytes
from app.redaction_rules import seed_builtin_redaction_rules
from app.scratch import get_scratch_store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

SAMPLE = (
    "2024-05-01 10:20:30 login user=alice@example.com from 10.1.2.3 and 2001:db8::7\n"
    "charge card 4111 1111 1111 1111 ok; iban PT50 0002 0123 1234 5678 9015 4\n"
    "GET /x Authorization: Bearer abcdefgh12345678\n"
    "dsn=postgres://app:Sup3rS3cret@db:5432/x password=hunter2\n"
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U\n"
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
# One value per detector, and pieces of them that mustn't show either.
SECRETS = [
    "alice@example.com",
    "10.1.2.3",
    "2001:db8::7",
    "4111 1111 1111 1111",
    "PT50 0002 0123 1234 5678 9015 4",
    "abcdefgh12345678",
    "Sup3rS3cret",
    "hunter2",
    "eyJhbGciOiJIUzI1NiJ9",
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW",
    "alice@",
    "Sup3r",
    "hunt",
    "abcdefgh",
]


def assert_no_secret(data: bytes | str) -> None:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    for secret in SECRETS:
        assert secret not in text, secret


# Content compressed in a format PerchTail doesn't decompress, told by its
# first bytes. bzip2 and xz are real; for zstd, 7z and rar only the
# signature is real, followed by SAMPLE in clear, so a format that slipped
# through would show the values.
UNSUPPORTED_FORMATS = ["bz2", "xz", "zst", "7z", "rar"]
# gzip and zip content in a file whose name doesn't say so (app.log.1), so
# PerchTail doesn't decompress it. Both are stored uncompressed, so SAMPLE is
# in clear inside them and one that slipped through would show the values.
MISNAMED_FORMATS = ["gzip", "zip"]


def _stored_zip(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("app.log", data)
    return buffer.getvalue()


def compressed_sample(fmt: str, data: bytes | None = None) -> bytes:
    data = SAMPLE.encode() if data is None else data
    return {
        "bz2": lambda: bz2.compress(data),
        "xz": lambda: lzma.compress(data),
        "zst": lambda: b"\x28\xb5\x2f\xfd" + data,
        "7z": lambda: b"7z\xbc\xaf\x27\x1c" + data,
        "rar": lambda: b"Rar!\x1a\x07\x01\x00" + data,
        "gzip": lambda: gzip.compress(data, compresslevel=0),
        "zip": lambda: _stored_zip(data),
    }[fmt]()


def all_rules() -> list[dict]:
    return [{"name": name, "detector": name} for name in DETECTOR_NAMES]


@pytest.fixture()
def scratch_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    get_settings.cache_clear()
    get_scratch_store.cache_clear()
    store = get_scratch_store()
    yield store
    get_settings.cache_clear()
    get_scratch_store.cache_clear()


def _enable_redaction(session, *, rules_on: bool = True) -> None:
    seed_builtin_redaction_rules(session)
    if rules_on:
        for rule in session.exec(select(RedactionRule)).all():
            rule.enabled = True
            session.add(rule)
    system_settings.set_bool(session, "redaction_enabled", True)
    session.commit()


@pytest.fixture()
def viewer(session, tmp_path, scratch_store):
    """(app, client, source, folder): a local source over `folder`, whose
    rules show logs, .gz files and archives, opened by a super admin, with
    redaction on and every built-in detector enabled."""
    folder = tmp_path / "logs"
    folder.mkdir()
    app = FastAPI()
    app.include_router(archive_router)
    app.dependency_overrides[get_session] = lambda: session
    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    source = Source(name="local", protocol=Protocol.local, host="localhost", base_path=str(folder))
    session.add(source)
    session.commit()
    for order, pattern in enumerate(("**/*",)):
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
    _enable_redaction(session)
    return app, TestClient(app), source, folder


def _open(client, source, path, **params):
    return client.get(f"/sources/{source.id}/open", params={"path": path, **params})


def _poll(client, source, offset, head=None, path="app.log", **extra):
    params = {"path": path, "offset": offset, **extra}
    if head is not None:
        params["head"] = head
    return client.get(f"/sources/{source.id}/follow", params=params)


def _scratch_files(store) -> list[Path]:
    return [p for p in store.root.rglob("*") if p.is_file()]


# -- /open --------------------------------------------------------------------


def test_open_redacts_every_detector_for_a_local_plain_file(viewer, scratch_store):
    _, client, source, folder = viewer
    (folder / "app.log").write_text(SAMPLE)

    response = _open(client, source, "app.log")
    assert response.status_code == 200
    assert_no_secret(response.content)
    expected, result = redact_bytes(SAMPLE.encode(), all_rules(), final=False)
    assert response.content == expected
    assert response.headers["x-redacted"] == "1"
    assert int(response.headers["x-redaction-count"]) == sum(result.counts.values()) >= 10
    assert response.headers["x-source-offset"] == str(len(SAMPLE))
    assert response.headers["x-follow-partial"] == "0"
    assert response.content.count(b"\n") == SAMPLE.count("\n")
    assert b"[redacted: card ****1111]" in response.content
    # A local file is served from scratch once redacted, so it is released.
    key = response.headers["x-scratch-key"]
    assert key in scratch_store._entries
    for path in _scratch_files(scratch_store):
        assert_no_secret(path.read_bytes())


def test_a_redaction_slower_than_the_idle_time_keeps_its_scratch_file(
    viewer, scratch_store, monkeypatch
):
    # The idle sweep runs while the file is being redacted into scratch,
    # after SCRATCH_IDLE_SECONDS: the entry is busy, so it stays, and the
    # close leaves nothing behind.
    import time

    import app.api.archive as archive_module

    _, client, source, folder = viewer
    (folder / "app.log").write_text(SAMPLE)
    idle = 0.2
    monkeypatch.setattr(get_settings(), "scratch_idle_seconds", idle)
    redact_file = archive_module._redact_file

    def slow_redact(original, destination, *args, **kwargs):
        destination.write_bytes(b"partial")
        time.sleep(idle * 3)
        scratch_store.sweep_idle(get_settings().scratch_idle_seconds)
        return redact_file(original, destination, *args, **kwargs)

    monkeypatch.setattr(archive_module, "_redact_file", slow_redact)

    response = _open(client, source, "app.log")
    assert response.status_code == 200
    expected, _ = redact_bytes(SAMPLE.encode(), all_rules(), final=False)
    assert response.content == expected
    assert client.post(f"/sources/{source.id}/close", json={"path": "app.log"}).status_code == 204
    assert list(scratch_store.root.rglob("*")) == []


def test_open_over_a_remote_source_keeps_only_the_redacted_copy(
    session, viewer, scratch_store, monkeypatch
):
    _, client, _, _ = viewer
    remote = Source(
        name="ssh",
        protocol=Protocol.ssh,
        host="app01.example.com",
        base_path="/var/log",
        credential_ref="encrypted",
    )
    session.add(remote)
    session.commit()
    session.add(
        Rule(
            source_id=remote.id,
            order=0,
            type=RuleType.include,
            pattern="*.log",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    fake = _FakeSSH({"/var/log/app.log": SAMPLE})
    monkeypatch.setattr(ssh_module, "_connect", lambda source: fake)

    response = _open(client, remote, "app.log")
    assert response.status_code == 200
    assert_no_secret(response.content)
    files = _scratch_files(scratch_store)
    assert len(files) == 1
    assert not files[0].name.endswith(".source")
    assert_no_secret(files[0].read_bytes())


def test_open_redacts_a_gz_file_and_its_last_line(viewer):
    _, client, source, folder = viewer
    # No newline at the end: a .gz file is complete, so its last line is
    # redacted as it is rather than held back.
    (folder / "app.log.gz").write_bytes(gzip.compress(SAMPLE.encode() + b"password=hunter2"))

    response = _open(client, source, "app.log.gz")
    assert response.status_code == 200
    assert_no_secret(response.content)
    assert response.content.endswith(b"password=[redacted: password]")
    assert response.headers["x-follow-partial"] == "0"
    assert "x-follow-head" not in response.headers


def test_open_redacts_an_archive_member(viewer):
    _, client, source, folder = viewer
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("inner/app.log", SAMPLE)
    (folder / "bundle.zip").write_bytes(buffer.getvalue())

    response = _open(client, source, "bundle.zip", member="inner/app.log")
    assert response.status_code == 200
    assert_no_secret(response.content)
    assert response.headers["x-redacted"] == "1"


@pytest.mark.parametrize("member", [None, "nested.zip"])
def test_a_whole_archive_is_refused_and_audited(session, viewer, member):
    _, client, source, folder = viewer
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("app.log", SAMPLE)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("nested.zip", inner.getvalue())
    (folder / "bundle.zip").write_bytes(outer.getvalue())

    params = {"member": member} if member else {}
    response = _open(client, source, "bundle.zip", **params)
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.open_denied")).one()
    assert event.event_metadata["reason"] == "redaction_whole_archive"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_open_redacts_utf16_and_serves_utf8(viewer, encoding):
    _, client, source, folder = viewer
    content = SAMPLE.encode(encoding)
    (folder / "app.log").write_bytes(content)

    response = _open(client, source, "app.log")
    assert response.status_code == 200
    text = response.content.decode("utf-8")
    assert_no_secret(text)
    assert "[redacted: email]" in text
    assert response.headers["x-source-offset"] == str(len(content))


def test_utf32_is_refused_and_audited(session, viewer):
    _, client, source, folder = viewer
    (folder / "app.log").write_bytes(SAMPLE.encode("utf-32"))

    response = _open(client, source, "app.log")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "redaction_unsupported_encoding"
    assert "hunter2" not in response.text
    response = _poll(client, source, 0)
    assert response.status_code == 409
    events = session.exec(select(AuditLog).where(AuditLog.action == "file.open_denied")).all()
    assert [e.event_metadata["reason"] for e in events] == ["redaction_unsupported_encoding"] * 2
    assert events[1].event_metadata["follow"] is True


@pytest.mark.parametrize("fmt", UNSUPPORTED_FORMATS)
@pytest.mark.parametrize("name", ["app.log", "app.log.{fmt}"])
def test_an_unsupported_compressed_format_is_refused_and_audited(session, viewer, fmt, name):
    _, client, source, folder = viewer
    # Told by the content, whatever the file is called.
    path = name.format(fmt=fmt)
    (folder / path).write_bytes(compressed_sample(fmt))

    response = _open(client, source, path)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "redaction_unsupported_format"
    assert_no_secret(response.text)
    response = _poll(client, source, 0, path=path)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "redaction_unsupported_format"
    assert_no_secret(response.text)
    events = session.exec(select(AuditLog).where(AuditLog.action == "file.open_denied")).all()
    assert [e.event_metadata["reason"] for e in events] == ["redaction_unsupported_format"] * 2
    assert events[1].event_metadata["follow"] is True


@pytest.mark.parametrize("fmt", MISNAMED_FORMATS)
@pytest.mark.parametrize("name", ["app.log", "app.log.1"])
def test_gzip_or_zip_content_under_another_name_is_refused_and_audited(session, viewer, fmt, name):
    _, client, source, folder = viewer
    (folder / name).write_bytes(compressed_sample(fmt))

    response = _open(client, source, name)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "redaction_unsupported_format"
    assert_no_secret(response.text)
    response = _poll(client, source, 0, path=name)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "redaction_unsupported_format"
    assert_no_secret(response.text)
    events = session.exec(select(AuditLog).where(AuditLog.action == "file.open_denied")).all()
    assert [e.event_metadata["reason"] for e in events] == ["redaction_unsupported_format"] * 2
    assert events[1].event_metadata["follow"] is True


def test_gz_and_zip_named_as_such_still_open_redacted(viewer):
    _, client, source, folder = viewer
    (folder / "app.log.gz").write_bytes(gzip.compress(SAMPLE.encode()))
    (folder / "bundle.zip").write_bytes(compressed_sample("zip"))

    for path, params in (("app.log.gz", {}), ("bundle.zip", {"member": "app.log"})):
        response = _open(client, source, path, **params)
        assert response.status_code == 200, (path, params)
        assert "[redacted: email]" in response.text
        assert_no_secret(response.text)


@pytest.mark.parametrize("fmt", UNSUPPORTED_FORMATS + MISNAMED_FORMATS)
def test_an_unsupported_compressed_format_inside_gz_or_a_member_is_refused(viewer, fmt):
    _, client, source, folder = viewer
    (folder / "app.log.gz").write_bytes(gzip.compress(compressed_sample(fmt)))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("app.log", compressed_sample(fmt))
        zf.writestr("app.log.gz", gzip.compress(compressed_sample(fmt)))
    (folder / "bundle.zip").write_bytes(buffer.getvalue())

    for path, params in (
        ("app.log.gz", {}),
        ("bundle.zip", {"member": "app.log"}),
        ("bundle.zip", {"member": "app.log.gz"}),
    ):
        response = _open(client, source, path, **params)
        assert response.status_code == 409, (path, params)
        assert response.json()["detail"]["error_code"] == "redaction_unsupported_format"


def test_without_redaction_compressed_formats_open_as_before(session, viewer):
    _, client, source, folder = viewer
    system_settings.set_bool(session, "redaction_enabled", False)
    session.commit()
    content = compressed_sample("xz")
    (folder / "app.log").write_bytes(content)

    assert _open(client, source, "app.log").content == content
    assert _poll(client, source, 0).content == content
    assert session.exec(select(AuditLog).where(AuditLog.action == "file.open_denied")).all() == []


def _gz_member_archive(kind: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("inner/app.log.gz", content)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
            info = tarfile.TarInfo("inner/app.log.gz")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize("kind, archive", [("zip", "bundle.zip"), ("tar", "bundle.tar.gz")])
def test_open_of_a_gz_member_decompresses_and_redacts_it(
    session, viewer, scratch_store, kind, archive
):
    _, client, source, folder = viewer
    content = SAMPLE.encode() + b"password=hunter2"
    (folder / archive).write_bytes(_gz_member_archive(kind, gzip.compress(content)))

    response = _open(client, source, archive, member="inner/app.log.gz")
    assert response.status_code == 200
    assert_no_secret(response.content)
    # Complete content, like a .gz file: the last line is redacted as it is.
    assert response.content == redact_bytes(content, all_rules(), final=True)[0]
    assert response.headers["x-redacted"] == "1"
    assert "x-follow-head" not in response.headers
    assert 'filename="app.log"' in response.headers["content-disposition"]
    files = _scratch_files(scratch_store)
    assert len(files) == 1
    assert_no_secret(files[0].read_bytes())
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.open")).one()
    assert event.event_metadata["redaction"] == "applied"
    # The Tail doesn't follow archive members: the viewer reloads them whole.
    response = _poll(client, source, 0, path=archive)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "follow_unsupported"


def test_open_of_a_corrupt_gz_member_is_refused_and_audited(session, viewer, scratch_store):
    _, client, source, folder = viewer
    (folder / "bundle.zip").write_bytes(_gz_member_archive("zip", b"not gzip " + SAMPLE.encode()))

    response = _open(client, source, "bundle.zip", member="inner/app.log.gz")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "redaction_unreadable"
    assert_no_secret(response.text)
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.open_denied")).one()
    assert event.event_metadata["reason"] == "redaction_unreadable"
    assert event.event_metadata["path"] == "bundle.zip/inner/app.log.gz"
    assert _scratch_files(scratch_store) == []


def test_open_of_a_gz_member_past_the_decompression_limit_is_refused(
    viewer, scratch_store, monkeypatch
):
    _, client, source, folder = viewer
    monkeypatch.setenv("MAX_DECOMPRESSED_MB", "0.01")
    get_settings.cache_clear()
    content = SAMPLE.encode() * 1000
    (folder / "bundle.zip").write_bytes(_gz_member_archive("zip", gzip.compress(content)))

    response = _open(client, source, "bundle.zip", member="inner/app.log.gz")
    assert response.status_code == 413
    assert_no_secret(response.text)
    assert _scratch_files(scratch_store) == []


def test_open_holds_back_an_unfinished_last_line(viewer):
    _, client, source, folder = viewer
    (folder / "app.log").write_bytes(b"first line\ncard 4111 1111 11")

    response = _open(client, source, "app.log")
    assert response.content == b"first line\n[redacted: incomplete line]"
    assert response.headers["x-source-offset"] == "11"
    assert response.headers["x-follow-partial"] == str(len(b"[redacted: incomplete line]"))


def test_open_and_follow_are_audited_as_redacted(session, viewer):
    _, client, source, folder = viewer
    (folder / "app.log").write_text(SAMPLE)
    _open(client, source, "app.log")
    _poll(client, source, 0, start=True)
    for action in ("file.open", "file.follow"):
        event = session.exec(select(AuditLog).where(AuditLog.action == action)).one()
        assert event.event_metadata["redaction"] == "applied"
        assert event.event_metadata["redaction_at"] == "server"


def test_redaction_applies_to_users_who_arent_super_admins(session, viewer):
    app, client, source, folder = viewer
    customer = Customer(name="Acme")
    session.add(customer)
    session.commit()
    source.customer_id = customer.id
    session.add(source)
    role = Role(name="reader")
    session.add(role)
    session.commit()
    session.add(
        RoleGrant(
            role_id=role.id,
            scope_type=ScopeType.source,
            scope_id=source.id,
            capabilities=[Capability.view],
        )
    )
    reader = User(username="reader", role_id=role.id)
    session.add(reader)
    session.commit()
    app.dependency_overrides[get_current_active_user] = lambda: reader
    (folder / "app.log").write_text(SAMPLE)

    response = _open(client, source, "app.log")
    assert response.status_code == 200
    assert_no_secret(response.content)
    assert_no_secret(_poll(client, source, 0).content)


# -- redaction off: as before ---------------------------------------------------


@pytest.mark.parametrize("setup", ["off", "no_rules"])
def test_without_redaction_answers_are_unchanged(session, viewer, setup):
    _, client, source, folder = viewer
    if setup == "off":
        system_settings.set_bool(session, "redaction_enabled", False)
    else:
        for rule in session.exec(select(RedactionRule)).all():
            rule.enabled = False
            session.add(rule)
    session.commit()
    content = SAMPLE.encode() + b"password=still being writ"
    (folder / "app.log").write_bytes(content)

    opened = _open(client, source, "app.log")
    assert opened.content == content
    for header in ("x-redacted", "x-redaction-count", "x-source-offset", "x-follow-partial"):
        assert header not in opened.headers
    polled = _poll(client, source, 10)
    assert polled.content == content[10:]
    assert polled.headers["x-follow-offset"] == str(len(content))
    assert "x-redacted" not in polled.headers
    assert "x-follow-aligned" not in polled.headers
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.open")).one()
    assert "redaction" not in event.event_metadata


def test_follow_head_is_an_hmac_not_a_plain_hash(session, viewer):
    _, client, source, folder = viewer
    system_settings.set_bool(session, "redaction_enabled", False)
    session.commit()
    (folder / "app.log").write_bytes(b"password=1234\n")
    head = _open(client, source, "app.log").headers["x-follow-head"]
    assert head != hashlib.sha256(b"password=1234\n").hexdigest()[:32]
    assert head == head_digest(follow_head_key(source.id), b"password=1234\n", 14)


# -- /follow --------------------------------------------------------------------


def test_follow_from_the_start_redacts(viewer):
    _, client, source, folder = viewer
    (folder / "app.log").write_text(SAMPLE)

    response = _poll(client, source, 0)
    assert response.status_code == 200
    assert_no_secret(response.content)
    assert response.headers["x-redacted"] == "1"
    assert response.headers["x-follow-offset"] == str(len(SAMPLE))
    assert int(response.headers["x-redaction-count"]) >= 10
    assert "x-follow-aligned" not in response.headers


def test_follow_from_inside_a_line_starts_at_the_next_one(viewer):
    _, client, source, folder = viewer
    content = b"user password=hunter2 logged in\nnext line\n"
    (folder / "app.log").write_bytes(content)

    # "Open only the end": the offset falls inside the password.
    response = _poll(client, source, content.index(b"nter2"), start=True)
    assert response.content == b"next line\n"
    assert response.headers["x-follow-aligned"] == "1"
    assert response.headers["x-follow-offset"] == str(len(content))


def test_a_value_split_between_polls_never_shows(viewer):
    _, client, source, folder = viewer
    log = folder / "app.log"
    log.write_bytes(b"start\npassword=hun")

    first = _poll(client, source, 0)
    assert first.content == b"start\n[redacted: incomplete line]"
    assert first.headers["x-follow-offset"] == "6"
    assert first.headers["x-follow-partial"] == str(len(b"[redacted: incomplete line]"))
    assert "x-follow-more" not in first.headers
    head = first.headers["x-follow-head"]

    # Nothing new: the unfinished line comes back as the marker again.
    again = _poll(client, source, 6, head)
    assert again.content == b"[redacted: incomplete line]"
    assert again.headers["x-follow-offset"] == "6"

    with log.open("ab") as f:
        f.write(b"ter2 done\n")
    done = _poll(client, source, 6, again.headers["x-follow-head"])
    assert done.content == b"password=[redacted: password] done\n"
    assert done.headers["x-follow-partial"] == "0"
    assert "x-follow-reset" not in done.headers


def test_a_private_key_opened_before_the_offset_stays_hidden(viewer):
    _, client, source, folder = viewer
    log = folder / "app.log"
    log.write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0first\n")
    first = _poll(client, source, 0)
    assert b"MIIEpAIBAAKCAQEA0first" not in first.content
    offset = int(first.headers["x-follow-offset"])

    with log.open("ab") as f:
        f.write(b"MIIEsecondkeyline\n-----END RSA PRIVATE KEY-----\nafter\n")
    response = _poll(client, source, offset, first.headers["x-follow-head"])
    assert b"MIIEsecondkeyline" not in response.content
    assert response.content.endswith(b"\nafter\n")
    assert response.content.count(b"\n") == 3


def test_tail_after_a_redacted_open_neither_repeats_nor_loses_lines(viewer):
    _, client, source, folder = viewer
    log = folder / "app.log"
    log.write_bytes(SAMPLE.encode() + b"tail token=abc")

    opened = _open(client, source, "app.log")
    partial = int(opened.headers["x-follow-partial"])
    shown = opened.content[: len(opened.content) - partial]
    offset = int(opened.headers["x-source-offset"])
    head = opened.headers["x-follow-head"]

    with log.open("ab") as f:
        f.write(b"def\nlast email bob@example.org\n")
    polled = _poll(client, source, offset, head)
    assert "x-follow-reset" not in polled.headers
    shown += polled.content[: len(polled.content) - int(polled.headers["x-follow-partial"])]

    whole, _ = redact_bytes(log.read_bytes(), all_rules(), final=False)
    assert shown == whole
    assert_no_secret(shown)
    assert b"bob@example.org" not in shown


def test_a_line_longer_than_a_poll_is_skipped_with_a_marker(viewer):
    _, client, source, folder = viewer
    log = folder / "app.log"
    long_line = b"password=hunter2 " + b"x" * RANGE_MAX_BYTES + b"\n"
    log.write_bytes(long_line + b"next line\n")

    response = _poll(client, source, 0)
    assert response.content == f"[redacted: {long_line_label(FOLLOW_WINDOW_BYTES)}]\n".encode()
    assert response.headers["x-follow-more"] == "1"
    offset = int(response.headers["x-follow-offset"])
    assert 0 < offset < len(long_line)

    rest = b""
    head = response.headers["x-follow-head"]
    while "x-follow-more" in response.headers:
        response = _poll(client, source, offset, head)
        rest += response.content
        offset = int(response.headers["x-follow-offset"])
        head = response.headers["x-follow-head"]
    assert rest == b"next line\n"
    assert offset == len(long_line) + len(b"next line\n")


def test_the_context_read_before_the_offset_is_never_sent(viewer):
    _, client, source, folder = viewer
    log = folder / "app.log"
    before = b"old password=hunter2\n" * (FOLLOW_CONTEXT_BYTES // 20)
    log.write_bytes(before + b"new line\n")
    response = _poll(client, source, len(before))
    assert response.content == b"new line\n"


# Content that changes encoding part way: plain text, then a UTF-16 line,
# then plain text, then a UTF-32 line and plain text after it.
MIDSTREAM_START = b"2024-05-01 10:20:30 service started, nothing to see in this line\n"
MIDSTREAM_UTF16 = "user password=hunter2 card 4111 1111 1111 1111\n".encode("utf-16-le")
MIDSTREAM_MIDDLE = b"plain again\n"
MIDSTREAM_UTF32 = "token=Sup3rS3cret\n".encode("utf-32-le")
MIDSTREAM_AFTER = b"after alice@example.com\n"
MIDSTREAM = MIDSTREAM_START + MIDSTREAM_UTF16 + MIDSTREAM_MIDDLE + MIDSTREAM_UTF32 + MIDSTREAM_AFTER
MIDSTREAM_REFUSED_AT = len(MIDSTREAM_START + MIDSTREAM_UTF16 + MIDSTREAM_MIDDLE)


def assert_no_midstream_secret(data: bytes) -> None:
    for secret in ("hunter2", "4111 1111 1111 1111", "Sup3rS3cret", "alice@example.com"):
        for encoding in ("utf-8", "utf-16-le", "utf-32-le"):
            assert secret.encode(encoding) not in data, (secret, encoding)


def test_open_reads_content_that_changes_encoding_part_way(session, viewer):
    _, client, source, folder = viewer
    (folder / "app.log").write_bytes(MIDSTREAM)

    response = _open(client, source, "app.log")
    assert response.status_code == 200
    assert response.content.startswith(MIDSTREAM_START)
    assert response.content.endswith(b"[redacted: unsupported encoding]")
    assert_no_midstream_secret(response.content)
    # The Tail goes on after what was read.
    assert response.headers["x-source-offset"] == str(len(MIDSTREAM))
    assert response.headers["x-follow-partial"] == "0"
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.open")).one()
    assert event.event_metadata["redaction"] == "applied"
    assert event.event_metadata["redaction_refused"] == "redaction_unsupported_encoding"
    assert event.event_metadata["redaction_refused_offset"] == MIDSTREAM_REFUSED_AT
    assert "offset" not in event.event_metadata


def test_open_without_a_change_of_encoding_says_nothing_refused(session, viewer):
    _, client, source, folder = viewer
    (folder / "app.log").write_bytes(MIDSTREAM_START + MIDSTREAM_UTF16)
    response = _open(client, source, "app.log")
    assert_no_midstream_secret(response.content)
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.open")).one()
    assert "redaction_refused" not in event.event_metadata
    assert "redaction_refused_offset" not in event.event_metadata


def test_follow_refuses_the_rest_part_way_and_audits_every_such_poll(session, viewer):
    _, client, source, folder = viewer
    log = folder / "app.log"
    log.write_bytes(MIDSTREAM_START)
    first = _poll(client, source, 0, start=True)
    assert first.content == MIDSTREAM_START
    with log.open("ab") as f:
        f.write(MIDSTREAM[len(MIDSTREAM_START) :])

    # Not the start of the Tail, but the refusal is audited all the same,
    # with the offset this poll read from.
    response = _poll(client, source, len(MIDSTREAM_START), first.headers["x-follow-head"])
    assert response.content.endswith(b"[redacted: unsupported encoding]")
    assert_no_midstream_secret(response.content)
    assert response.headers["x-follow-offset"] == str(len(MIDSTREAM))
    events = session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all()
    assert len(events) == 2
    assert events[0].event_metadata["offset"] == 0
    assert "redaction_refused" not in events[0].event_metadata
    assert "redaction_refused_offset" not in events[0].event_metadata
    assert events[1].event_metadata["offset"] == len(MIDSTREAM_START)
    assert events[1].event_metadata["redaction_refused"] == "redaction_unsupported_encoding"
    assert events[1].event_metadata["redaction_refused_offset"] == MIDSTREAM_REFUSED_AT

    # A later poll that meets no UTF-32 isn't audited.
    with log.open("ab") as f:
        f.write(b"more plain\n")
    later = _poll(client, source, len(MIDSTREAM), response.headers["x-follow-head"])
    assert later.content == b"more plain\n"
    assert len(session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all()) == 2


class _FakeRemoteFile(io.BytesIO):
    """An open remote file, as paramiko's SFTPFile offers it."""

    def prefetch(self, file_size=None):
        pass

    def stat(self):
        attr = type("Attr", (), {})()
        attr.st_size = len(self.getvalue())
        return attr

    def readv(self, chunks):
        for offset, length in chunks:
            self.seek(offset)
            yield self.read(length)


class _FakeSFTP:
    def __init__(self, files: dict[str, str]):
        self.files = {path: content.encode() for path, content in files.items()}

    def normalize(self, path):
        return path

    def open(self, remote_path, mode="r"):
        return _FakeRemoteFile(self.files[remote_path])

    def stat(self, remote_path):
        attr = type("Attr", (), {})()
        attr.st_mode = stat_module.S_IFREG
        attr.st_size = len(self.files[remote_path])
        return attr

    def get_channel(self):
        return type("Channel", (), {"settimeout": lambda self, seconds: None})()

    def close(self):
        pass


class _FakeSSH:
    def __init__(self, files: dict[str, str]):
        self._sftp = _FakeSFTP(files)

    def open_sftp(self):
        return self._sftp

    def close(self):
        pass
