import gzip
import io
import tarfile
import zipfile

import pytest
from app.archives import (
    SKIP_LINK,
    SKIP_NESTED_ARCHIVE,
    SKIP_UNSUPPORTED_COMPRESSION,
    DecompressionLimitError,
    decompress_gzip,
    extract_member,
    is_archive,
    is_transparent_gzip,
    list_members,
    rebuild_archive,
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


def _special(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info


def _tar_with_special_members(path):
    with tarfile.open(path, "w:gz") as tf:
        data = b"hidden content"
        info = tarfile.TarInfo("secret.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        directory = tarfile.TarInfo("logs")
        directory.type = tarfile.DIRTYPE
        tf.addfile(directory)
        tf.addfile(_special("logs/sym.log", tarfile.SYMTYPE, "../secret.txt"))
        tf.addfile(_special("hard.log", tarfile.LNKTYPE, "secret.txt"))
        tf.addfile(_special("dev", tarfile.CHRTYPE))
        tf.addfile(_special("blk", tarfile.BLKTYPE))
        tf.addfile(_special("fifo", tarfile.FIFOTYPE))


def test_list_members_tar_keeps_only_files_and_directories(tmp_path):
    archive_path = tmp_path / "logs.tar.gz"
    _tar_with_special_members(archive_path)

    members = {m.name: m.is_dir for m in list_members(archive_path, "logs.tar.gz")}
    assert members == {"secret.txt": False, "logs": True}


@pytest.mark.parametrize("member", ["logs/sym.log", "hard.log", "dev", "blk", "fifo", "logs"])
def test_extract_member_tar_refuses_anything_but_a_regular_file(tmp_path, member):
    archive_path = tmp_path / "logs.tar.gz"
    _tar_with_special_members(archive_path)
    destination = tmp_path / "out"

    with pytest.raises(KeyError):
        extract_member(archive_path, "logs.tar.gz", member, destination)
    assert not destination.exists()


def test_extract_member_missing_is_a_key_error(tmp_path):
    tar_path = tmp_path / "logs.tar.gz"
    _tar_with_special_members(tar_path)
    zip_path = tmp_path / "logs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("app.log", "x")

    for path in (tar_path, zip_path):
        with pytest.raises(KeyError):
            extract_member(path, path.name, "missing.log", tmp_path / "out")


# -- rebuild_archive --------------------------------------------------------------


def _upper(name, src, out):
    """A transform that upper-cases a member, and turns down `refuse.log`."""
    if name.endswith("refuse.log"):
        return "refused"
    out.write_bytes(src.read().upper())
    return None


def _skipped_list(skipped, names):
    return "SKIPPED-LIST.txt", "".join(f"{n}: {r}\n" for n, r in skipped).encode()


def _rebuild(source, filename, tmp_path, keep=lambda name: name != "hidden.log"):
    destination = tmp_path / f"rebuilt-{filename}"
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    skipped = rebuild_archive(
        source,
        filename,
        destination,
        work,
        keep=keep,
        transform=_upper,
        skipped_list=_skipped_list,
    )
    assert list(work.iterdir()) == []
    return destination, skipped


def test_rebuild_zip_keeps_names_and_times_and_lists_what_it_leaves_out(tmp_path):
    source = tmp_path / "logs.zip"
    when = (2024, 5, 1, 10, 20, 30)
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(zipfile.ZipInfo("dir/", date_time=when), b"")
        for name in ("dir/a.log", "../evil.log", "hidden.log", "refuse.log", "inner.tgz"):
            zf.writestr(zipfile.ZipInfo(name, date_time=when), b"content of " + name.encode())

    destination, skipped = _rebuild(source, "logs.zip", tmp_path)

    assert skipped == [("refuse.log", "refused"), ("inner.tgz", SKIP_NESTED_ARCHIVE)]
    with zipfile.ZipFile(destination) as zf:
        assert zf.namelist() == ["dir/a.log", "evil.log", "SKIPPED-LIST.txt"]
        assert zf.read("dir/a.log") == b"CONTENT OF DIR/A.LOG"
        assert zf.getinfo("dir/a.log").date_time == when
        assert b"hidden" not in zf.read("SKIPPED-LIST.txt")


def zip_with_unsupported_method(path, name, content, others=()):
    """A zip whose first member, `name`, says it is compressed with Deflate64
    (method 9), which zipfile can't read; `others` are ordinary members."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, content)
        for other_name, other_content in others:
            zf.writestr(other_name, other_content)
    data = bytearray(path.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    data[local + 8 : local + 10] = (9).to_bytes(2, "little")
    data[central + 10 : central + 12] = (9).to_bytes(2, "little")
    path.write_bytes(bytes(data))


def zip_link_info(name):
    """A zip member that is a symbolic link (S_IFLNK in its attributes)."""
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    return info


def test_rebuild_zip_lists_a_member_with_an_unsupported_compression_method(tmp_path):
    source = tmp_path / "logs.zip"
    zip_with_unsupported_method(source, "big.log", b"secret", others=[("a.log", b"fine")])

    destination, skipped = _rebuild(source, "logs.zip", tmp_path)

    assert skipped == [("big.log", SKIP_UNSUPPORTED_COMPRESSION)]
    with zipfile.ZipFile(destination) as zf:
        assert zf.namelist() == ["a.log", "SKIPPED-LIST.txt"]
        assert zf.read("a.log") == b"FINE"


def test_rebuild_zip_leaves_out_and_lists_symbolic_links(tmp_path):
    source = tmp_path / "logs.zip"
    with zipfile.ZipFile(source, "w") as zf:
        zf.writestr(zip_link_info("current.log"), b"hidden.log")
        zf.writestr("a.log", b"fine")

    destination, skipped = _rebuild(source, "logs.zip", tmp_path)

    assert skipped == [("current.log", SKIP_LINK)]
    with zipfile.ZipFile(destination) as zf:
        assert zf.namelist() == ["a.log", "SKIPPED-LIST.txt"]
        assert all((info.external_attr >> 16) & 0o170000 != 0o120000 for info in zf.infolist())


def test_rebuild_tar_gz_drops_links_devices_and_folders(tmp_path):
    source = tmp_path / "logs.tar.gz"
    _tar_with_special_members(source)

    destination, skipped = _rebuild(source, "logs.tar.gz", tmp_path, keep=lambda name: True)

    assert skipped == []
    with tarfile.open(destination, "r:gz") as tf:
        assert tf.getnames() == ["secret.txt"]
        assert tf.extractfile("secret.txt").read() == b"HIDDEN CONTENT"


def test_rebuild_leaves_nothing_behind_when_it_fails(tmp_path):
    source = tmp_path / "logs.zip"
    source.write_bytes(b"not a zip")

    with pytest.raises(zipfile.BadZipFile):
        _rebuild(source, "logs.zip", tmp_path)
    assert not (tmp_path / "rebuilt-logs.zip").exists()
    with pytest.raises(ValueError):
        _rebuild(source, "logs.rar", tmp_path)
