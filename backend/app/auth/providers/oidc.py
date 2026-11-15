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

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import InvalidToken
from joserfc import jwt
from joserfc.jwk import KeySet
from sqlmodel import Session, select

from app import profile
from app.audit import record_audit_event
from app.auth.models import AuthProviderType, SSOGroupRoleMapping, User
from app.config import get_settings
from app.crypto import decrypt_secret, encrypt_secret
from app.timeutils import utcnow

STATE_TTL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 10
# The discovery document's endpoints PerchTail calls or sends browsers to.
DISCOVERY_ENDPOINTS = ("authorization_endpoint", "token_endpoint", "jwks_uri")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class InsecureIdPURLError(ValueError):
    """An issuer or IdP endpoint that isn't https://. The message names which
    one and says what's accepted; it never quotes a secret."""


def require_https(url: object, what: str) -> None:
    """Refuses anything but an https:// URL with a host, so the ID token, the
    client secret and the authorization code never travel in clear text.
    SSO_ALLOW_HTTP_LOCALHOST=true also accepts http:// on a loopback host,
    for local development only."""
    if not isinstance(url, str):
        raise InsecureIdPURLError(f"The {what} is missing")
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return
    if (
        parts.scheme == "http"
        and host in _LOOPBACK_HOSTS
        and get_settings().sso_allow_http_localhost
    ):
        return
    raise InsecureIdPURLError(f"The {what} must be an https:// URL")


class IssuerMismatchError(ValueError):
    """The discovery document names an issuer other than the configured one.
    The message quotes neither of them."""


def _same_issuer(configured: str, discovered: object) -> bool:
    """Exact comparison, ignoring only a trailing slash (OpenID Connect
    Discovery 1.0, section 4.3)."""
    return isinstance(discovered, str) and discovered.rstrip("/") == configured.rstrip("/")


@dataclass
class OIDCSettings:
    issuer: str
    client_id: str
    client_secret: str
    scopes: str = "openid email profile"
    # Name of the ID token claim carrying the user's IdP group memberships
    # (e.g. "groups", or "roles" for some IdPs) -- see
    # resolve_group_mapped_role_id. None/empty disables group-to-role
    # auto-mapping entirely, same as today's behavior.
    group_claim: str | None = None


def read_provider_config(ciphertext: str) -> dict | None:
    """The provider's decrypted settings, or None when they can't be
    decrypted with the current CREDENTIAL_ENCRYPTION_KEY and salt (the key
    or the salt was lost or changed outside `app.rotate_credential_key`).
    Callers treat None as "the stored secret is unreadable": the admin page
    still lists the provider so a new secret can be saved, and a login fails
    with the usual neutral error."""
    try:
        return json.loads(decrypt_secret(ciphertext))
    except InvalidToken:
        return None


def parse_oidc_settings(raw_config: dict) -> OIDCSettings:
    return OIDCSettings(
        issuer=raw_config["issuer"],
        client_id=raw_config["client_id"],
        client_secret=raw_config["client_secret"],
        scopes=raw_config.get("scopes", "openid email profile"),
        group_claim=raw_config.get("group_claim") or None,
    )


def extract_claim_groups(claims: dict, group_claim: str | None) -> list[str]:
    """Reads the configured group claim out of the ID token's claims and
    normalizes it to a list of group names -- IdPs vary on whether a
    single-group claim comes back as a bare string or a one-element list."""
    if not group_claim:
        return []
    value = claims.get(group_claim)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def resolve_group_mapped_role_id(session: Session, claim_groups: list[str]) -> int | None:
    """Returns the role_id the user's IdP groups map to, or None if none of
    the configured SSOGroupRoleMapping rows match -- callers decide what
    "no match" means (fall back to the no-access default for a brand-new
    user; leave the existing role untouched for a returning one). Mappings
    are evaluated in `order`, last match wins, same semantics as Rule's
    include/exclude precedence (see CLAUDE.md)."""
    if not claim_groups:
        return None
    claim_set = set(claim_groups)
    mappings = session.exec(select(SSOGroupRoleMapping).order_by(SSOGroupRoleMapping.order)).all()
    matched_role_id: int | None = None
    for mapping in mappings:
        if mapping.group_name in claim_set:
            matched_role_id = mapping.role_id
    return matched_role_id


def fetch_discovery_document(issuer: str) -> dict:
    """GET <issuer>/.well-known/openid-configuration. Raises on any HTTP or
    network failure — callers turn that into a user-facing detail —,
    InsecureIdPURLError when the issuer or any endpoint the document names
    (DISCOVERY_ENDPOINTS) isn't https://, and IssuerMismatchError when the
    document's `issuer` isn't the configured one: its endpoints and keys
    then speak for another identity provider."""
    require_https(issuer, "issuer URL")
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    response = httpx.get(url, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict):
        raise ValueError("The discovery document isn't a JSON object")
    if not _same_issuer(issuer, document.get("issuer")):
        raise IssuerMismatchError(
            "The identity provider's discovery document names a different issuer"
        )
    for name in DISCOVERY_ENDPOINTS:
        if name in document:
            require_https(document[name], f"identity provider's {name}")
    return document


