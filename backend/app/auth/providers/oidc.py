"""OIDC SSO — the second `AuthProvider` behind CLAUDE.md's abstracted
provider interface, alongside `LocalPasswordProvider`. Unlike password auth
this is a multi-step, redirect-based flow, so it doesn't fit a single
`authenticate()` call — `OIDCProvider` instead exposes the two steps the
login/callback endpoints need.

JWKS parsing and ID-token signature verification go through `joserfc`
(authlib's own `authlib.jose` is deprecated in favor of it as of authlib
1.7). The authorization-code exchange itself is just a couple of HTTP calls,
kept as small, individually-mockable module-level functions rather than
routed through authlib's OAuth2Client, so tests can monkeypatch each step
directly instead of faking that client's internals — the same "mock the
client, don't hit a real remote" convention the SSH/SMB/WinRM connector
tests already use (see CONTRIBUTING.md)."""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet
from sqlmodel import Session, select

from app.audit import record_audit_event
from app.auth.models import AuthProviderType, User
from app.crypto import decrypt_secret, encrypt_secret
from app.timeutils import utcnow

STATE_TTL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 10


@dataclass
class OIDCSettings:
    issuer: str
    client_id: str
    client_secret: str
    scopes: str = "openid email profile"


def parse_oidc_settings(raw_config: dict) -> OIDCSettings:
    return OIDCSettings(
        issuer=raw_config["issuer"],
        client_id=raw_config["client_id"],
        client_secret=raw_config["client_secret"],
        scopes=raw_config.get("scopes", "openid email profile"),
    )


def fetch_discovery_document(issuer: str) -> dict:
    """GET <issuer>/.well-known/openid-configuration. Raises on any HTTP or
    network failure — callers turn that into a user-facing detail."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    response = httpx.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def fetch_jwks(jwks_uri: str) -> dict:
    response = httpx.get(jwks_uri, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def exchange_code_for_tokens(
    *, token_endpoint: str, code: str, redirect_uri: str, client_id: str, client_secret: str
) -> dict:
    response = httpx.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def verify_id_token(id_token: str, *, jwks: dict, issuer: str, client_id: str, nonce: str) -> dict:
    """Validates the ID token's signature against the IdP's published JWKS
    and its iss/exp/aud/nonce claims. Raises `ValueError` on any failure —
    an ID token's claims are never trusted without this."""
    key_set = KeySet.import_key_set(jwks)
    token = jwt.decode(id_token, key_set)

    claims_registry = jwt.JWTClaimsRegistry(iss={"essential": True, "value": issuer})
    try:
        claims_registry.validate(token.claims)
    except Exception as exc:  # noqa: BLE001 - re-raised as a uniform ValueError for callers
        raise ValueError(f"invalid ID token claims: {exc}") from exc

    aud = token.claims.get("aud")
    allowed_aud = aud if isinstance(aud, list) else [aud]
    if client_id not in allowed_aud:
        raise ValueError("unexpected audience in ID token")

    if token.claims.get("nonce") != nonce:
        raise ValueError("nonce mismatch")

    return token.claims


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
    nonce: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "nonce": nonce,
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


def encode_state(nonce: str) -> str:
    """Self-contained, encrypted state param (Fernet, via app.crypto) instead
    of a server-side state table — Fernet tokens already embed a creation
    timestamp, which `decode_state` uses for expiry."""
    return encrypt_secret(nonce)


def decode_state(state: str) -> str:
    """Returns the nonce, or raises if `state` is malformed, tampered with,
    or older than STATE_TTL_SECONDS."""
    return decrypt_secret(state, ttl=STATE_TTL_SECONDS)


def find_or_provision_user(
    session: Session, *, external_id: str, email: str, no_access_role_id: int
) -> User:
    """Finds the existing SSO user by (auth_provider=oidc, external_id), or
    auto-provisions a new one with the no-access default role per CLAUDE.md's
    Access control section. Does not attempt to link to an existing local
    account sharing the same email/username — that's a deliberate account-
    linking decision left out of v1; a username collision surfaces as a
    plain 409 to the caller instead (see api/auth.py's callback handler)."""
    user = session.exec(
        select(User).where(
            User.auth_provider == AuthProviderType.oidc, User.external_id == external_id
        )
    ).first()
    if user is not None:
        return user

    user = User(
        username=email,
        password_hash=None,
        role_id=no_access_role_id,
        auth_provider=AuthProviderType.oidc,
        external_id=external_id,
    )
    session.add(user)
    session.flush()

    record_audit_event(
        session,
        user_id=user.id,
        action="user.sso_provision",
        target_type="user",
        target_id=user.id,
        metadata={"username": email, "external_id": external_id},
    )
    session.commit()
    session.refresh(user)
    return user


class OIDCProvider:
    def __init__(self, settings: OIDCSettings):
        self.settings = settings

    def authorization_redirect_url(self, *, redirect_uri: str, state: str, nonce: str) -> str:
        discovery = fetch_discovery_document(self.settings.issuer)
        return build_authorization_url(
            authorization_endpoint=discovery["authorization_endpoint"],
            client_id=self.settings.client_id,
            redirect_uri=redirect_uri,
            scopes=self.settings.scopes,
            state=state,
            nonce=nonce,
        )

    def complete_login(
        self,
        session: Session,
        *,
        code: str,
        redirect_uri: str,
        nonce: str,
        no_access_role_id: int,
    ) -> User:
        """Exchanges the authorization code, verifies the ID token, and
        returns the signed-in (or freshly-provisioned) user with its
        last_login_at/audit trail updated and committed. Raises `ValueError`
        for any failure the caller should treat as "login didn't succeed"
        (bad/expired code, invalid token, deactivated account) — distinct
        from httpx's own exceptions, which mean the IdP itself was
        unreachable."""
        discovery = fetch_discovery_document(self.settings.issuer)
        tokens = exchange_code_for_tokens(
            token_endpoint=discovery["token_endpoint"],
            code=code,
            redirect_uri=redirect_uri,
            client_id=self.settings.client_id,
            client_secret=self.settings.client_secret,
        )
        id_token = tokens.get("id_token")
        if not id_token:
            raise ValueError("IdP response did not include an id_token")

        jwks = fetch_jwks(discovery["jwks_uri"])
        claims = verify_id_token(
            id_token,
            jwks=jwks,
            issuer=self.settings.issuer,
            client_id=self.settings.client_id,
            nonce=nonce,
        )

        external_id = claims.get("sub")
        if not external_id:
            raise ValueError("ID token is missing a sub claim")
        email = claims.get("email") or claims.get("preferred_username") or external_id

        user = find_or_provision_user(
            session, external_id=external_id, email=email, no_access_role_id=no_access_role_id
        )
        if not user.active:
            raise ValueError("account is deactivated")

        user.last_login_at = utcnow()
        session.add(user)
        record_audit_event(
            session,
            user_id=user.id,
            action="user.login",
            target_type="user",
            target_id=user.id,
            metadata={"via": "oidc"},
        )
        session.commit()
        session.refresh(user)
        return user
