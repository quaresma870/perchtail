import gzip
import io
import tarfile
import zipfile

import pytest
from app.archives import (
    DecompressionLimitError,
    decompress_gzip,
    extract_member,
    is_archive,
    is_transparent_gzip,
    list_members,
)


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("logs.zip", True),
        ("logs.tar.gz", True),
        ("logs.tgz", True),
        ("OLD.ZIP", True),
        ("inner.Zip", True),
        ("logs.TAR.GZ", True),
        ("logs.Tgz", True),
        ("app.log", False),
        ("app.log.gz", False),
        ("app.log.GZ", False),
    ],
)
def test_is_archive(filename, expected):
    assert is_archive(filename) is expected


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("app.log.gz", True),
        ("app.log", False),
        ("logs.tar.gz", False),
        ("logs.zip", False),
        ("logs.tgz", False),
        ("app.log.GZ", True),
        ("APP.LOG.Gz", True),
        ("logs.TAR.GZ", False),
        ("OLD.ZIP", False),
    ],
)
def test_is_transparent_gzip(filename, expected):
    assert is_transparent_gzip(filename) is expected


def test_upper_case_extensions_list_and_extract_members(tmp_path):
    zip_path = tmp_path / "OLD.ZIP"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("app.log", "from zip")
    tar_path = tmp_path / "OLD.TAR.GZ"
    data = b"from tar"
    with tarfile.open(tar_path, mode="w:gz") as tf:
        info = tarfile.TarInfo("app.log")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    for path, expected in ((zip_path, b"from zip"), (tar_path, b"from tar")):
        assert [m.name for m in list_members(path, path.name)] == ["app.log"]
        destination = tmp_path / f"{path.name}.out"
        extract_member(path, path.name, "app.log", destination)
        assert destination.read_bytes() == expected


def test_list_members_zip(tmp_path):
    archive_path = tmp_path / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("app.log", "hello from app.log")
        zf.writestr("nested/debug.log", "hello from debug.log")

    members = {m.name: m for m in list_members(archive_path, "logs.zip")}
    assert members["app.log"].size == len("hello from app.log")
    assert members["app.log"].is_dir is False
    assert "nested/debug.log" in members


def test_list_members_tar_gz(tmp_path):
    archive_path = tmp_path / "logs.tar.gz"
    inner = tmp_path / "app.log"
    inner.write_text("hello from app.log")
    with tarfile.open(archive_path, "w:gz") as tf:
        tf.add(inner, arcname="app.log")

    members = {m.name: m for m in list_members(archive_path, "logs.tar.gz")}
    assert members["app.log"].size == len("hello from app.log")
    assert members["app.log"].is_dir is False


def test_list_members_rejects_unsupported_filename(tmp_path):
    with pytest.raises(ValueError):
        list_members(tmp_path / "whatever", "app.log")


def test_extract_member_zip(tmp_path):
    archive_path = tmp_path / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("app.log", "hello from app.log")

    destination = tmp_path / "extracted.log"
    extract_member(archive_path, "logs.zip", "app.log", destination)
    assert destination.read_text() == "hello from app.log"


def test_extract_member_tar_gz(tmp_path):
    archive_path = tmp_path / "logs.tar.gz"
    inner = tmp_path / "app.log"
    inner.write_text("hello from app.log")
    with tarfile.open(archive_path, "w:gz") as tf:
        tf.add(inner, arcname="app.log")

    destination = tmp_path / "extracted.log"
    extract_member(archive_path, "logs.tar.gz", "app.log", destination)
    assert destination.read_text() == "hello from app.log"


def test_extract_member_rejects_unsupported_filename(tmp_path):
    with pytest.raises(ValueError):
        extract_member(tmp_path / "whatever", "app.log", "member", tmp_path / "out")


def test_decompress_gzip(tmp_path):
    archive_path = tmp_path / "app.log.gz"
    with gzip.open(archive_path, "wb") as f:
        f.write(b"hello from app.log")

    destination = tmp_path / "app.log"
    decompress_gzip(archive_path, destination)
    assert destination.read_bytes() == b"hello from app.log"


def test_decompress_gzip_stops_at_the_byte_limit(tmp_path):
    src = tmp_path / "bomb.log.gz"
    src.write_bytes(gzip.compress(b"A" * 100_000))
    dest = tmp_path / "out"

    with pytest.raises(DecompressionLimitError):
        decompress_gzip(src, dest, max_bytes=1000)
    assert not dest.exists()


def test_decompress_gzip_at_exactly_the_limit_is_fine(tmp_path):
    src = tmp_path / "ok.log.gz"
    src.write_bytes(gzip.compress(b"A" * 1000))
    dest = tmp_path / "out"

    decompress_gzip(src, dest, max_bytes=1000)
    assert dest.read_bytes() == b"A" * 1000


def test_extract_zip_member_stops_at_the_byte_limit(tmp_path):
    src = tmp_path / "bomb.zip"
    with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.log", b"A" * 100_000)
    dest = tmp_path / "out"

    with pytest.raises(DecompressionLimitError):
        extract_member(src, "bomb.zip", "big.log", dest, max_bytes=1000)
    assert not dest.exists()


def test_extract_tar_member_stops_at_the_byte_limit(tmp_path):
    payload = tmp_path / "big.log"
    payload.write_bytes(b"A" * 100_000)
    src = tmp_path / "bomb.tar.gz"
    with tarfile.open(src, "w:gz") as tf:
        tf.add(payload, arcname="big.log")
    dest = tmp_path / "out"

    with pytest.raises(DecompressionLimitError):
        extract_member(src, "bomb.tar.gz", "big.log", dest, max_bytes=1000)
    assert not dest.exists()
