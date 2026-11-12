"""Encrypted online backups, their verification and restore (#334).

    python -m app.backup create [--reason manual|scheduled|pre-upgrade]
    python -m app.backup verify <file>
    python -m app.backup restore <file> [--force]
    python -m app.backup list

`create` runs while PerchTail runs. It copies the database with SQLite's
online backup API (one consistent snapshot, writers keep going), checks the
copy with `PRAGMA quick_check`, and packs it in a tar with the credential
salt, SSH known_hosts and a manifest (format, app version, Alembic
revision, UTC time, reason and the SHA-256 of every file). The tar is
always encrypted before it reaches the disk, and written under a temporary
name renamed into place at the end, readable by PerchTail's user only.

Archive format (`.ptbackup`, version 1):

- a header in clear: `PTBK`, the format version (1), which key was used
  (0: CREDENTIAL_ENCRYPTION_KEY, 1: BACKUP_ENCRYPTION_KEY), a random 16-byte
  salt and a random 8-byte nonce prefix;
- the tar, encrypted with AES-256-GCM in blocks of 1 MiB. The key is
  HKDF-SHA256 of the chosen secret with that salt and the info
  `perchtail-backup-v1`. Each block's nonce is the prefix followed by the
  block's index (4 bytes, big-endian); its associated data is the header,
  the index and a byte that is 1 on the last block only, so an archive that
  was truncated, had blocks removed or reordered, or a changed header fails
  to decrypt. The key never enters the archive or the logs.

`restore` needs PerchTail stopped (it takes the instance lock, app/
instance_lock.py), verifies the whole archive first, refuses a database at
an Alembic revision this version doesn't know, and checks that the current
CREDENTIAL_ENCRYPTION_KEY decrypts a credential stored in the backup
(`--force` skips only that check, never the archive's authentication).
Then it moves DATA_DIR aside to `<DATA_DIR>.before-restore-<time>` and puts
the backup's files in a fresh DATA_DIR. When it refuses, nothing changes.

Each action is audited (`backup.create`, `backup.verify`, `backup.restore`)
with the file name, the reason and the result. A restore replaces the
database it would be audited in, so it is logged, and the first instance
that starts on the restored database writes the audit entry.

Only SQLite for now: the database part goes through a small per-dialect
interface, which PostgreSQL will implement (#306)."""

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app import instance_lock
from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.version import APP_VERSION

logger = get_logger(__name__)

REASONS = ("manual", "scheduled", "pre-upgrade")
PRE_UPGRADE_KEEP = 3
SUFFIX = ".ptbackup"

MAGIC = b"PTBK"
FORMAT_VERSION = 1
MANIFEST_FORMAT = 1
KEY_CREDENTIAL = 0
KEY_BACKUP = 1
_KEY_NAMES = {KEY_CREDENTIAL: "CREDENTIAL_ENCRYPTION_KEY", KEY_BACKUP: "BACKUP_ENCRYPTION_KEY"}
HKDF_INFO = b"perchtail-backup-v1"
SALT_BYTES = 16
NONCE_PREFIX_BYTES = 8
HEADER_BYTES = len(MAGIC) + 1 + 1 + SALT_BYTES + NONCE_PREFIX_BYTES
BLOCK_BYTES = 1 << 20
TAG_BYTES = 16
_MAX_BLOCKS = 1 << 32

MANIFEST_NAME = "manifest.json"
SALT_NAME = "credential_salt"
KNOWN_HOSTS_NAME = "ssh_known_hosts"
# Left in a restored DATA_DIR for the first start to audit (record_pending_restore).
RESTORE_MARKER = ".restored.json"

ROOT_REFUSAL = "run it as the perchtail user (docker compose exec -u perchtail ...)"

_NAME = re.compile(
    r"^perchtail-(?P<version>.+)-(?P<stamp>\d{8}T\d{6}Z)-(?P<reason>manual|scheduled|pre-upgrade)"
    + re.escape(SUFFIX)
    + r"$"
)
_STAMP = "%Y%m%dT%H%M%SZ"

# One create at a time in this process (the scheduled job and nothing else
# share it; a CLI run is its own process with its own file name).
_create_lock = threading.Lock()


