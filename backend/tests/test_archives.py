import gzip
import tarfile
import zipfile

import pytest
from app.archives import (
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
        ("app.log", False),
        ("app.log.gz", False),
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
    ],
)
def test_is_transparent_gzip(filename, expected):
    assert is_transparent_gzip(filename) is expected


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
