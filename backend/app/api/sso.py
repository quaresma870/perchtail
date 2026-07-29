import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, SSOProtocol, SSOProviderConfig, User
from app.auth.providers.oidc import fetch_discovery_document, fetch_jwks, parse_oidc_settings
from app.auth.rbac import require_global_capability
from app.crypto import decrypt_secret, encrypt_secret
from app.db import get_session

router = APIRouter(prefix="/sso", tags=["sso"])

require_manage = require_global_capability(GlobalCapability.manage_sso, get_current_active_user)


class SSOProviderPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    protocol: SSOProtocol
    name: str
    enabled: bool
    issuer: str
    client_id: str
    scopes: str
    # client_secret is deliberately never returned once set.

    @classmethod
    def from_config(cls, config: SSOProviderConfig) -> "SSOProviderPublic":
        raw = json.loads(decrypt_secret(config.config))
        return cls(
            id=config.id,
            protocol=config.protocol,
            name=config.name,
            enabled=config.enabled,
            issuer=raw["issuer"],
            client_id=raw["client_id"],
            scopes=raw.get("scopes", "openid email profile"),
        )


class SSOProviderCreate(BaseModel):
    name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str = "openid email profile"
    enabled: bool = False


class SSOProviderUpdate(BaseModel):
    name: str | None = None
    issuer: str | None = None
    client_id: str | None = None
    # Only overwrites the stored secret if provided — omitting it keeps the
    # existing one, so editing the issuer/name doesn't force re-entering it.
    client_secret: str | None = None
    scopes: str | None = None
    enabled: bool | None = None


class ConnectionCheckResult(BaseModel):
    ok: bool
    detail: str


def _encrypt_config(*, issuer: str, client_id: str, client_secret: str, scopes: str) -> str:
    return encrypt_secret(
        json.dumps(
            {
                "issuer": issuer,
                "client_id": client_id,
                "client_secret": client_secret,
                "scopes": scopes,
            }
        )
    )


def _assert_single_enabled(session: Session, *, enabled: bool, exclude_id: int | None = None):
    # CLAUDE.md: "One OIDC/SAML provider configured at a time for v1" — the
    # login flow needs to unambiguously pick "the" active provider, so this
    # is enforced here rather than left as a documentation-only convention.
    if not enabled:
        return
    query = select(SSOProviderConfig).where(SSOProviderConfig.enabled.is_(True))
    if exclude_id is not None:
        query = query.where(SSOProviderConfig.id != exclude_id)
    if session.exec(query).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another SSO provider is already enabled — disable it first",
        )


@router.get("", response_model=list[SSOProviderPublic])
def list_providers(user: User = Depends(require_manage), session: Session = Depends(get_session)):
    configs = session.exec(select(SSOProviderConfig)).all()
    return [SSOProviderPublic.from_config(c) for c in configs]


@router.post("", response_model=SSOProviderPublic, status_code=status.HTTP_201_CREATED)
def create_provider(
    payload: SSOProviderCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    _assert_single_enabled(session, enabled=payload.enabled)

    config = SSOProviderConfig(
        protocol=SSOProtocol.oidc,
        name=payload.name,
        config=_encrypt_config(
            issuer=payload.issuer,
            client_id=payload.client_id,
            client_secret=payload.client_secret,
            scopes=payload.scopes,
        ),
        enabled=payload.enabled,
    )
    session.add(config)
    session.flush()

    record_audit_event(
        session,
        user_id=user.id,
        action="sso_provider.create",
        target_type="sso_provider",
        target_id=config.id,
        metadata={"name": payload.name, "enabled": payload.enabled},
    )
    session.commit()
    session.refresh(config)
    return SSOProviderPublic.from_config(config)


def _get_or_404(session: Session, provider_id: int) -> SSOProviderConfig:
    config = session.get(SSOProviderConfig, provider_id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SSO provider not found")
    return config


@router.get("/{provider_id}", response_model=SSOProviderPublic)
def get_provider(
    provider_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    return SSOProviderPublic.from_config(_get_or_404(session, provider_id))


@router.patch("/{provider_id}", response_model=SSOProviderPublic)
def update_provider(
    provider_id: int,
    payload: SSOProviderUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    config = _get_or_404(session, provider_id)
    raw = json.loads(decrypt_secret(config.config))

    if payload.enabled is not None:
        _assert_single_enabled(session, enabled=payload.enabled, exclude_id=provider_id)
        config.enabled = payload.enabled
    if payload.name is not None:
        config.name = payload.name
    if payload.issuer is not None:
        raw["issuer"] = payload.issuer
    if payload.client_id is not None:
        raw["client_id"] = payload.client_id
    if payload.client_secret is not None:
        raw["client_secret"] = payload.client_secret
    if payload.scopes is not None:
        raw["scopes"] = payload.scopes

    config.config = encrypt_secret(json.dumps(raw))
    session.add(config)

    record_audit_event(
        session,
        user_id=user.id,
        action="sso_provider.update",
        target_type="sso_provider",
        target_id=config.id,
    )
    session.commit()
    session.refresh(config)
    return SSOProviderPublic.from_config(config)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(
    provider_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    config = _get_or_404(session, provider_id)
    session.delete(config)

    record_audit_event(
        session,
        user_id=user.id,
        action="sso_provider.delete",
        target_type="sso_provider",
        target_id=provider_id,
    )
    session.commit()


@router.post("/{provider_id}/test", response_model=ConnectionCheckResult)
def test_provider(
    provider_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Mirrors sources.py's `/check` endpoint: fetches the issuer's discovery
    document and JWKS, the same two requests a real login would make, without
    running the actual auth-code exchange."""
    config = _get_or_404(session, provider_id)
    settings = parse_oidc_settings(json.loads(decrypt_secret(config.config)))
    try:
        discovery = fetch_discovery_document(settings.issuer)
        for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if required not in discovery:
                return ConnectionCheckResult(
                    ok=False, detail=f"discovery document is missing {required}"
                )
        fetch_jwks(discovery["jwks_uri"])
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        return ConnectionCheckResult(ok=False, detail=str(exc))

    return ConnectionCheckResult(ok=True, detail="Connected")
