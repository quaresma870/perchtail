"""Redaction in downloads (#382, #249): with redaction on and the
redaction_downloads setting on, /download and /download-zip send content
redacted on the server, for every user: single files, .gz files, archive
members, whole archives rebuilt in the same format and folder ZIPs; what
can't be redacted is left out and listed; nothing of a redacted value comes
out. With redaction_downloads off, downloads are as before."""

import gzip
import io
import tarfile
import zipfile
from pathlib import Path

import app.api.archive as archive_module
import app.collectors.ssh as ssh_module
import pytest
from app import system_settings
from app.api.archive import redacted_filename
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.auth.models import AuditLog, Capability, Role, RoleGrant, ScopeType, User
from app.config import get_settings
from app.db import get_session
from app.models import Customer, PatternKind, Protocol, Rule, RuleType, Source
from app.redaction import redact_bytes
from app.scratch import get_scratch_store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.test_archives import zip_link_info, zip_with_unsupported_method
from tests.test_redaction_endpoints import (
    MISNAMED_FORMATS,
    SAMPLE,
    UNSUPPORTED_FORMATS,
    _enable_redaction,
    _FakeSSH,
    all_rules,
    assert_no_secret,
    compressed_sample,
)

SAMPLE_BYTES = SAMPLE.encode()
WHEN = (2024, 5, 1, 10, 20, 30)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    get_settings.cache_clear()
    get_scratch_store.cache_clear()
    yield get_scratch_store()
    get_settings.cache_clear()
    get_scratch_store.cache_clear()


@pytest.fixture()
def downloads(session, tmp_path, store):
    """(app, client, source, folder): a local source over `folder` showing
    everything but `secret*` files, downloaded by a super admin, with
    redaction on, every built-in detector enabled and the download settings
    at their defaults (redacted, with the suffix)."""
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
    for order, (kind, pattern) in enumerate(
        ((RuleType.include, "**/*"), (RuleType.exclude, "**/secret*"))
    ):
        session.add(
            Rule(
                source_id=source.id,
                order=order,
                type=kind,
                pattern=pattern,
                pattern_kind=PatternKind.glob,
            )
        )
    session.commit()
    app.dependency_overrides[get_current_active_user] = lambda: user
    _enable_redaction(session)
    return app, TestClient(app), source, folder


def _download(client, source, path, **params):
    return client.get(f"/sources/{source.id}/download", params={"path": path, **params})


def _download_zip(client, source, path=""):
    return client.get(f"/sources/{source.id}/download-zip", params={"path": path})


def _redacted(content: bytes, **options) -> bytes:
    return redact_bytes(content, all_rules(), output_encoding="source", **options)[0]


def _disposition_name(response) -> str:
    return response.headers["content-disposition"].split('filename="', 1)[1].rstrip('"')


def _scratch_files(store) -> list[Path]:
    return [p for p in store.root.rglob("*") if p.is_file()]


def _audit(session, action: str) -> AuditLog:
    return session.exec(select(AuditLog).where(AuditLog.action == action)).one()


def _audits(session, action: str) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.action == action)).all())


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=WHEN), content)
    return buffer.getvalue()


def _mark_encrypted(data: bytes, name: str) -> bytes:
    """Sets the "encrypted" flag of member `name` (Python can't write an
    encrypted zip; the flag is all the rebuild looks at)."""
    out = bytearray(data)
    encoded = name.encode()
    headers = ((b"PK\x03\x04", 6, 26, 30), (b"PK\x01\x02", 8, 28, 46))
    for signature, flag_at, length_at, name_at in headers:
        index = out.find(signature)
        while index >= 0:
            length = int.from_bytes(out[index + length_at : index + length_at + 2], "little")
            if out[index + name_at : index + name_at + length] == encoded:
                out[index + flag_at] |= 0x1
            index = out.find(signature, index + 4)
    return bytes(out)