def fetch_jwks(jwks_uri: str) -> dict:
    require_https(jwks_uri, "identity provider's jwks_uri")
    response = httpx.get(jwks_uri, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def generate_code_verifier() -> str:
    """A PKCE code verifier (RFC 7636): 64 random bytes, 86 URL-safe
    characters, inside the 43 to 128 the RFC allows."""
    return secrets.token_urlsafe(64)


def code_challenge_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def exchange_code_for_tokens(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    code_verifier: str,
) -> dict:
    require_https(token_endpoint, "identity provider's token_endpoint")
    response = httpx.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def verify_id_token(id_token: str, *, jwks: dict, issuer: str, client_id: str, nonce: str) -> dict:
    """Validates the ID token's signature against the IdP's published JWKS
    and its iss/exp/iat/aud/nonce claims; exp and iat must be present.
    Raises `ValueError` on any failure — an ID token's claims are never
    trusted without this."""
    key_set = KeySet.import_key_set(jwks)
    token = jwt.decode(id_token, key_set)

    claims_registry = jwt.JWTClaimsRegistry(
        iss={"essential": True, "value": issuer},
        exp={"essential": True},
        iat={"essential": True},
    )
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
    code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


def encode_state(nonce: str, code_verifier: str) -> str:
    """Self-contained, encrypted state param (Fernet, via app.crypto) instead
    of a server-side state table — Fernet tokens already embed a creation
    timestamp, which `decode_state` uses for expiry. Carries the nonce and
    the PKCE code verifier, which never reaches the browser in clear."""
    return encrypt_secret(json.dumps({"nonce": nonce, "code_verifier": code_verifier}))


def decode_state(state: str) -> tuple[str, str]:
    """Returns (nonce, code_verifier), or raises if `state` is malformed,
    tampered with, or older than STATE_TTL_SECONDS."""
    payload = json.loads(decrypt_secret(state, ttl=STATE_TTL_SECONDS))
    nonce, code_verifier = payload["nonce"], payload["code_verifier"]
    if not isinstance(nonce, str) or not isinstance(code_verifier, str):
        raise ValueError("malformed state")
    return nonce, code_verifier


def find_or_provision_user(
    session: Session,
    *,
    issuer: str,
    external_id: str,
    email: str,
    no_access_role_id: int,
    mapped_role_id: int | None = None,
) -> User:
    """Finds the existing SSO user by (auth_provider=oidc, issuer,
    external_id) -- `sub` is only unique per issuer, so a user from one IdP
    can never be matched to an account another IdP created -- or
    auto-provisions a new one per CLAUDE.md's Access control section --
    `mapped_role_id` (see resolve_group_mapped_role_id) if the IdP's group
    claim matched a configured SSOGroupRoleMapping, else the no-access
    default role. Does not attempt to link to an existing local account
    sharing the same email/username — that's a deliberate account-linking
    decision left out of v1. A username collision makes the insert fail;
    api/auth.py's callback handler rolls back, audits it as a failed SSO
    login and sends the browser back to the login page with the neutral SSO
    error."""
    user = session.exec(
        select(User).where(
            User.auth_provider == AuthProviderType.oidc,
            User.external_issuer == issuer,
            User.external_id == external_id,
        )
    ).first()
    if user is not None:
        return user

    user = User(
        username=email,
        password_hash=None,
        role_id=mapped_role_id if mapped_role_id is not None else no_access_role_id,
        auth_provider=AuthProviderType.oidc,
        external_issuer=issuer,
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
        metadata={"username": email, "issuer": issuer, "external_id": external_id},
    )
    session.commit()
    session.refresh(user)
    return user


class OIDCProvider:
    def __init__(self, settings: OIDCSettings):
        self.settings = settings

    def authorization_redirect_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_verifier: str
    ) -> str:
        discovery = fetch_discovery_document(self.settings.issuer)
        authorization_endpoint = discovery["authorization_endpoint"]
        require_https(authorization_endpoint, "identity provider's authorization_endpoint")
        return build_authorization_url(
            authorization_endpoint=authorization_endpoint,
            client_id=self.settings.client_id,
            redirect_uri=redirect_uri,
            scopes=self.settings.scopes,
            state=state,
            nonce=nonce,
            code_challenge=code_challenge_s256(code_verifier),
        )

    def complete_login(
        self,
        session: Session,
        *,
        code: str,
        redirect_uri: str,
        nonce: str,
        code_verifier: str,
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
            code_verifier=code_verifier,
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

        claim_groups = extract_claim_groups(claims, self.settings.group_claim)
        mapped_role_id = resolve_group_mapped_role_id(session, claim_groups)

        user = find_or_provision_user(
            session,
            # verify_id_token already checked `iss` equals the configured
            # issuer; the claim is what identifies the IdP that vouched.
            issuer=claims["iss"],
            external_id=external_id,
            email=email,
            no_access_role_id=no_access_role_id,
            mapped_role_id=mapped_role_id,
        )
        if not user.active:
            raise ValueError("account is deactivated")

        # Name and email follow the IdP (#243): read-only in My profile.
        profile.sync_from_claims(session, user, claims)

        # Re-synced on every login, not just first provisioning -- see
        # SSOGroupRoleMapping's docstring for why this can override a
        # manual role change made directly in PerchTail.
        if mapped_role_id is not None and user.role_id != mapped_role_id:
            user.role_id = mapped_role_id
            record_audit_event(
                session,
                user_id=user.id,
                action="user.sso_role_synced",
                target_type="user",
                target_id=user.id,
                metadata={"role_id": mapped_role_id, "groups": claim_groups},
            )

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
