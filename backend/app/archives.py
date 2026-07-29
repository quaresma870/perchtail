import gzip
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

_TAR_SUFFIXES = (".tar.gz", ".tgz")


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    size: int
    is_dir: bool


def is_archive(filename: str) -> bool:
    """Zip/tar.gz are virtual folders in the tree (CLAUDE.md's "Live
    browsing & ephemeral fetch behavior") — expanding one lists its members
    rather than opening it directly."""
    return filename.endswith(".zip") or filename.endswith(_TAR_SUFFIXES)


def is_transparent_gzip(filename: str) -> bool:
    """A plain single-file .gz decompresses transparently and opens like any
    other file — unlike .zip/.tar.gz, it is not a virtual folder."""
    return filename.endswith(".gz") and not filename.endswith(_TAR_SUFFIXES)


def list_members(local_path: Path, filename: str) -> list[ArchiveMember]:
    if filename.endswith(".zip"):
        with zipfile.ZipFile(local_path) as zf:
            return [
                ArchiveMember(name=info.filename, size=info.file_size, is_dir=info.is_dir())
                for info in zf.infolist()
            ]
    if filename.endswith(_TAR_SUFFIXES):
        with tarfile.open(local_path, mode="r:gz") as tf:
            return [
                ArchiveMember(name=member.name, size=member.size, is_dir=member.isdir())
                for member in tf.getmembers()
            ]
    raise ValueError(f"Not a supported archive: {filename}")


def extract_member(local_path: Path, filename: str, member_name: str, destination: Path) -> None:
    """Decompresses and fetches just the requested member — same
    always-fresh, purge-on-close rules apply to it as to anything else."""
    if filename.endswith(".zip"):
        with (
            zipfile.ZipFile(local_path) as zf,
            zf.open(member_name) as src,
            open(destination, "wb") as dst,
        ):
            shutil.copyfileobj(src, dst)
        return
    if filename.endswith(_TAR_SUFFIXES):
        with tarfile.open(local_path, mode="r:gz") as tf:
            member = tf.getmember(member_name)
            src = tf.extractfile(member)
            if src is None:
                raise ValueError(f"{member_name} is not a regular file")
            with src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return
    raise ValueError(f"Not a supported archive: {filename}")


def decompress_gzip(local_path: Path, destination: Path) -> None:
    with gzip.open(local_path, "rb") as src, open(destination, "wb") as dst:
        shutil.copyfileobj(src, dst)