def _tar_gz_bytes(members: dict[str, bytes], symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 1714558830
            tf.addfile(info, io.BytesIO(content))
        if symlink is not None:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "app.log"
            tf.addfile(info)
    return buffer.getvalue()


# -- file names -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("app.log", "app.redacted.log"),
        ("app.log.gz", "app.redacted.log.gz"),
        ("x.gz", "x.redacted.gz"),
        ("logs.tar.gz", "logs.redacted.tar.gz"),
        ("logs.TGZ", "logs.redacted.TGZ"),
        ("bundle.zip", "bundle.redacted.zip"),
        ("README", "README.redacted"),
        (".bashrc", ".bashrc.redacted"),
    ],
)
def test_redacted_filename(name, expected):
    assert redacted_filename(name) == expected


# -- /download ------------------------------------------------------------------


def test_download_of_a_plain_file_is_redacted(session, downloads, store):
    _, client, source, folder = downloads
    # No newline at the end: a download is a snapshot, so its last line is
    # redacted as it is.
    content = SAMPLE_BYTES + b"password=hunter2"
    (folder / "app.log").write_bytes(content)

    response = _download(client, source, "app.log")
    assert response.status_code == 200
    assert_no_secret(response.content)
    assert response.content == _redacted(content)
    assert response.content.endswith(b"password=[redacted: password]")
    assert response.headers["x-perchtail-redacted"] == "1"
    # Content of a log: never kept by the browser or a proxy.
    assert response.headers["cache-control"] == "no-store"
    assert _disposition_name(response) == "app.redacted.log"
    event = _audit(session, "file.download")
    assert event.event_metadata["path"] == "app.log"
    assert event.event_metadata["redaction"] == "applied"
    assert event.event_metadata["redaction_at"] == "server"
    assert "skipped_count" not in event.event_metadata
    assert _scratch_files(store) == []
    # The file on the source is never changed.
    assert (folder / "app.log").read_bytes() == content


def test_download_without_the_suffix_keeps_the_name(session, downloads):
    _, client, source, folder = downloads
    system_settings.set_bool(session, "redaction_download_suffix", False)
    session.commit()
    (folder / "app.log").write_text(SAMPLE)

    response = _download(client, source, "app.log")
    assert_no_secret(response.content)
    assert response.headers["x-perchtail-redacted"] == "1"
    assert _disposition_name(response) == "app.log"


@pytest.mark.parametrize("setting", ["redaction_downloads", "redaction_enabled"])
def test_download_is_the_original_when_downloads_or_redaction_are_off(session, downloads, setting):
    _, client, source, folder = downloads
    system_settings.set_bool(session, setting, False)
    session.commit()
    (folder / "app.log").write_text(SAMPLE)
    (folder / "sub").mkdir()
    (folder / "sub" / "x.log").write_text(SAMPLE)

    response = _download(client, source, "app.log")
    assert response.content == SAMPLE_BYTES
    assert "x-perchtail-redacted" not in response.headers
    assert _disposition_name(response) == "app.log"
    assert "redaction" not in _audit(session, "file.download").event_metadata
    folder_zip = _download_zip(client, source, "sub")
    assert "x-perchtail-redacted" not in folder_zip.headers
    with zipfile.ZipFile(io.BytesIO(folder_zip.content)) as zf:
        assert zf.read("x.log") == SAMPLE_BYTES
    assert "redaction" not in _audit(session, "folder.download").event_metadata


def test_download_over_a_remote_source_keeps_nothing_in_scratch(
    session, downloads, store, monkeypatch
):
    _, client, _, _ = downloads
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

    response = _download(client, remote, "app.log")
    assert response.status_code == 200
    assert_no_secret(response.content)
    assert response.content == _redacted(SAMPLE_BYTES)
    assert _scratch_files(store) == []


def test_download_of_a_gz_file_is_decompressed_and_redacted(downloads):
    _, client, source, folder = downloads
    (folder / "app.log.gz").write_bytes(gzip.compress(SAMPLE_BYTES))

    response = _download(client, source, "app.log.gz")
    assert response.status_code == 200
    assert response.content == _redacted(SAMPLE_BYTES)
    assert _disposition_name(response) == "app.redacted.log"


