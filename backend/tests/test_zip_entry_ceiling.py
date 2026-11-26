"""A .zip with more than MAX_ZIP_ENTRIES entries is refused as an archive
that can't be read: the count is read from the end-of-central-directory
record (or its ZIP64 version) before zipfile loads the index, both for a
.zip read in place and for one copied whole."""

import io
import struct
import zipfile
from pathlib import Path

import pytest
from app import archives
from app.archives import (
    MAX_ZIP_ENTRIES,
    ZipTooManyEntriesError,
    extract_member,
    scan_members,
    zip_entry_count,
)
from app.auth.models import AuditLog
from app.models import RuleType
from sqlmodel import select

from tests.test_api_archive import (  # noqa: F401 (fixtures)
    _add_rule,
    _make_source,
    _make_user,
    app_client,
    fake_sftp,
    scratch_store,
)
from tests.test_archives import _skipped_list, _upper

ZIP_PATH = "/var/log/appname/logs.zip"
EOCD = struct.Struct("<4s4H2LH")


def _zip(members: dict[str, bytes], comment: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
        zf.comment = comment
    return buffer.getvalue()


def _stating(content: bytes, entries: int) -> bytes:
    """`content` with its end-of-central-directory record stating `entries`."""
    at = content.rindex(b"PK\x05\x06")
    fields = list(EOCD.unpack_from(content, at))
    fields[3] = fields[4] = entries
    return content[:at] + EOCD.pack(*fields) + content[at + EOCD.size :]


def _zip64_stating(content: bytes, entries: int) -> bytes:
    """`content` with a ZIP64 record and locator before its end record,
    the ZIP64 record stating `entries` (the end record says 0xFFFF)."""
    at = content.rindex(b"PK\x05\x06")
    fields = list(EOCD.unpack_from(content, at))
    _, _, _, _, _, cd_size, cd_offset, _ = fields
    record = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, cd_size, cd_offset
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, at, 1)
    fields[3] = fields[4] = 0xFFFF
    return content[:at] + record + locator + EOCD.pack(*fields) + content[at + EOCD.size :]


SMALL = _zip({"app.log": b"one line\n", "logs/b.log": b"two\n"})


# -- reading the count -------------------------------------------------------------------


def test_the_count_comes_from_the_end_record():
    assert zip_entry_count(io.BytesIO(SMALL)) == 2
    assert zip_entry_count(io.BytesIO(_stating(SMALL, 60_000))) == 60_000


def test_the_count_is_found_behind_a_comment():
    content = _zip({"a": b"1"}, comment=b"PK" + b"x" * 5000)
    assert zip_entry_count(io.BytesIO(content)) == 1


def test_the_count_comes_from_the_zip64_record():
    content = _zip64_stating(SMALL, 70_000)
    assert zip_entry_count(io.BytesIO(content)) == 70_000


def test_a_file_without_an_end_record_has_no_count():
    assert zip_entry_count(io.BytesIO(b"")) is None
    assert zip_entry_count(io.BytesIO(b"not a zip at all" * 10)) is None


# -- refusing it ---------------------------------------------------------------------------


@pytest.mark.parametrize("stated", [MAX_ZIP_ENTRIES + 1, 0xFFFF])
def test_a_zip_stating_more_than_the_ceiling_is_refused_before_zipfile_reads_it(
    tmp_path, monkeypatch, stated
):
    built = []
    real_zipfile = zipfile.ZipFile
    monkeypatch.setattr(
        archives.zipfile, "ZipFile", lambda *a, **k: built.append(a) or real_zipfile(*a, **k)
    )
    path = tmp_path / "logs.zip"
    path.write_bytes(_stating(SMALL, stated))

    with pytest.raises(ZipTooManyEntriesError) as refused:
        scan_members(path, "logs.zip")
    assert refused.value.entries == stated
    assert isinstance(refused.value, zipfile.BadZipFile)
    with pytest.raises(ZipTooManyEntriesError):
        extract_member(path, "logs.zip", "app.log", tmp_path / "out")
    with pytest.raises(ZipTooManyEntriesError):
        scan_members(io.BytesIO(path.read_bytes()), "logs.zip")
    assert built == []
    assert not (tmp_path / "out").exists()


def test_a_zip64_stating_more_than_the_ceiling_is_refused(tmp_path):
    path = tmp_path / "logs.zip"
    path.write_bytes(_zip64_stating(SMALL, MAX_ZIP_ENTRIES + 1))
    with pytest.raises(ZipTooManyEntriesError):
        scan_members(path, "logs.zip")


def test_the_ceiling_itself_is_allowed(tmp_path):
    path = tmp_path / "logs.zip"
    path.write_bytes(_stating(SMALL, MAX_ZIP_ENTRIES))
    members, cut = scan_members(path, "logs.zip")
    assert [m.name for m in members] == ["app.log", "logs/b.log"] and not cut


def test_a_zip_holding_more_than_its_record_says_is_refused_too(tmp_path, monkeypatch):
    monkeypatch.setattr(archives, "MAX_ZIP_ENTRIES", 3)
    content = _zip({f"f{n}.log": b"x" for n in range(4)})
    path = tmp_path / "logs.zip"
    path.write_bytes(_stating(content, 1))
    with pytest.raises(ZipTooManyEntriesError) as refused:
        scan_members(path, "logs.zip")
    assert refused.value.entries == 4


def test_a_rebuilt_download_skips_a_zip_over_the_ceiling(tmp_path):
    path = tmp_path / "logs.zip"
    path.write_bytes(_stating(SMALL, MAX_ZIP_ENTRIES + 1))
    with pytest.raises(zipfile.BadZipFile):
        archives.rebuild_archive(
            path,
            "logs.zip",
            tmp_path / "rebuilt.zip",
            tmp_path,
            keep=lambda name: True,
            transform=_upper,
            skipped_list=_skipped_list,
        )


# -- through the API, read in place ------------------------------------------------------


@pytest.fixture()
def too_many(app_client, session, fake_sftp, scratch_store):  # noqa: F811
    app, client = app_client
    source = _make_source(session)
    _make_user(session, app, is_super_admin=True)
    _add_rule(session, source, 0, RuleType.include, "**/*")
    fake_sftp.files[ZIP_PATH] = _stating(SMALL, MAX_ZIP_ENTRIES + 1)
    return client, source, session, scratch_store


def test_listing_it_is_a_409_archive_unreadable(too_many):
    client, source, _, _ = too_many
    response = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "archive_unreadable"


@pytest.mark.parametrize(
    "endpoint, action", [("open", "file.open_denied"), ("download", "file.download_denied")]
)
def test_opening_or_downloading_a_member_is_refused_and_audited(too_many, endpoint, action):
    client, source, session, store = too_many
    response = client.get(
        f"/sources/{source.id}/{endpoint}", params={"path": "logs.zip", "member": "app.log"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "archive_unreadable"
    event = session.exec(select(AuditLog).where(AuditLog.action == action)).one()
    assert event.event_metadata["reason"] == "archive_unreadable"
    assert list(Path(store.root).rglob("*.*")) == []
