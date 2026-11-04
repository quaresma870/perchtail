"""Moves the database, credential salt and SSH known_hosts from the pre-0.5.0
layout into DATA_DIR (#172).

Before 0.5.0 they sat directly in the data volume (`/data/perchtail.db`,
`/data/credential_salt`, `/data/ssh_known_hosts`) next to `logs/` and
`scratch/`. From 0.5.0 they live in DATA_DIR (`/data/db` in the image), the
directory to back up, so logs and scratch can go elsewhere.

On startup -- from the container entrypoint, from Alembic's env.py and from
the app's lifespan, whichever runs first -- each file that is still at the
legacy location (DATA_DIR's parent) and not yet in DATA_DIR is moved there.
Only paths left to default are moved: an explicit DATABASE_URL,
CREDENTIAL_SALT_PATH or SSH_KNOWN_HOSTS_PATH is used as configured.

The salt moves first and the database last, so an interrupted move can
never leave the database next to a missing salt: the next start finishes
it, and bootstrap.assert_credential_salt_present still refuses to start
without the salt when encrypted data exists. Each move is an atomic rename
within one filesystem, or a copy to a temporary name that is renamed into
place before the original is removed."""

import os
import shutil
import sys
from pathlib import Path

from app.config import Settings, get_settings

_SQLITE_COMPANIONS = ("-journal", "-wal", "-shm")


def _sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    path = url.removeprefix("sqlite:///")
    return Path(path) if path and path != ":memory:" else None


def _move(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, dest)
    except OSError:
        # Another filesystem: copy under a temporary name, make it durable,
        # then rename it into place before removing the original.
        tmp = dest.with_name(dest.name + ".moving")
        shutil.copy2(source, tmp)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
        source.unlink()


def legacy_moves(settings: Settings) -> list[tuple[Path, Path]]:
    """(from, to) for every legacy file that still has to move, in order."""
    data_dir = Path(settings.data_dir)
    legacy_dir = data_dir.parent
    if data_dir.resolve() == legacy_dir.resolve():
        return []
    moves: list[tuple[Path, Path]] = []
    for kind, name, target in (
        ("credential_salt", "credential_salt", Path(settings.credential_salt_path)),
        ("ssh_known_hosts", "ssh_known_hosts", Path(settings.ssh_known_hosts_path)),
    ):
        legacy = legacy_dir / name
        if kind in settings.derived_from_data_dir and legacy.is_file() and not target.exists():
            moves.append((legacy, target))

    db_target = _sqlite_path(settings.database_url)
    legacy_db = legacy_dir / "perchtail.db"
    if (
        "database" in settings.derived_from_data_dir
        and db_target is not None
        and legacy_db.is_file()
        and not db_target.exists()
    ):
        for suffix in _SQLITE_COMPANIONS:
            companion = legacy_db.with_name(legacy_db.name + suffix)
            if companion.is_file():
                moves.append((companion, db_target.with_name(db_target.name + suffix)))
        moves.append((legacy_db, db_target))
    return moves


def migrate_legacy_layout(settings: Settings | None = None) -> list[tuple[Path, Path]]:
    settings = settings or get_settings()
    moves = legacy_moves(settings)
    for source, dest in moves:
        _move(source, dest)
    return moves


def leftover_legacy_database(settings: Settings | None = None) -> Path | None:
    """A legacy database still sitting next to DATA_DIR while another one is
    in use there -- e.g. Alembic created an empty database in DATA_DIR
    before this move ran. Startup warns about it rather than guessing."""
    settings = settings or get_settings()
    if "database" not in settings.derived_from_data_dir:
        return None
    legacy = Path(settings.data_dir).parent / "perchtail.db"
    target = _sqlite_path(settings.database_url)
    if (
        target is not None
        and legacy.is_file()
        and target.exists()
        and legacy.resolve() != target.resolve()
    ):
        return legacy
    return None


def main() -> None:
    for source, dest in migrate_legacy_layout():
        print(f"perchtail: moved {source} -> {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