def test_download_of_an_archive_member_is_redacted(downloads):
    _, client, source, folder = downloads
    (folder / "bundle.zip").write_bytes(_zip_bytes({"inner/app.log": SAMPLE_BYTES}))

    response = _download(client, source, "bundle.zip", member="inner/app.log")
    assert response.status_code == 200
    assert response.content == _redacted(SAMPLE_BYTES)
    assert _disposition_name(response) == "app.redacted.log"


def test_download_of_a_gz_member_is_recompressed(session, downloads):
    _, client, source, folder = downloads
    (folder / "bundle.zip").write_bytes(_zip_bytes({"app.log.gz": gzip.compress(SAMPLE_BYTES)}))

    response = _download(client, source, "bundle.zip", member="app.log.gz")
    assert response.status_code == 200
    assert gzip.decompress(response.content) == _redacted(SAMPLE_BYTES)
    assert _disposition_name(response) == "app.redacted.log.gz"
    assert "skipped_count" not in _audit(session, "file.download").event_metadata


def test_a_corrupt_gz_member_is_refused_and_audited(session, downloads):
    _, client, source, folder = downloads
    (folder / "bundle.zip").write_bytes(_zip_bytes({"app.log.gz": b"not gzip " + SAMPLE_BYTES}))

    response = _download(client, source, "bundle.zip", member="app.log.gz")
    assert response.status_code == 409
    assert_no_secret(response.text)
    event = _audit(session, "file.download_denied")
    assert event.event_metadata["reason"] == "redaction_unreadable"
    assert event.event_metadata["path"] == "bundle.zip/app.log.gz"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_download_of_utf16_stays_utf16(downloads, encoding):
    _, client, source, folder = downloads
    content = SAMPLE.encode(encoding)
    (folder / "app.log").write_bytes(content)

    response = _download(client, source, "app.log")
    assert response.status_code == 200
    # Decoded the way the file was written, BOM included.
    text = response.content.decode(encoding)
    assert_no_secret(text)
    assert "[redacted: email]" in text
    assert response.content[:2] == content[:2]


def test_download_of_utf32_is_refused_and_audited(session, downloads):
    _, client, source, folder = downloads
    (folder / "app.log").write_bytes(SAMPLE.encode("utf-32"))

    response = _download(client, source, "app.log")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "redaction_unsupported_encoding"
    event = _audit(session, "file.download_denied")
    assert event.event_metadata["reason"] == "redaction_unsupported_encoding"


@pytest.mark.parametrize("fmt", UNSUPPORTED_FORMATS)
def test_download_of_an_unsupported_compressed_format_is_refused_and_audited(
    session, downloads, store, fmt
):
    _, client, source, folder = downloads
    # Told by the content: a plain name, a matching extension, inside a .gz
    # file, an archive member and a .gz archive member.
    (folder / "app.log").write_bytes(compressed_sample(fmt))
    (folder / f"app.log.{fmt}").write_bytes(compressed_sample(fmt))
    (folder / "app.log.gz").write_bytes(gzip.compress(compressed_sample(fmt)))
    (folder / "bundle.zip").write_bytes(
        _zip_bytes(
            {"m.log": compressed_sample(fmt), "m.log.gz": gzip.compress(compressed_sample(fmt))}
        )
    )
    requests = [
        ("app.log", {}),
        (f"app.log.{fmt}", {}),
        ("app.log.gz", {}),
        ("bundle.zip", {"member": "m.log"}),
        ("bundle.zip", {"member": "m.log.gz"}),
    ]
    for path, params in requests:
        response = _download(client, source, path, **params)
        assert response.status_code == 409, (path, params)
        assert response.json()["detail"]["error_code"] == "redaction_unsupported_format"
        assert_no_secret(response.text)
    events = _audits(session, "file.download_denied")
    assert [e.event_metadata["reason"] for e in events] == ["redaction_unsupported_format"] * len(
        requests
    )
    assert [e.event_metadata["path"] for e in events] == [
        "app.log",
        f"app.log.{fmt}",
        "app.log.gz",
        "bundle.zip/m.log",
        "bundle.zip/m.log.gz",
    ]
    assert _audits(session, "file.download") == []
    assert _scratch_files(store) == []


