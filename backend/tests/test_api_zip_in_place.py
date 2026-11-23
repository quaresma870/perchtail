"""A .zip on an SSH source is listed and its members opened by reading only
the bytes they need (collectors/ranged.py), never by copying the archive
whole: MAX_FETCH_MB counts what an operation transfers, and an archive that
changes while it is read, or whose member fails its CRC, is refused."""

import io
import os
import zipfile

import pytest
from app.auth.models import AuditLog
from app.models import RuleType
from sqlmodel import select

from tests.test_api_archive import (  # noqa: F401 (fixtures)
    _add_rule,
    _make_source,
    _make_user,
    _RemoteFile,
    app_client,
    fake_sftp,
    scratch_store,
)

MIB = 1024 * 1024
ZIP_PATH = "/var/log/appname/logs.zip"
APP_LOG = b"2026-11-23 10:00:00 INFO a line of the log\n" * 200


def _zip(members: dict[str, bytes], *, stored: bool = False) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w", compression=method) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def _limit(monkeypatch, limit: int) -> None:
    monkeypatch.setattr("app.collectors.base.max_fetch_bytes", lambda: limit)
    monkeypatch.setattr("app.api.archive.max_fetch_bytes", lambda: limit)


@pytest.fixture()
def big_zip(app_client, session, fake_sftp, scratch_store, monkeypatch):  # noqa: F811
    """A 6 MiB .zip (a 6 MiB member that doesn't compress, then app.log) on
    an SSH source, with MAX_FETCH_MB at 3 MiB."""
    _limit(monkeypatch, 3 * MIB)
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files[ZIP_PATH] = _zip({"big.bin": os.urandom(6 * MIB), "app.log": APP_LOG})
    assert len(fake_sftp.files[ZIP_PATH]) > 6 * MIB
    return client, source, fake_sftp


@pytest.mark.parametrize("all_members", [False, True])
def test_a_zip_over_max_fetch_mb_is_listed_reading_only_its_end(big_zip, all_members):
    client, source, sftp = big_zip
    params = {"path": "logs.zip", **({"all": "true"} if all_members else {})}

    response = client.get(f"/sources/{source.id}/browse", params=params)

    assert response.status_code == 200, response.text
    assert {entry["name"] for entry in response.json()} == {"big.bin", "app.log"}
    assert sftp.bytes_sent < 2 * MIB


def test_a_member_of_a_zip_over_max_fetch_mb_is_opened_reading_only_its_own_bytes(
    big_zip, scratch_store  # noqa: F811
):
    client, source, sftp = big_zip

    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )

    assert response.status_code == 200, response.text
    assert response.content == APP_LOG
    # The archive's end (to find the member), then the member's own stretch.
    assert sftp.bytes_sent < 2 * MIB + len(APP_LOG)


def test_a_member_that_would_need_more_than_max_fetch_mb_is_the_413_of_today(
    big_zip, session, scratch_store  # noqa: F811
):
    client, source, sftp = big_zip

    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "big.bin"}
    )

    assert response.status_code == 413, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "fetch_too_large"
    assert detail["size_bytes"] > 6 * MIB
    assert detail["limit_bytes"] == 3 * MIB
    # Refused before its data was read.
    assert sftp.bytes_sent < 2 * MIB
    assert list(scratch_store.root.rglob("*.*")) == []


@pytest.mark.parametrize("all_members", [False, True])
def test_a_zip_whose_listing_needs_more_than_max_fetch_mb_is_a_413(
    app_client, session, fake_sftp, scratch_store, monkeypatch, all_members  # noqa: F811
):
    """The listing itself would transfer more than the limit: the 413
    opening one of its members gives, and nothing in the audit log."""
    _limit(monkeypatch, 8)
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files[ZIP_PATH] = _zip({"app.log": APP_LOG})
    params = {"path": "logs.zip", **({"all": "true"} if all_members else {})}

    browsed = client.get(f"/sources/{source.id}/browse", params=params)
    opened = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )

    assert browsed.status_code == 413
    assert browsed.json() == opened.json()
    assert browsed.json()["detail"]["limit_bytes"] == 8
    assert session.exec(select(AuditLog)).all() == []


