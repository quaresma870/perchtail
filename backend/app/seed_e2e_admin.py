"""Seeds a deterministic super-admin user for the Playwright E2E suite (see
frontend/e2e/) so tests don't have to scrape a randomly generated password
out of application logs (app/bootstrap.py's seed_initial_super_admin does
that for real deployments, by design -- not test-friendly).

Run once against an isolated e2e database, before the app itself starts
(see backend/scripts/run_e2e_server.sh). Inserting this user first makes
seed_initial_super_admin's own bootstrap no-op the moment the app's lifespan
runs -- it already checks "does any user exist" -- so only one super-admin
account ever exists for the e2e run, and it's the one with a known password.

Refuses to run against a database that already has a user, so this can
never silently reset credentials in a real deployment if pointed at the
wrong DATABASE_URL by mistake.

Usage (from backend/, matching run_e2e_server.sh):

    E2E_ADMIN_USERNAME=... E2E_ADMIN_PASSWORD=... python -m app.seed_e2e_admin
"""

import os
import sys

from sqlmodel import Session, select

from app.auth.models import User
from app.auth.providers.local import hash_password
from app.auth.rbac import create_role

DEFAULT_USERNAME = "e2e-admin"
DEFAULT_PASSWORD = "e2e-test-password-123!"  # noqa: S105 -- test-only, isolated DB


def seed(username: str, password: str) -> int:
    # Deferred: lets tests patch app.db.engine, same as
    # app.rotate_credential_key.rotate/app.search_index.run_indexing_sweep do.
    from app.db import engine, init_db

    init_db()
    with Session(engine) as session:
        if session.exec(select(User)).first() is not None:
            print(
                "ERROR: a user already exists in this database -- refusing to seed "
                "over it. Point E2E runs at an isolated DATABASE_URL.",
                file=sys.stderr,
            )
            return 1

        role = create_role(
            session,
            actor_user_id=None,
            name="Super Admin",
            is_builtin=True,
            is_super_admin=True,
        )
        user = User(
            username=username,
            password_hash=hash_password(password),
            role_id=role.id,
        )
        session.add(user)
        session.commit()

    print(f"Seeded e2e super-admin user {username!r}.")
    return 0


def main() -> int:
    username = os.environ.get("E2E_ADMIN_USERNAME", DEFAULT_USERNAME)
    password = os.environ.get("E2E_ADMIN_PASSWORD", DEFAULT_PASSWORD)
    return seed(username, password)


if __name__ == "__main__":
    raise SystemExit(main())
