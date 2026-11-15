"""The first super admin's temporary password, handed over in a file.

A fresh install has no users, so app/bootstrap.py seeds the first super admin
with a random password. It used to be written to the log, where it stayed
for the log's whole retention and was readable from the built-in log viewer.
Now it goes to DATA_DIR/initial-admin-password, readable only by the
PerchTail user (mode 0600), and the log only says where to find it. The file
is deleted as soon as that account's password changes: its own forced
change at first sign-in, or a reset by another admin.
"""

import os
from pathlib import Path

from app.auth.models import User
from app.config import get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)

FILE_NAME = "initial-admin-password"


def initial_admin_password_path() -> Path:
    return Path(get_settings().data_dir) / FILE_NAME


def write_initial_admin_password(password: str) -> Path:
    """Writes the password to a new file only the owner can read. A file left
    by an earlier install is replaced, never appended to. Raises OSError if
    the file can't be written: starting without a way to sign in is worse
    than not starting."""
    path = initial_admin_password_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(password + "\n")
    # O_CREAT's mode is narrowed by the umask, never widened; this makes sure.
    os.chmod(path, 0o600)
    return path


def remove_initial_admin_password(user: User) -> None:
    """Deletes the file once the first super admin's password has changed.
    Doesn't touch it for any other account."""
    if user.username != get_settings().initial_admin_username:
        return
    path = initial_admin_password_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.error(
            "initial_super_admin.password_file_not_removed", path=str(path), error=str(exc)
        )
        return
    logger.info("initial_super_admin.password_file_removed", path=str(path))