class BackupError(RuntimeError):
    """A backup that can't be made, verified or restored. The message says
    why, for the person running the command."""


# --- Database dialects -----------------------------------------------------


class DatabaseDialect(Protocol):
    """What a backup needs from the database. SQLite now; PostgreSQL (#306)
    will add its own."""

    member: str  # the database's name inside the archive

    def target(self) -> Path:
        """Where the database lives."""

    def snapshot(self, dest: Path) -> None:
        """A consistent copy of the live database, written to dest."""

    def check(self, path: Path) -> None:
        """Raises BackupError when the copy at path is damaged."""

    def revision(self, path: Path) -> str | None:
        """The Alembic revision of the copy at path."""

    def clear_companions(self, path: Path) -> None:
        """Removes files the database engine keeps next to path."""


class SqliteDialect:
    member = "perchtail.db"
    _companions = ("-wal", "-shm", "-journal")
    _busy_timeout = 30

    def __init__(self, path: Path):
        self._path = path

    def target(self) -> Path:
        return self._path

    def snapshot(self, dest: Path) -> None:
        if not self._path.is_file():
            raise BackupError(f"the database {self._path} doesn't exist")
        source = sqlite3.connect(self._path, timeout=self._busy_timeout)
        try:
            copy = sqlite3.connect(dest)
            try:
                # pages=-1: the whole database in one step, so the copy is one
                # consistent snapshot while writers keep going (WAL).
                source.backup(copy, pages=-1)
                # A self-contained file, without a -wal to go with it; the app
                # switches it back to WAL when it opens it.
                copy.execute("PRAGMA journal_mode=DELETE")
            finally:
                copy.close()
        finally:
            source.close()

    def check(self, path: Path) -> None:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = connection.execute("PRAGMA quick_check").fetchall()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"the database in the backup is damaged: {exc}") from None
        if [row[0] for row in rows] != ["ok"]:
            detail = "; ".join(str(row[0]) for row in rows[:3])
            raise BackupError(f"the database in the backup failed quick_check: {detail}")

    def revision(self, path: Path) -> str | None:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            connection.close()
        return row[0] if row else None

    def clear_companions(self, path: Path) -> None:
        for suffix in self._companions:
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def dialect_for(settings: Settings) -> DatabaseDialect:
    url = settings.database_url
    if url.startswith("sqlite:///"):
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            return SqliteDialect(Path(path))
    raise BackupError("backups support a SQLite database file only for now")


# --- Encryption ------------------------------------------------------------


def _secret(settings: Settings, key_kind: int) -> str:
    if key_kind == KEY_BACKUP:
        if not settings.backup_encryption_key:
            raise BackupError(
                "this backup was encrypted with BACKUP_ENCRYPTION_KEY, which isn't set; "
                "set it to the key the backup was made with"
            )
        return settings.backup_encryption_key
    if key_kind == KEY_CREDENTIAL:
        return settings.credential_encryption_key
    raise BackupError("the backup's header names an unknown key; the file was altered")


def _derive_key(secret: str, salt: bytes) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=HKDF_INFO)
    return hkdf.derive(secret.encode("utf-8"))


def _block_nonce(prefix: bytes, index: int) -> bytes:
    return prefix + index.to_bytes(4, "big")


def _block_aad(header: bytes, index: int, final: bool) -> bytes:
    return header + index.to_bytes(4, "big") + (b"\x01" if final else b"\x00")


