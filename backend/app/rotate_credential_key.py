"""Rotates CREDENTIAL_ENCRYPTION_KEY without losing access to already-
encrypted data (Source.credential_ref, SSOProviderConfig.config) -- see
ROADMAP.md's security hardening backlog. Run with the app stopped: this
reads/writes credentials directly against the DB, and a live app process
would keep using the old key (cached via crypto.py's _fernet()) for any
request that touches credentials during the window, which could leave rows
re-encrypted under the old key after this script already moved on.

Usage (from backend/, with CREDENTIAL_ENCRYPTION_KEY still set to the
*current* value in the environment -- same as any normal app startup):

    NEW_CREDENTIAL_ENCRYPTION_KEY=<new-value> python -m app.rotate_credential_key
    python -m app.rotate_credential_key --dry-run   # preview only, no writes

If NEW_CREDENTIAL_ENCRYPTION_KEY isn't set, prompts for it interactively
(never accepted as a CLI argument -- that would land in shell history and
`ps` output). After a successful (non-dry-run) rotation, update
CREDENTIAL_ENCRYPTION_KEY in .env to the new value and restart the app --
the DB now only decrypts correctly under the new key.
"""

import argparse
import getpass
import os
import sys

from cryptography.fernet import InvalidToken
from sqlmodel import Session, select

from app.auth.models import SSOProviderConfig
from app.config import get_settings
from app.crypto import build_fernet
from app.models import Source


def _read_new_key() -> str:
    env_value = os.environ.get("NEW_CREDENTIAL_ENCRYPTION_KEY")
    if env_value:
        return env_value
    return getpass.getpass("New CREDENTIAL_ENCRYPTION_KEY: ")


def rotate(*, old_key: str, new_key: str, dry_run: bool) -> int:
    # Deferred: lets tests patch app.db.engine, same as
    # app.search_index.run_indexing_sweep/app.alerts.evaluate_alerts do.
    from app.db import engine

    old_fernet = build_fernet(old_key)
    new_fernet = build_fernet(new_key)

    with Session(engine) as session:
        sources = session.exec(select(Source).where(Source.credential_ref.is_not(None))).all()
        sso_configs = session.exec(select(SSOProviderConfig)).all()

        rotated = 0
        for source in sources:
            try:
                plaintext = old_fernet.decrypt(source.credential_ref.encode("utf-8"))
            except InvalidToken:
                print(
                    f"ERROR: source id={source.id!r} name={source.name!r} did not decrypt "
                    "under the old key -- wrong old key, or already rotated. Aborting "
                    "without writing anything.",
                    file=sys.stderr,
                )
                return 1
            if not dry_run:
                source.credential_ref = new_fernet.encrypt(plaintext).decode("utf-8")
                session.add(source)
            rotated += 1

        for config in sso_configs:
            try:
                plaintext = old_fernet.decrypt(config.config.encode("utf-8"))
            except InvalidToken:
                print(
                    f"ERROR: SSO provider id={config.id!r} name={config.name!r} did not "
                    "decrypt under the old key -- wrong old key, or already rotated. "
                    "Aborting without writing anything.",
                    file=sys.stderr,
                )
                return 1
            if not dry_run:
                config.config = new_fernet.encrypt(plaintext).decode("utf-8")
                session.add(config)
            rotated += 1

        if dry_run:
            print(
                f"Dry run: {len(sources)} source credential(s) and {len(sso_configs)} "
                f"SSO provider config(s) decrypted successfully under the old key "
                f"({rotated} total). No changes written."
            )
            return 0

        session.commit()
        print(
            f"Rotated {len(sources)} source credential(s) and {len(sso_configs)} SSO "
            f"provider config(s) ({rotated} total) to the new key.\n"
            "Next: set CREDENTIAL_ENCRYPTION_KEY to the new value in .env and restart "
            "the app -- the database now only decrypts correctly under the new key."
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Decrypt everything under the old key and report what would be rotated, "
        "without writing anything.",
    )
    args = parser.parse_args()

    old_key = get_settings().credential_encryption_key
    new_key = _read_new_key()

    if not new_key:
        print("ERROR: no new key provided.", file=sys.stderr)
        return 1
    if new_key == old_key:
        print(
            "ERROR: new key is the same as the current key -- nothing to rotate.",
            file=sys.stderr,
        )
        return 1

    return rotate(old_key=old_key, new_key=new_key, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
