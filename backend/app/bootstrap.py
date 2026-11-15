from pathlib import Path

from sqlmodel import Session, select

from app import password_policy
from app.audit import record_audit_event
from app.auth.models import AuditLog, AuthProviderType, Role, SSOProtocol, SSOProviderConfig, User
from app.auth.providers.local import create_local_user
from app.auth.providers.oidc import read_provider_config
from app.auth.rbac import create_role
from app.config import get_settings
from app.initial_admin_password import write_initial_admin_password
from app.logging_config import get_logger
from app.models import PatternKind, Protocol, Rule, RuleType, SmtpSettings, Source
from app.redaction_rules import seed_builtin_redaction_rules
from app.severity_patterns import seed_default_global_patterns

logger = get_logger(__name__)

SYSTEM_SOURCE_NAME = "PerchTail application logs"
SUPER_ADMIN_ROLE_NAME = "Super Admin"
NO_ACCESS_ROLE_NAME = "No Access"


class CredentialSaltMissingError(RuntimeError):
    pass


def _has_salt_dependent_data(session: Session) -> bool:
    checks = (
        select(Source.id).where(Source.credential_ref.is_not(None)),
        select(SSOProviderConfig.id),
        select(User.id).where(User.mfa_secret_encrypted.is_not(None)),
        select(SmtpSettings.id).where(SmtpSettings.password_encrypted.is_not(None)),
        select(AuditLog.id).where(AuditLog.row_hash.is_not(None)),
    )
    return any(session.exec(query).first() is not None for query in checks)


def assert_credential_salt_present(session: Session) -> None:
    """Must run before anything derives a key (app/crypto.py creates a new
    salt on first use if the file is missing). A missing salt on a database
    that already holds encrypted data means the salt was lost -- typically
    a container recreated while the salt lived outside the persistent
    volume -- and silently generating a new one would make every stored
    credential, SSO secret and MFA secret undecryptable and every audit
    row fail verification. Refusing to start keeps the original salt
    recoverable."""
    settings = get_settings()
    salt_path = Path(settings.credential_salt_path)
    if salt_path.exists() or not _has_salt_dependent_data(session):
        return
    if settings.credential_salt_allow_regenerate:
        logger.critical("startup.credential_salt_regenerated", salt_path=str(salt_path))
        return
    logger.critical("startup.credential_salt_missing", salt_path=str(salt_path))
    raise CredentialSaltMissingError(
        f"CREDENTIAL_SALT_PATH ({salt_path}) is missing, but the database already holds "
        "data encrypted with a key derived from it. Starting now would generate a new "
        "salt and make every stored credential, SSO secret and MFA secret undecryptable. "
        "Restore the original salt file (see the CHANGELOG's upgrade note) and restart, or "
        "set CREDENTIAL_SALT_ALLOW_REGENERATE=true if it is permanently lost."
    )