class _Encryptor:
    """A write-only stream that encrypts what it's given in 1 MiB blocks.
    finish() writes the last block, which may be empty."""

    def __init__(self, out: BinaryIO, settings: Settings):
        self._out = out
        key_kind = KEY_BACKUP if settings.backup_encryption_key else KEY_CREDENTIAL
        salt = secrets.token_bytes(SALT_BYTES)
        self._prefix = secrets.token_bytes(NONCE_PREFIX_BYTES)
        self._header = MAGIC + bytes([FORMAT_VERSION, key_kind]) + salt + self._prefix
        self._aead = AESGCM(_derive_key(_secret(settings, key_kind), salt))
        self._index = 0
        self._buffer = bytearray()
        out.write(self._header)

    def write(self, data: bytes) -> int:
        self._buffer += data
        # Keep at least one byte back: only finish() knows the last block.
        while len(self._buffer) > BLOCK_BYTES:
            self._emit(bytes(self._buffer[:BLOCK_BYTES]), final=False)
            del self._buffer[:BLOCK_BYTES]
        return len(data)

    def finish(self) -> None:
        self._emit(bytes(self._buffer), final=True)
        self._buffer.clear()

    def _emit(self, block: bytes, *, final: bool) -> None:
        if self._index >= _MAX_BLOCKS:
            raise BackupError("the backup is too large for one archive")
        nonce = _block_nonce(self._prefix, self._index)
        aad = _block_aad(self._header, self._index, final)
        self._out.write(self._aead.encrypt(nonce, block, aad))
        self._index += 1


class _Decryptor:
    """A read-only stream of an archive's plaintext. Every block is
    authenticated before any of it is returned; the end of the file must
    come right after the block marked last."""

    def __init__(self, fh: BinaryIO, settings: Settings):
        self._fh = fh
        header = fh.read(HEADER_BYTES)
        if len(header) < HEADER_BYTES or not header.startswith(MAGIC):
            raise BackupError("not a PerchTail backup (no PTBK header)")
        version, key_kind = header[4], header[5]
        if version != FORMAT_VERSION:
            raise BackupError(f"unknown backup format version {version}")
        salt = header[6 : 6 + SALT_BYTES]
        self._prefix = header[6 + SALT_BYTES :]
        self._header = header
        self.key_name = _KEY_NAMES.get(key_kind, "an unknown key")
        self._aead = AESGCM(_derive_key(_secret(settings, key_kind), salt))
        self._index = 0
        self._done = False
        self._plain = bytearray()
        self._next = fh.read(BLOCK_BYTES + TAG_BYTES)
        if not self._next:
            raise BackupError("the backup is truncated: it has no content after the header")

    def _decrypt_next(self) -> None:
        current = self._next
        self._next = self._fh.read(BLOCK_BYTES + TAG_BYTES)
        final = not self._next
        try:
            plain = self._aead.decrypt(
                _block_nonce(self._prefix, self._index),
                current,
                _block_aad(self._header, self._index, final),
            )
        except (InvalidTag, ValueError):
            if self._index == 0:
                raise BackupError(
                    f"the backup doesn't decrypt with the current {self.key_name}: wrong key, "
                    "or the file was altered"
                ) from None
            raise BackupError(
                f"block {self._index} of the backup failed authentication: the file is "
                "truncated, damaged or was altered"
            ) from None
        self._index += 1
        self._plain += plain
        self._done = final

    def read(self, size: int = -1) -> bytes:
        while not self._done and (size < 0 or len(self._plain) < size):
            self._decrypt_next()
        if size < 0:
            size = len(self._plain)
        out = bytes(self._plain[:size])
        del self._plain[:size]
        return out

    def drain(self) -> None:
        """Authenticates whatever the tar reader didn't need (its padding),
        up to the end of the file."""
        while not self._done:
            self._decrypt_next()
        self._plain.clear()


# --- Names, listing and retention ------------------------------------------


@dataclass(frozen=True)
class BackupInfo:
    name: str
    created_at: datetime
    size_bytes: int
    reason: str
    version: str


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime(_STAMP)


def backup_name(reason: str, now: datetime) -> str:
    return f"perchtail-{APP_VERSION}-{_stamp(now)}-{reason}{SUFFIX}"


