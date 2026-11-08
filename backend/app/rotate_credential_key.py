"""Rotates CREDENTIAL_ENCRYPTION_KEY without losing access to anything
it protects (#326): source credentials (Source.credential_ref), SSO provider
settings (SSOProviderConfig.config), users' MFA secrets
(User.mfa_secret_encrypted), and the audit log's hash chain, whose HMAC key
is derived from the same secret. The chain is verified under the old key
first and the rotation stops if it's broken -- re-signing a tampered chain
would vouch for the tampering -- then every row is re-signed under the new
key and the rotation itself is recorded in the audit log. All of it in one
transaction: either everything moves to the new key or nothing does. Run with the app stopped: this
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

from app.audit_hash_chain import chain_new_entry, resign_chain
from app.audit_integrity import verify_chain
from app.auth.models import AuditLog, SSOProviderConfig, User
from app.config import get_settings
from app.crypto import build_audit_chain_key, build_fernet
from app.models import Source
from app.timeutils import utcnow


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

        mfa_users = session.exec(select(User).where(User.mfa_secret_encrypted.is_not(None))).all()
        for user in mfa_users:
            try:
                plaintext = old_fernet.decrypt(user.mfa_secret_encrypted.encode("utf-8"))
            except InvalidToken:
                print(
                    f"ERROR: the MFA secret of user id={user.id!r} did not decrypt under "
                    "the old key -- wrong old key, or already rotated. Aborting without "
                    "writing anything.",
                    file=sys.stderr,
                )
                return 1
            if not dry_run:
                user.mfa_secret_encrypted = new_fernet.encrypt(plaintext).decode("utf-8")
                session.add(user)
            rotated += 1

        # The audit chain: intact under the old key, or no rotation.
        chain = verify_chain(build_audit_chain_key(old_key), session)
        if not chain.ok:
            session.rollback()
            print(
                f"ERROR: the audit log's hash chain doesn't verify under the old key "
                f"(first problem at row {chain.broken_row_id}). Re-signing it would vouch "
                "for whatever broke it, so nothing was rotated. Check the audit log's "
                "integrity page and resolve it first.",
                file=sys.stderr,
            )
            return 1

        counts = (
            f"{len(sources)} source credential(s), {len(sso_configs)} SSO provider "
            f"config(s), {len(mfa_users)} MFA secret(s) and {chain.checked_count} audit "
            "log row(s)"
        )
        if dry_run:
            session.rollback()
            print(
                f"Dry run: {counts} checked under the old key; the audit chain is "
                "intact. No changes written."
            )
            return 0

        new_chain_key = build_audit_chain_key(new_key)
        resigned = resign_chain(new_chain_key, session)
        entry = AuditLog(
            user_id=None,
            action="credential_key.rotate",
            timestamp=utcnow(),
            event_metadata={
                "sources": len(sources),
                "sso_configs": len(sso_configs),
                "mfa_secrets": len(mfa_users),
                "audit_rows_resigned": resigned,
            },
        )
        chain_new_entry(new_chain_key, session, entry)
        session.add(entry)
        session.flush()
        if not verify_chain(new_chain_key, session).ok:
            session.rollback()
            print(
                "ERROR: the re-signed audit chain didn't verify under the new key; "
                "nothing was rotated.",
                file=sys.stderr,
            )
            return 1
        session.commit()
        print(
            f"Rotated {counts} to the new key.\n"
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