@pytest.mark.parametrize("fmt", MISNAMED_FORMATS)
def test_download_of_gzip_or_zip_content_under_another_name_is_refused(
    session, downloads, store, fmt
):
    _, client, source, folder = downloads
    # Not named as a .gz file or an archive, so not decompressed: a plain
    # name, a rotated log, inside a .gz file, an archive member and a .gz
    # archive member.
    (folder / "app.log").write_bytes(compressed_sample(fmt))
    (folder / "app.log.1").write_bytes(compressed_sample(fmt))
    (folder / "app.log.gz").write_bytes(gzip.compress(compressed_sample(fmt)))
    (folder / "bundle.zip").write_bytes(
        _zip_bytes(
            {"m.log.1": compressed_sample(fmt), "m.log.gz": gzip.compress(compressed_sample(fmt))}
        )
    )
    requests = [
        ("app.log", {}),
        ("app.log.1", {}),
        ("app.log.gz", {}),
        ("bundle.zip", {"member": "m.log.1"}),
        ("bundle.zip", {"member": "m.log.gz"}),
    ]
    for path, params in requests:
        response = _download(client, source, path, **params)
        assert response.status_code == 409, (path, params)
        assert response.json()["detail"]["error_code"] == "redaction_unsupported_format"
        assert_no_secret(response.content)
    events = _audits(session, "file.download_denied")
    assert [e.event_metadata["reason"] for e in events] == ["redaction_unsupported_format"] * len(
        requests
    )
    assert _audits(session, "file.download") == []
    assert _scratch_files(store) == []


def test_without_redaction_gzip_content_under_another_name_downloads_as_before(session, downloads):
    _, client, source, folder = downloads
    system_settings.set_bool(session, "redaction_enabled", False)
    session.commit()
    content = compressed_sample("gzip")
    (folder / "app.log.1").write_bytes(content)

    assert _download(client, source, "app.log.1").content == content
    assert _audits(session, "file.download_denied") == []


def test_without_redaction_compressed_formats_download_as_before(session, downloads):
    _, client, source, folder = downloads
    system_settings.set_bool(session, "redaction_enabled", False)
    session.commit()
    content = compressed_sample("bz2")
    (folder / "app.log.bz2").write_bytes(content)

    response = _download(client, source, "app.log.bz2")
    assert response.content == content
    with zipfile.ZipFile(io.BytesIO(_download_zip(client, source).content)) as zf:
        assert zf.read("app.log.bz2") == content
    assert _audits(session, "file.download_denied") == []
    assert _audits(session, "file.download_redaction_skipped") == []