def parse_name(name: str) -> tuple[str, datetime, str] | None:
    """(version, created_at, reason) for a backup's file name, or None."""
    match = _NAME.match(name)
    if match is None:
        return None
    try:
        created = datetime.strptime(match["stamp"], _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return match["version"], created, match["reason"]


def list_backups(settings: Settings | None = None) -> list[BackupInfo]:
    """The backups in BACKUP_DIR, newest first."""
    settings = settings or get_settings()
    directory = Path(settings.backup_dir)
    found: list[BackupInfo] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        parsed = parse_name(path.name)
        if parsed is None or not path.is_file():
            continue
        version, created, reason = parsed
        try:
            size = path.stat().st_size
        except OSError:
            continue
        found.append(BackupInfo(path.name, created, size, reason, version))
    found.sort(key=lambda info: (info.created_at, info.name), reverse=True)
    return found


def apply_retention(settings: Settings | None = None) -> list[str]:
    """Keeps the last BACKUP_KEEP manual and scheduled backups and the last
    PRE_UPGRADE_KEEP pre-upgrade ones, deleting only the oldest of each
    kind. Returns the names deleted."""
    settings = settings or get_settings()
    groups: dict[str, list[BackupInfo]] = {"regular": [], "pre-upgrade": []}
    for info in list_backups(settings):
        groups["pre-upgrade" if info.reason == "pre-upgrade" else "regular"].append(info)
    keep = {"regular": max(settings.backup_keep, 1), "pre-upgrade": PRE_UPGRADE_KEEP}
    deleted: list[str] = []
    for kind, infos in groups.items():
        for info in infos[keep[kind] :]:  # newest first
            try:
                (Path(settings.backup_dir) / info.name).unlink()
                deleted.append(info.name)
            except FileNotFoundError:
                pass
    if deleted:
        logger.info("backup.retention_deleted", files=deleted)
    return deleted


# --- Audit -----------------------------------------------------------------


def _audit(action: str, *, file: str, reason: str | None, result: str, error: str | None = None):
    """Best effort: an audit write that fails (no database yet, say) is
    logged and never fails the backup itself. Never the key, never content."""
    metadata: dict[str, Any] = {"file": file, "reason": reason, "result": result}
    if error:
        metadata["error"] = error
    try:
        from sqlmodel import Session

        from app.audit import record_audit_event
        from app.db import engine  # deferred, so tests can patch app.db.engine

        with Session(engine) as session:
            record_audit_event(
                session, user_id=None, action=action, target_type="backup", metadata=metadata
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("backup.audit_not_saved", action=action, file=file, error=str(exc))


def record_pending_restore(session, settings: Settings | None = None) -> bool:
    """At startup: audits a restore that left its marker in DATA_DIR, then
    removes the marker. The caller commits."""
    from app.audit import record_audit_event

    settings = settings or get_settings()
    marker = Path(settings.data_dir) / RESTORE_MARKER
    if not marker.is_file():
        return False
    try:
        details = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    metadata = {key: details.get(key) for key in ("file", "reason", "result", "restored_at")}
    metadata["result"] = metadata.get("result") or "ok"
    record_audit_event(
        session, user_id=None, action="backup.restore", target_type="backup", metadata=metadata
    )
    marker.unlink(missing_ok=True)
    return True


# --- Create ----------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(BLOCK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path, 0o700)


def _open_private(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")


def _tar_member(tar: tarfile.TarFile, name: str, fh: BinaryIO, size: int, mtime: float) -> None:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = int(mtime)
    tar.addfile(info, fh)


def create_backup(reason: str = "manual", settings: Settings | None = None) -> Path:
    """Writes a new encrypted backup to BACKUP_DIR and returns its path."""
    if reason not in REASONS:
        raise BackupError(f"reason must be one of {', '.join(REASONS)}")
    settings = settings or get_settings()
    with _create_lock:
        try:
            path = _create(reason, settings)
        except Exception as exc:
            _audit("backup.create", file="", reason=reason, result="failed", error=str(exc))
            logger.error("backup.create_failed", reason=reason, error=str(exc))
            raise
    _audit("backup.create", file=path.name, reason=reason, result="ok")
    logger.info("backup.created", file=path.name, reason=reason, size=path.stat().st_size)
    apply_retention(settings)
    return path


def _create(reason: str, settings: Settings) -> Path:
    dialect = dialect_for(settings)
    directory = Path(settings.backup_dir)
    _private_dir(directory)
    now = datetime.now(UTC)
    final = directory / backup_name(reason, now)
    if final.exists():
        raise BackupError(f"{final.name} already exists; try again in a second")

    work = Path(tempfile.mkdtemp(prefix=".backup-", dir=directory))
    tmp_archive = directory / f".{final.name}.tmp"
    try:
        db_copy = work / dialect.member
        dialect.snapshot(db_copy)
        dialect.check(db_copy)

        files: list[tuple[str, Path]] = [(dialect.member, db_copy)]
        salt = Path(settings.credential_salt_path)
        if not salt.is_file():
            raise BackupError(f"the credential salt {salt} doesn't exist")
        files.append((SALT_NAME, salt))
        known_hosts = Path(settings.ssh_known_hosts_path)
        if known_hosts.is_file():
            files.append((KNOWN_HOSTS_NAME, known_hosts))

        # Read each file once into the work dir, so what's hashed is what's packed.
        staged: list[tuple[str, Path]] = []
        for name, path in files:
            if path != db_copy:
                copy = work / name
                shutil.copyfile(path, copy)
                path = copy
            staged.append((name, path))

        manifest = {
            "format": MANIFEST_FORMAT,
            "app_version": APP_VERSION,
            "alembic_revision": dialect.revision(db_copy),
            "created_at": now.isoformat(timespec="seconds"),
            "reason": reason,
            "files": {name: _sha256(path) for name, path in staged},
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

        with _open_private(tmp_archive) as out:
            encryptor = _Encryptor(out, settings)
            with tarfile.open(fileobj=encryptor, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                _tar_member(
                    tar, MANIFEST_NAME, io.BytesIO(manifest_bytes), len(manifest_bytes), time.time()
                )
                for name, path in staged:
                    with open(path, "rb") as fh:
                        _tar_member(tar, name, fh, path.stat().st_size, path.stat().st_mtime)
            encryptor.finish()
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_archive, final)
    finally:
        tmp_archive.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    return final


# --- Verify ----------------------------------------------------------------


@dataclass
class Opened:
    manifest: dict
    files: dict[str, Path]
    key_name: str


def _check_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise BackupError("the backup's manifest isn't valid JSON") from None
    if not isinstance(manifest, dict):
        raise BackupError("the backup's manifest isn't an object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise BackupError(f"unknown manifest format {manifest.get('format')!r}")
    files = manifest.get("files")
    if not isinstance(files, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) for name, digest in files.items()
    ):
        raise BackupError("the backup's manifest has no valid file list")
    return manifest


def _copy_hashed(reader: BinaryIO | None, target: Path) -> str:
    """Writes reader to a new private file at target; returns its SHA-256."""
    digest = hashlib.sha256()
    with _open_private(target) as out:
        while reader is not None and (chunk := reader.read(BLOCK_BYTES)):
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest()


def _open_archive(path: Path, settings: Settings, into: Path, dialect: DatabaseDialect) -> Opened:
    """Decrypts the archive at path into the directory `into`, checking the
    manifest, every file's SHA-256 and the database. Raises BackupError."""
    allowed = {dialect.member, SALT_NAME, KNOWN_HOSTS_NAME}
    try:
        fh = open(path, "rb")
    except OSError as exc:
        raise BackupError(f"can't read {path}: {exc.strerror}") from None
    with fh:
        decryptor = _Decryptor(fh, settings)
        manifest: dict | None = None
        extracted: dict[str, Path] = {}
        digests: dict[str, str] = {}
        try:
            with tarfile.open(fileobj=decryptor, mode="r|") as tar:
                for member in tar:
                    if manifest is None:
                        if member.name != MANIFEST_NAME or not member.isreg():
                            raise BackupError("the backup doesn't start with its manifest")
                        reader = tar.extractfile(member)
                        manifest = _check_manifest(reader.read() if reader else b"")
                        continue
                    if not member.isreg() or member.name not in allowed:
                        raise BackupError(f"unexpected entry {member.name!r} in the backup")
                    if member.name in extracted:
                        raise BackupError(f"{member.name!r} appears twice in the backup")
                    target = into / member.name
                    digests[member.name] = _copy_hashed(tar.extractfile(member), target)
                    extracted[member.name] = target
        except tarfile.TarError as exc:
            raise BackupError(f"the backup's content isn't a valid archive: {exc}") from None
        decryptor.drain()

    if manifest is None:
        raise BackupError("the backup has no manifest")
    expected = manifest["files"]
    if set(expected) != set(digests):
        raise BackupError("the backup's files don't match its manifest")
    for name, digest in digests.items():
        if expected[name] != digest:
            raise BackupError(f"{name} doesn't match its SHA-256 in the manifest")
    for required in (dialect.member, SALT_NAME):
        if required not in extracted:
            raise BackupError(f"the backup has no {required}")
    dialect.check(extracted[dialect.member])
    return Opened(manifest=manifest, files=extracted, key_name=decryptor.key_name)


def _reason_of(path: Path) -> str | None:
    parsed = parse_name(path.name)
    return parsed[2] if parsed else None


def verify_backup(path: Path | str, settings: Settings | None = None) -> dict:
    """Decrypts and checks the whole backup without restoring anything.
    Returns its manifest; raises BackupError."""
    settings = settings or get_settings()
    path = Path(path)
    try:
        dialect = dialect_for(settings)
        _private_dir(Path(settings.backup_dir))
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=settings.backup_dir) as work:
            opened = _open_archive(path, settings, Path(work), dialect)
    except BackupError as exc:
        _audit(
            "backup.verify",
            file=path.name,
            reason=_reason_of(path),
            result="failed",
            error=str(exc),
        )
        logger.warning("backup.verify_failed", file=path.name, error=str(exc))
        raise
    _audit("backup.verify", file=path.name, reason=opened.manifest.get("reason"), result="ok")
    logger.info("backup.verified", file=path.name)
    return opened.manifest


# --- Restore ---------------------------------------------------------------


def _first_encrypted_value(db: Path) -> str | None:
    """A Fernet token stored in the database (a source credential, an SSO
    provider's settings or an MFA secret), or None if it holds none."""
    from app.auth.models import SSOProviderConfig, User
    from app.models import Source

    queries = (
        (Source.__tablename__, "credential_ref"),
        (SSOProviderConfig.__tablename__, "config"),
        (User.__tablename__, "mfa_secret_encrypted"),
    )
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for table, column in queries:
            try:
                row = connection.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT 1'  # nosec B608 - names from the models
                ).fetchone()
            except sqlite3.OperationalError:
                continue
            if row and row[0]:
                return str(row[0])
    finally:
        connection.close()
    return None


def credential_key_matches(db: Path, salt: bytes, secret: str) -> bool | None:
    """Whether CREDENTIAL_ENCRYPTION_KEY `secret` with the backup's salt
    decrypts a credential stored in the backup's database; None when it
    holds nothing encrypted."""
    from app.crypto import fernet_key_for_salt

    token = _first_encrypted_value(db)
    if token is None:
        return None
    try:
        Fernet(fernet_key_for_salt(secret, salt)).decrypt(token.encode("utf-8"))
    except InvalidToken:
        return False
    return True


def _inside(path: Path, directory: Path) -> Path | None:
    """path relative to directory, or None when it's outside."""
    try:
        return path.resolve().relative_to(directory.resolve())
    except ValueError:
        return None


def _move_children(source: Path, dest: Path) -> None:
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        shutil.move(str(child), str(dest / child.name))


def _place_file(source: Path, target: Path) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.restoring")
    tmp.unlink(missing_ok=True)
    with open(source, "rb") as fh, _open_private(tmp) as out:
        shutil.copyfileobj(fh, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, target)


@dataclass
class RestoreResult:
    previous: Path
    manifest: dict
    key_check: bool | None
    forced: bool


def restore_backup(
    path: Path | str, force: bool = False, settings: Settings | None = None
) -> RestoreResult:
    """Restores the backup at path into DATA_DIR, with PerchTail stopped.
    The previous DATA_DIR is kept as `<DATA_DIR>.before-restore-<time>`.
    Raises BackupError, having changed nothing, when it refuses."""
    settings = settings or get_settings()
    path = Path(path)
    try:
        return _restore(path, force, settings)
    except BackupError as exc:
        # Logged only: a refused restore changes nothing, not even the
        # current database's audit log.
        logger.warning(
            "backup.restore",
            file=path.name,
            reason=_reason_of(path),
            result="refused",
            error=str(exc),
        )
        raise


def _restore(path: Path, force: bool, settings: Settings) -> RestoreResult:
    dialect = dialect_for(settings)
    data_dir = Path(settings.data_dir)
    try:
        lock = instance_lock.acquire(settings)
    except instance_lock.InstanceLockHeldError as exc:
        raise BackupError(
            f"PerchTail is running on this DATA_DIR ({exc.holder or exc.content}); stop it "
            "before restoring"
        ) from None
    staging_lock = None
    backup_dir = Path(settings.backup_dir)
    _private_dir(backup_dir)
    work = Path(tempfile.mkdtemp(prefix=".restore-", dir=backup_dir))
    stamp = _stamp(datetime.now(UTC))
    staging = data_dir.with_name(f"{data_dir.name}.restoring-{stamp}")
    try:
        opened = _open_archive(path, settings, work, dialect)
        manifest = opened.manifest

        from app.schema_version import is_known_revision

        revision = manifest.get("alembic_revision")
        if not is_known_revision(revision):
            raise BackupError(
                f"the backup's database is at revision {revision}, which PerchTail "
                f"{APP_VERSION} doesn't know (it was made by version "
                f"{manifest.get('app_version')}); restore it with that version or newer"
            )

        key_check = credential_key_matches(
            opened.files[dialect.member],
            opened.files[SALT_NAME].read_bytes(),
            settings.credential_encryption_key,
        )
        if key_check is False and not force:
            raise BackupError(
                "the current CREDENTIAL_ENCRYPTION_KEY doesn't decrypt the credentials in this "
                "backup: set the key it was made with (after a key rotation, the old key), or "
                "use --force to restore anyway and re-enter every credential"
            )
        if key_check is False:
            logger.warning("backup.restore_forced", file=path.name)

        # Lay out the new DATA_DIR next to the current one, then swap.
        targets = {
            dialect.member: dialect.target(),
            SALT_NAME: Path(settings.credential_salt_path),
            KNOWN_HOSTS_NAME: Path(settings.ssh_known_hosts_path),
        }
        if staging.exists():
            raise BackupError(f"{staging} already exists; try again in a second")
        _private_dir(staging)
        outside: list[tuple[Path, Path]] = []
        for name, extracted in opened.files.items():
            relative = _inside(targets[name], data_dir)
            if relative is None:
                outside.append((extracted, targets[name]))
                continue
            dest = staging / relative
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.move(str(extracted), str(dest))
        restored_at = datetime.now(UTC).isoformat(timespec="seconds")
        marker = {
            "file": path.name,
            "reason": manifest.get("reason"),
            "result": "forced" if key_check is False else "ok",
            "restored_at": restored_at,
        }
        (staging / RESTORE_MARKER).write_text(json.dumps(marker), encoding="utf-8")
        staging_lock = instance_lock.acquire(settings.model_copy(update={"data_dir": str(staging)}))

        previous = data_dir.with_name(f"{data_dir.name}.before-restore-{stamp}")
        if previous.exists():
            raise BackupError(f"{previous} already exists; try again in a second")
        # From here on the current data is being replaced.
        if data_dir.exists():
            try:
                os.rename(data_dir, previous)
            except OSError:
                # DATA_DIR is a mount point (its own volume): move what's in it.
                _move_children(data_dir, previous)
        else:
            previous.mkdir(mode=0o700)
        if data_dir.exists():
            _move_children(staging, data_dir)
            staging.rmdir()
        else:
            os.rename(staging, data_dir)
        for extracted, target in outside:
            if target.exists():
                os.replace(target, target.with_name(f"{target.name}.before-restore-{stamp}"))
            if target == dialect.target():
                for suffix in ("-wal", "-shm", "-journal"):
                    old = target.with_name(target.name + suffix)
                    if old.exists():
                        os.replace(old, old.with_name(f"{old.name}.before-restore-{stamp}"))
            _place_file(extracted, target)
        dialect.clear_companions(dialect.target())
    finally:
        if staging_lock is not None:
            staging_lock.release()
        lock.release()
        shutil.rmtree(work, ignore_errors=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    logger.info(
        "backup.restore",
        file=path.name,
        reason=manifest.get("reason"),
        result=marker["result"],
        previous=str(previous),
        alembic_revision=revision,
    )
    return RestoreResult(previous=previous, manifest=manifest, key_check=key_check, forced=force)


# --- Scheduled job ---------------------------------------------------------

SCHEDULE_TICK = timedelta(minutes=5)
RETRY_AFTER_FAILURE = timedelta(hours=1)

# When the last scheduled attempt failed, so a backup that keeps failing is
# tried at most every min(interval, RETRY_AFTER_FAILURE) rather than every
# tick. In memory only: a restart tries again straight away.
_last_failed_attempt: datetime | None = None


def run_scheduled_backup(
    settings: Settings | None = None, now: Callable[[], datetime] | None = None
) -> Path | None:
    """The job (every SCHEDULE_TICK): a `scheduled` backup once the
    interval set in Settings -> System has gone by since the last one. Off
    (None) with an interval of 0. After a failed attempt, the next one waits
    min(interval, RETRY_AFTER_FAILURE); a success clears that wait."""
    global _last_failed_attempt
    from sqlmodel import Session

    from app import system_settings
    from app.db import engine  # deferred, so tests can patch app.db.engine

    settings = settings or get_settings()
    with Session(engine) as session:
        hours = system_settings.get_int(session, "backup_interval_hours")
    if hours <= 0:
        return None
    interval = timedelta(hours=hours)
    current = (now or (lambda: datetime.now(UTC)))()
    failed = _last_failed_attempt
    if failed is not None and current - failed < min(interval, RETRY_AFTER_FAILURE):
        return None
    latest = next((b for b in list_backups(settings) if b.reason == "scheduled"), None)
    if latest is not None and current - latest.created_at < interval:
        return None
    try:
        path = create_backup("scheduled", settings)
    except Exception:
        _last_failed_attempt = current
        raise
    _last_failed_attempt = None
    return path


# --- CLI -------------------------------------------------------------------


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.backup",
        description="Encrypted online backups of PerchTail's database, credential salt and "
        "SSH known_hosts (docs/backup-and-restore.md).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="make a backup now (PerchTail may be running)")
    create.add_argument("--reason", choices=REASONS, default="manual")
    verify = commands.add_parser("verify", help="decrypt and check a backup, restoring nothing")
    verify.add_argument("file")
    restore = commands.add_parser("restore", help="restore a backup (PerchTail stopped)")
    restore.add_argument("file")
    restore.add_argument(
        "--force",
        action="store_true",
        help="restore even if the current CREDENTIAL_ENCRYPTION_KEY doesn't decrypt the "
        "backup's credentials (they then have to be re-entered)",
    )
    commands.add_parser("list", help="list the backups in BACKUP_DIR")
    args = parser.parse_args(argv)

    if _is_root():
        print(f"ERROR: refusing to run as root: {ROOT_REFUSAL}", file=sys.stderr)
        return 2

    from app.logging_config import configure_logging

    configure_logging()
    settings = get_settings()
    try:
        if args.command == "create":
            path = create_backup(args.reason, settings)
            print(f"Backup written: {path}")
        elif args.command == "verify":
            manifest = verify_backup(args.file, settings)
            print(
                f"OK: {Path(args.file).name} decrypts and checks out (PerchTail "
                f"{manifest.get('app_version')}, revision {manifest.get('alembic_revision')}, "
                f"{manifest.get('created_at')}, {manifest.get('reason')})."
            )
        elif args.command == "restore":
            result = restore_backup(args.file, force=args.force, settings=settings)
            print(f"Restored {Path(args.file).name}. The previous data is in {result.previous}.")
            if result.key_check is False:
                print(
                    "The current CREDENTIAL_ENCRYPTION_KEY doesn't decrypt the restored "
                    "credentials: re-enter them, or set the key they were encrypted with."
                )
            print("Start PerchTail again.")
        else:
            backups = list_backups(settings)
            if not backups:
                print(f"No backups in {settings.backup_dir}.")
            for info in backups:
                print(
                    f"{info.name}  {info.created_at.isoformat(timespec='seconds')}  "
                    f"{_human(info.size_bytes)}  {info.reason}"
                )
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