class _GrowingFile(_RemoteFile):
    """A remote file that someone appends to once it has been read from:
    its handle's size is no longer the one it had when it was opened."""

    def readv(self, chunks):
        yield from super().readv(chunks)
        self._grown = True

    def stat(self):
        attributes = super().stat()
        if getattr(self, "_grown", False):
            attributes.st_size += 100
        return attributes


@pytest.mark.parametrize(
    "endpoint, action", [("open", "file.open_denied"), ("download", "file.download_denied")]
)
def test_a_zip_that_changes_while_its_member_is_read_is_refused_as_unreadable(
    app_client, session, fake_sftp, scratch_store, monkeypatch, endpoint, action  # noqa: F811
):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files[ZIP_PATH] = _zip({"app.log": APP_LOG})
    monkeypatch.setattr(
        fake_sftp, "open", lambda path, mode="r": _GrowingFile(fake_sftp, fake_sftp.files[path])
    )

    response = client.get(
        f"/sources/{source.id}/{endpoint}", params={"path": "logs.zip", "member": "app.log"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "archive_unreadable"
    event = session.exec(select(AuditLog).where(AuditLog.action == action)).one()
    assert event.event_metadata["reason"] == "archive_unreadable"
    assert list(scratch_store.root.rglob("*.*")) == []


def test_a_zip_that_changes_while_it_is_listed_is_refused_as_unreadable(
    app_client, session, fake_sftp, scratch_store, monkeypatch  # noqa: F811
):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files[ZIP_PATH] = _zip({"app.log": APP_LOG})
    monkeypatch.setattr(
        fake_sftp, "open", lambda path, mode="r": _GrowingFile(fake_sftp, fake_sftp.files[path])
    )

    response = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip"})

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "archive_unreadable"


def test_a_member_that_fails_its_crc_is_refused_and_nothing_is_served(
    app_client, session, fake_sftp, scratch_store  # noqa: F811
):
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    content = bytearray(_zip({"app.log": APP_LOG}, stored=True))
    at = bytes(content).index(APP_LOG[:40])
    content[at] ^= 0xFF
    fake_sftp.files[ZIP_PATH] = bytes(content)

    response = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "archive_unreadable"
    assert list(scratch_store.root.rglob("*.*")) == []


def test_a_zip_with_data_before_it_is_listed_and_opened_in_place(
    app_client, session, fake_sftp, scratch_store  # noqa: F811
):
    """A self-extracting archive: the zip after a stub, its offsets
    relative to where the zip starts."""
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files[ZIP_PATH] = b"MZ" + b"\0" * 4094 + _zip({"app.log": APP_LOG})

    browsed = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip"})
    opened = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )

    assert browsed.status_code == 200, browsed.text
    assert [entry["name"] for entry in browsed.json()] == ["app.log"]
    assert opened.status_code == 200, opened.text
    assert opened.content == APP_LOG


def test_a_tar_gz_is_still_copied_whole_and_read_from_the_copy(
    app_client, session, fake_sftp, scratch_store  # noqa: F811
):
    import tarfile

    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        info = tarfile.TarInfo("app.log")
        info.size = len(APP_LOG)
        tf.addfile(info, io.BytesIO(APP_LOG))
    fake_sftp.files["/var/log/appname/logs.tar.gz"] = buffer.getvalue()

    opened = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.tar.gz", "member": "app.log"}
    )

    assert opened.status_code == 200, opened.text
    assert opened.content == APP_LOG
    assert fake_sftp.reads == []  # no ranged read
    assert fake_sftp.bytes_sent == len(buffer.getvalue())