def test_a_whole_archive_leaves_out_unsupported_compressed_formats(session, downloads):
    _, client, source, folder = downloads
    (folder / "bundle.zip").write_bytes(
        _zip_bytes(
            {
                "app.log": SAMPLE_BYTES,
                **{f"x.{fmt}": compressed_sample(fmt) for fmt in UNSUPPORTED_FORMATS},
                "y.log.gz": gzip.compress(compressed_sample("xz")),
            }
        )
    )

    response = _download(client, source, "bundle.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == ["REDACTION-SKIPPED.txt", "app.log"]
        assert zf.read("app.log") == _redacted(SAMPLE_BYTES)
        skipped = zf.read("REDACTION-SKIPPED.txt").decode()
    for fmt in UNSUPPORTED_FORMATS:
        assert f"x.{fmt}: in a compressed format redaction doesn't support" in skipped
    assert "y.log.gz: in a compressed format redaction doesn't support" in skipped
    assert _audit(session, "file.download").event_metadata["skipped_count"] == 6


def test_a_whole_archive_leaves_out_gzip_or_zip_content_under_another_name(session, downloads):
    _, client, source, folder = downloads
    (folder / "bundle.tar.gz").write_bytes(
        _tar_gz_bytes(
            {
                "app.log": SAMPLE_BYTES,
                "app.log.1": compressed_sample("gzip"),
                "app.log.2": compressed_sample("zip"),
                "y.log.gz": gzip.compress(compressed_sample("gzip")),
            }
        )
    )

    response = _download(client, source, "bundle.tar.gz")
    assert response.status_code == 200
    assert_no_secret(gzip.decompress(response.content))
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["REDACTION-SKIPPED.txt", "app.log"]
        skipped = tf.extractfile("REDACTION-SKIPPED.txt").read().decode()
    for name in ("app.log.1", "app.log.2", "y.log.gz"):
        assert f"{name}: in a compressed format redaction doesn't support" in skipped
    assert _audit(session, "file.download").event_metadata["skipped_count"] == 3


def test_a_whole_zip_is_rebuilt_redacted(session, downloads, store):
    _, client, source, folder = downloads
    nested = _zip_bytes({"app.log": SAMPLE_BYTES})
    archive = _zip_bytes(
        {
            "app.log": SAMPLE_BYTES,
            "deep/other.log": SAMPLE_BYTES,
            "secret.log": b"hidden by the rules",
            "rotated.log.gz": gzip.compress(SAMPLE_BYTES),
            "wide.log": SAMPLE.encode("utf-16"),
            "wider.log": SAMPLE.encode("utf-32"),
            "nested.zip": nested,
            "locked.log": SAMPLE_BYTES,
        }
    )
    (folder / "bundle.zip").write_bytes(_mark_encrypted(archive, "locked.log"))

    response = _download(client, source, "bundle.zip")
    assert response.status_code == 200
    assert response.headers["x-perchtail-redacted"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert _disposition_name(response) == "bundle.redacted.zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = zf.namelist()
        assert sorted(names) == [
            "REDACTION-SKIPPED.txt",
            "app.log",
            "deep/other.log",
            "rotated.log.gz",
            "wide.log",
        ]
        assert zf.getinfo("app.log").date_time == WHEN
        assert zf.read("app.log") == _redacted(SAMPLE_BYTES)
        assert zf.read("deep/other.log") == _redacted(SAMPLE_BYTES)
        assert gzip.decompress(zf.read("rotated.log.gz")) == _redacted(SAMPLE_BYTES)
        wide = zf.read("wide.log")
        assert wide[:2] == b"\xff\xfe"
        assert_no_secret(wide.decode("utf-16"))
        skipped = zf.read("REDACTION-SKIPPED.txt").decode()
        for name in names:
            if name != "REDACTION-SKIPPED.txt":
                assert_no_secret(zf.read(name) if not name.endswith(".gz") else b"")
    assert "nested.zip:" in skipped and "locked.log:" in skipped and "wider.log:" in skipped
    # A member the rules hide leaves no trace, not even its name.
    assert "secret" not in skipped
    event = _audit(session, "file.download")
    assert event.event_metadata["redaction"] == "applied"
    assert event.event_metadata["skipped_count"] == 3
    assert _scratch_files(store) == []


def test_a_whole_tar_gz_is_rebuilt_redacted(downloads):
    _, client, source, folder = downloads
    (folder / "logs.tar.gz").write_bytes(
        _tar_gz_bytes(
            {"app.log": SAMPLE_BYTES, "secret.log": b"hidden", "sub/b.log": SAMPLE_BYTES},
            symlink="link.log",
        )
    )

    response = _download(client, source, "logs.tar.gz")
    assert response.status_code == 200
    assert _disposition_name(response) == "logs.redacted.tar.gz"
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tf:
        members = tf.getmembers()
        assert [m.name for m in members] == ["app.log", "sub/b.log"]
        assert all(m.isfile() and m.mtime == 1714558830 for m in members)
        for member in members:
            content = tf.extractfile(member).read()
            assert content == _redacted(SAMPLE_BYTES)


def test_an_archive_member_that_is_an_archive_is_rebuilt(downloads):
    _, client, source, folder = downloads
    inner = _zip_bytes({"app.log": SAMPLE_BYTES, "secret.log": b"hidden"})
    (folder / "bundle.zip").write_bytes(_zip_bytes({"nested.zip": inner}))

    response = _download(client, source, "bundle.zip", member="nested.zip")
    assert response.status_code == 200
    assert _disposition_name(response) == "nested.redacted.zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist() == ["app.log"]
        assert zf.read("app.log") == _redacted(SAMPLE_BYTES)


def test_an_unreadable_archive_is_refused_and_audited(session, downloads):
    _, client, source, folder = downloads
    (folder / "bundle.zip").write_bytes(b"PK\x03\x04 not really a zip " + SAMPLE_BYTES)

    response = _download(client, source, "bundle.zip")
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
    assert_no_secret(response.text)
    event = _audit(session, "file.download_denied")
    assert event.event_metadata["reason"] == "redaction_whole_archive"


def _zip_with_unreadable_and_link_members(path: Path) -> None:
    """A zip with a member compressed with Deflate64 (which zipfile can't
    read), a symbolic link and a plain member, all holding the sample."""
    zip_with_unsupported_method(
        path,
        "big.log",
        SAMPLE_BYTES,
        others=[(zip_link_info("current.log"), SAMPLE_BYTES), ("app.log", SAMPLE_BYTES)],
    )


def test_a_whole_zip_lists_unreadable_members_and_symbolic_links(session, downloads):
    _, client, source, folder = downloads
    _zip_with_unreadable_and_link_members(folder / "bundle.zip")

    response = _download(client, source, "bundle.zip")
    assert response.status_code == 200
    assert_no_secret(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == ["REDACTION-SKIPPED.txt", "app.log"]
        assert zf.read("app.log") == _redacted(SAMPLE_BYTES)
        assert all((i.external_attr >> 16) & 0o170000 != 0o120000 for i in zf.infolist())
        skipped = zf.read("REDACTION-SKIPPED.txt").decode()
    assert "big.log: compressed with a method that can't be read" in skipped
    assert "current.log: a symbolic link, which redaction doesn't follow" in skipped
    assert _audit(session, "file.download").event_metadata["skipped_count"] == 2


def test_a_folder_zip_lists_unreadable_members_and_symbolic_links(session, downloads, store):
    _, client, source, folder = downloads
    (folder / "app.log").write_text(SAMPLE)
    _zip_with_unreadable_and_link_members(folder / "bundle.zip")

    response = _download_zip(client, source)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == ["REDACTION-SKIPPED.txt", "app.log", "bundle.zip"]
        with zipfile.ZipFile(io.BytesIO(zf.read("bundle.zip"))) as inner:
            assert sorted(inner.namelist()) == ["REDACTION-SKIPPED.txt", "app.log"]
            assert_no_secret(inner.read("app.log"))
        skipped = zf.read("REDACTION-SKIPPED.txt").decode()
    assert "bundle.zip/big.log: compressed with a method that can't be read" in skipped
    assert "bundle.zip/current.log: a symbolic link, which redaction doesn't follow" in skipped
    assert _audit(session, "folder.download").event_metadata["skipped_count"] == 2
    assert _scratch_files(store) == []


def test_an_archive_zipfile_cant_read_is_refused_and_audited(session, downloads, monkeypatch):
    _, client, source, folder = downloads
    (folder / "bundle.zip").write_bytes(_zip_bytes({"app.log": SAMPLE_BYTES}))

    def unsupported(*args, **kwargs):
        raise NotImplementedError("compression type 9 (deflate64)")

    monkeypatch.setattr(archive_module, "rebuild_archive", unsupported)

    response = _download(client, source, "bundle.zip")
    assert response.status_code == 403
    assert response.json()["detail"]["error_code"] == "archive_not_downloadable_whole"
    event = _audit(session, "file.download_denied")
    assert event.event_metadata["reason"] == "redaction_whole_archive"


def test_downloads_are_redacted_for_users_who_arent_super_admins(session, downloads):
    app, client, source, folder = downloads
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
            capabilities=[Capability.view, Capability.download],
        )
    )
    reader = User(username="reader", role_id=role.id)
    session.add(reader)
    session.commit()
    app.dependency_overrides[get_current_active_user] = lambda: reader
    (folder / "app.log").write_text(SAMPLE)
    (folder / "bundle.zip").write_bytes(_zip_bytes({"app.log": SAMPLE_BYTES}))

    assert_no_secret(_download(client, source, "app.log").content)
    with zipfile.ZipFile(io.BytesIO(_download(client, source, "bundle.zip").content)) as zf:
        assert_no_secret(zf.read("app.log"))
    with zipfile.ZipFile(io.BytesIO(_download_zip(client, source).content)) as zf:
        assert_no_secret(zf.read("app.log"))


# -- /download-zip --------------------------------------------------------------


def test_a_folder_zip_is_redacted(session, downloads, store):
    _, client, source, folder = downloads
    (folder / "app.log").write_text(SAMPLE)
    (folder / "secret.log").write_text(SAMPLE)
    (folder / "sub").mkdir()
    (folder / "sub" / "rotated.log.gz").write_bytes(gzip.compress(SAMPLE_BYTES))
    (folder / "sub" / "wide.log").write_bytes(SAMPLE.encode("utf-16-le"))
    (folder / "sub" / "wider.log").write_bytes(SAMPLE.encode("utf-32"))
    (folder / "sub" / "bundle.zip").write_bytes(
        _zip_bytes(
            {
                "inner.log": SAMPLE_BYTES,
                "secret.log": b"hidden",
                "nested.tar.gz": _tar_gz_bytes({"x.log": SAMPLE_BYTES}),
            }
        )
    )
    (folder / "sub" / "logs.tar.gz").write_bytes(_tar_gz_bytes({"t.log": SAMPLE_BYTES}))

    response = _download_zip(client, source)
    assert response.status_code == 200
    assert response.headers["x-perchtail-redacted"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert _disposition_name(response) == "local.redacted.zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == [
            "REDACTION-SKIPPED.txt",
            "app.log",
            "sub/bundle.zip",
            "sub/logs.tar.gz",
            "sub/rotated.log.gz",
            "sub/wide.log",
        ]
        assert zf.read("app.log") == _redacted(SAMPLE_BYTES)
        # Kept compressed under the same name, and not compressed twice.
        assert zf.getinfo("sub/rotated.log.gz").compress_type == zipfile.ZIP_STORED
        assert gzip.decompress(zf.read("sub/rotated.log.gz")) == _redacted(SAMPLE_BYTES)
        assert_no_secret(zf.read("sub/wide.log").decode("utf-16-le"))
        with zipfile.ZipFile(io.BytesIO(zf.read("sub/bundle.zip"))) as inner:
            assert sorted(inner.namelist()) == ["REDACTION-SKIPPED.txt", "inner.log"]
            assert inner.read("inner.log") == _redacted(SAMPLE_BYTES)
        with tarfile.open(fileobj=io.BytesIO(zf.read("sub/logs.tar.gz")), mode="r:gz") as tf:
            assert tf.extractfile("t.log").read() == _redacted(SAMPLE_BYTES)
        skipped = zf.read("REDACTION-SKIPPED.txt").decode()
    assert "sub/bundle.zip/nested.tar.gz:" in skipped
    assert "sub/wider.log:" in skipped
    assert "secret" not in skipped
    event = _audit(session, "folder.download")
    assert event.event_metadata["redaction"] == "applied"
    assert event.event_metadata["redaction_at"] == "server"
    # Known before the ZIP starts: what the archives and .gz files leave out.
    assert event.event_metadata["skipped_count"] == 1
    # Found while the ZIP was sent: a second entry, once it ended.
    late = _audit(session, "file.download_redaction_skipped")
    assert late.target_type == "source" and late.target_id == source.id
    assert late.event_metadata["path"] == ""
    assert late.event_metadata["skipped_count"] == 1
    assert late.event_metadata["skipped"] == ["sub/wider.log"]
    assert late.id > event.id
    assert _scratch_files(store) == []


@pytest.mark.parametrize("fmt", UNSUPPORTED_FORMATS)
def test_a_folder_zip_leaves_out_unsupported_compressed_formats(session, downloads, store, fmt):
    _, client, source, folder = downloads
    (folder / "sub").mkdir()
    (folder / "sub" / "app.log").write_text(SAMPLE)
    (folder / "sub" / "plain.log").write_bytes(compressed_sample(fmt))
    (folder / "sub" / f"named.log.{fmt}").write_bytes(compressed_sample(fmt))
    (folder / "sub" / "inner.log.gz").write_bytes(gzip.compress(compressed_sample(fmt)))

    response = _download_zip(client, source, "sub")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == ["REDACTION-SKIPPED.txt", "app.log"]
        assert zf.read("app.log") == _redacted(SAMPLE_BYTES)
        skipped = zf.read("REDACTION-SKIPPED.txt").decode()
    for name in ("plain.log", f"named.log.{fmt}", "inner.log.gz"):
        assert f"{name}: in a compressed format redaction doesn't support" in skipped
    # The .gz file is prepared before the ZIP starts; the others are only
    # found to be left out while it is sent.
    assert _audit(session, "folder.download").event_metadata["skipped_count"] == 1
    late = _audit(session, "file.download_redaction_skipped")
    assert late.event_metadata["path"] == "sub"
    assert late.event_metadata["skipped_count"] == 2
    assert sorted(late.event_metadata["skipped"]) == sorted(
        ["sub/plain.log", f"sub/named.log.{fmt}"]
    )
    assert _scratch_files(store) == []


def test_a_folder_zip_leaves_out_gzip_or_zip_content_under_another_name(session, downloads, store):
    _, client, source, folder = downloads
    (folder / "sub").mkdir()
    (folder / "sub" / "app.log").write_text(SAMPLE)
    (folder / "sub" / "app.log.1").write_bytes(compressed_sample("gzip"))
    (folder / "sub" / "app.log.2").write_bytes(compressed_sample("zip"))
    (folder / "sub" / "inner.log.gz").write_bytes(gzip.compress(compressed_sample("gzip")))

    response = _download_zip(client, source, "sub")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == ["REDACTION-SKIPPED.txt", "app.log"]
        assert zf.read("app.log") == _redacted(SAMPLE_BYTES)
        skipped = zf.read("REDACTION-SKIPPED.txt").decode()
    for name in ("app.log.1", "app.log.2", "inner.log.gz"):
        assert f"{name}: in a compressed format redaction doesn't support" in skipped
    assert _audit(session, "folder.download").event_metadata["skipped_count"] == 1
    late = _audit(session, "file.download_redaction_skipped")
    assert sorted(late.event_metadata["skipped"]) == ["sub/app.log.1", "sub/app.log.2"]
    assert _scratch_files(store) == []


def test_a_folder_zip_names_its_list_apart_from_a_file_of_the_same_name(downloads):
    _, client, source, folder = downloads
    (folder / "REDACTION-SKIPPED.txt").write_text("mine")
    (folder / "wider.log").write_bytes(SAMPLE.encode("utf-32"))

    response = _download_zip(client, source)
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.read("REDACTION-SKIPPED.txt") == b"mine"
        assert "wider.log:" in zf.read("REDACTION-SKIPPED (PerchTail).txt").decode()


def test_a_folder_zip_without_suffix_keeps_its_name(session, downloads):
    _, client, source, folder = downloads
    system_settings.set_bool(session, "redaction_download_suffix", False)
    session.commit()
    (folder / "app.log").write_text(SAMPLE)

    response = _download_zip(client, source)
    assert _disposition_name(response) == "local.zip"
    assert response.headers["x-perchtail-redacted"] == "1"
    # Nothing left out while it was sent: no second entry.
    assert _audits(session, "file.download_redaction_skipped") == []