def seed_system_log_source(session: Session) -> Source:
    """Seeds the built-in, super-admin-only log viewer on first startup —
    dogfooding the same live-browsing/viewer path as customer sources, via
    the `local` protocol pointed at LOG_DIR. Idempotent: on every startup
    after the first it only follows a changed LOG_DIR, moving the source's
    base path to it with a system audit entry. Access is gated purely by
    is_system + is_super_admin in auth/rbac.py's resolve_capability — no
    grant, however permissive, can reach it.

    Seeds one broad include rule (LOG_DIR is a dedicated directory that
    should only ever contain this app's own rotated logs, and the source
    is already super-admin-only) rather than leaving it with zero rules,
    which would make the viewer show nothing at all."""
    settings = get_settings()
    existing = session.exec(select(Source).where(Source.is_system.is_(True))).first()
    if existing is not None:
        if existing.base_path != settings.log_dir:
            old_path = existing.base_path
            existing.base_path = settings.log_dir
            session.add(existing)
            record_audit_event(
                session,
                user_id=None,
                action="source.update",
                target_type="source",
                target_id=existing.id,
                metadata={
                    "reason": "log_dir_changed",
                    "changes": {"base_path": {"from": old_path, "to": settings.log_dir}},
                },
            )
            session.commit()
            session.refresh(existing)
            logger.info(
                "system_source.log_dir_updated",
                source_id=existing.id,
                old=old_path,
                new=settings.log_dir,
            )
        return existing

    source = Source(
        name=SYSTEM_SOURCE_NAME,
        customer_id=None,
        folder_id=None,
        protocol=Protocol.local,
        host="localhost",
        base_path=settings.log_dir,
        is_system=True,
    )
    session.add(source)
    session.flush()

    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**/*",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    session.refresh(source)

    logger.info("system_source.seeded", source_id=source.id, log_dir=settings.log_dir)
    return source


def seed_initial_super_admin(session: Session) -> User | None:
    """A fresh deployment has zero users and no way to log in — CLAUDE.md's
    LocalPasswordProvider is "the break-glass super-admin account," but
    something has to create the first one. No-op once any user exists (this
    is strictly a first-run bootstrap, not something that should ever touch
    an existing deployment). Generates a random password rather than
    reading one from config — same reasoning as the admin-driven
    reset-password endpoint (api/users.py): a temporary credential forced
    to change beats a long-lived secret sitting in a .env file. The
    password goes to a 0600 file in DATA_DIR, never to the log
    (app/initial_admin_password.py); the WARNING line only says where it
    is."""
    if session.exec(select(User)).first() is not None:
        return None

    settings = get_settings()
    role = create_role(
        session,
        actor_user_id=None,
        name=SUPER_ADMIN_ROLE_NAME,
        is_builtin=True,
        is_super_admin=True,
    )
    temporary_password = password_policy.generate_password(
        password_policy.get_policy(session), user_identifiers=[settings.initial_admin_username]
    )
    # Written before the account exists: if the file can't be written, the
    # start fails and the next one tries again, instead of leaving an
    # account nobody can sign in to.
    password_file = write_initial_admin_password(temporary_password)
    user = create_local_user(
        session,
        actor_user_id=None,
        username=settings.initial_admin_username,
        password=temporary_password,
        role_id=role.id,
    )

    logger.warning(
        "initial_super_admin.seeded",
        username=user.username,
        password_file=str(password_file),
        note=(
            "The temporary password is in this file, readable only by the PerchTail user. "
            "Sign in and change it; the file is deleted when you do."
        ),
    )
    return user


def seed_no_access_role(session: Session) -> Role:
    """The role SSO auto-provisioning assigns a brand-new user per CLAUDE.md's
    Access control section ("SSO login auto-provisions a user with the
    no-access default role; an admin assigns the real role afterward") — zero
    global capabilities and (implicitly, since nothing ever grants it any)
    zero source access. Idempotent, like the other bootstrap seeds."""
    existing = session.exec(select(Role).where(Role.name == NO_ACCESS_ROLE_NAME)).first()
    if existing is not None:
        return existing

    return create_role(
        session,
        actor_user_id=None,
        name=NO_ACCESS_ROLE_NAME,
        is_builtin=True,
        is_super_admin=False,
    )


def backfill_sso_user_issuers(session: Session) -> int:
    """Binds SSO accounts created before accounts were keyed on (issuer,
    sub) to the issuer they came from. Only done when that issuer is
    unambiguous: the enabled OIDC provider, or the only one configured. If
    it isn't, the accounts are left unbound -- they no longer match any
    login, so their owners get a new account (or a username clash an admin
    resolves) instead of possibly someone else's. Returns the number bound."""
    unbound = session.exec(
        select(User).where(
            User.auth_provider == AuthProviderType.oidc, User.external_issuer.is_(None)
        )
    ).all()
    if not unbound:
        return 0

    configs = session.exec(
        select(SSOProviderConfig).where(SSOProviderConfig.protocol == SSOProtocol.oidc)
    ).all()
    enabled = [c for c in configs if c.enabled]
    candidates = enabled if enabled else configs
    if len(candidates) != 1:
        logger.warning(
            "sso.issuer_backfill_skipped",
            unbound_users=len(unbound),
            oidc_providers=len(configs),
            reason="no single OIDC provider to attribute these accounts to",
        )
        return 0

    raw_config = read_provider_config(candidates[0].config)
    if raw_config is None:
        logger.warning(
            "sso.issuer_backfill_skipped",
            unbound_users=len(unbound),
            reason="the SSO provider's settings can't be decrypted with the current key",
        )
        return 0
    issuer = raw_config["issuer"]
    for user in unbound:
        user.external_issuer = issuer
        session.add(user)
    session.commit()
    logger.info("sso.issuer_backfill_complete", users=len(unbound), issuer=issuer)
    return len(unbound)


def seed_severity_patterns(session: Session) -> None:
    """Thin wrapper kept alongside the other bootstrap seeds (called from
    main.py's lifespan) so every first-run seed lives in one place — the
    actual idempotent seed logic is in app.severity_patterns, next to the
    effective-set resolution it's paired with."""
    seed_default_global_patterns(session)


def seed_redaction_rules(session: Session) -> None:
    """The built-in redaction rules (#382, #249), disabled; the idempotent
    seed is in app.redaction_rules, next to the effective-rule resolution."""
    seed_builtin_redaction_rules(session)
