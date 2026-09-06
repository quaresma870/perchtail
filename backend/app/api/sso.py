import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import (
    GlobalCapability,
    Role,
    SSOGroupRoleMapping,
    SSOProtocol,
    SSOProviderConfig,
    User,
)
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
    # Name of the ID token claim carrying group memberships, or null if
    # group-to-role auto-mapping isn't configured for this provider (see
    # SSOGroupRoleMapping).
    group_claim: str | None
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
            group_claim=raw.get("group_claim") or None,
        )


class SSOProviderCreate(BaseModel):
    name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str = "openid email profile"
    group_claim: str | None = None
    enabled: bool = False


class SSOProviderUpdate(BaseModel):
    name: str | None = None
    issuer: str | None = None
    client_id: str | None = None
    # Only overwrites the stored secret if provided — omitting it keeps the
    # existing one, so editing the issuer/name doesn't force re-entering it.
    client_secret: str | None = None
    scopes: str | None = None
    # Empty string clears it (disables group mapping); omit to leave
    # whatever's currently configured unchanged.
    group_claim: str | None = None
    enabled: bool | None = None


class ConnectionCheckResult(BaseModel):
    ok: bool
    detail: str


def _encrypt_config(
    *, issuer: str, client_id: str, client_secret: str, scopes: str, group_claim: str | None
) -> str:
    return encrypt_secret(
        json.dumps(
            {
                "issuer": issuer,
                "client_id": client_id,
                "client_secret": client_secret,
                "scopes": scopes,
                "group_claim": group_claim,
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
            group_claim=payload.group_claim,
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


class GroupRoleMappingPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order: int
    group_name: str
    role_id: int
    role_name: str


class GroupRoleMappingCreate(BaseModel):
    order: int
    group_name: str
    role_id: int


class GroupRoleMappingUpdate(BaseModel):
    order: int | None = None
    group_name: str | None = None
    role_id: int | None = None


def _mapping_public(mapping: SSOGroupRoleMapping, role_name: str) -> GroupRoleMappingPublic:
    return GroupRoleMappingPublic(
        id=mapping.id,
        order=mapping.order,
        group_name=mapping.group_name,
        role_id=mapping.role_id,
        role_name=role_name,
    )


def _require_role_exists(session: Session, role_id: int) -> Role:
    role = session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return role


# Registered before "/{provider_id}" below -- FastAPI/Starlette match routes
# in registration order, and "/{provider_id}" would otherwise shadow
# "/group-mappings" (a single path segment matches the dynamic param too),
# turning every request here into a 422 from trying to parse
# "group-mappings" as an int.
@router.get("/group-mappings", response_model=list[GroupRoleMappingPublic])
def list_group_mappings(
    user: User = Depends(require_manage), session: Session = Depends(get_session)
):
    mappings = session.exec(select(SSOGroupRoleMapping).order_by(SSOGroupRoleMapping.order)).all()
    roles_by_id = {r.id: r.name for r in session.exec(select(Role)).all()}
    return [_mapping_public(m, roles_by_id.get(m.role_id, f"#{m.role_id}")) for m in mappings]


@router.post(
    "/group-mappings", response_model=GroupRoleMappingPublic, status_code=status.HTTP_201_CREATED
)
def create_group_mapping(
    payload: GroupRoleMappingCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    role = _require_role_exists(session, payload.role_id)
    mapping = SSOGroupRoleMapping(
        order=payload.order, group_name=payload.group_name, role_id=payload.role_id
    )
    session.add(mapping)
    session.flush()

    record_audit_event(
        session,
        user_id=user.id,
        action="sso_group_mapping.create",
        target_type="sso_group_mapping",
        target_id=mapping.id,
        metadata={"group_name": payload.group_name, "role_id": payload.role_id},
    )
    session.commit()
    session.refresh(mapping)
    return _mapping_public(mapping, role.name)


def _get_mapping_or_404(session: Session, mapping_id: int) -> SSOGroupRoleMapping:
    mapping = session.get(SSOGroupRoleMapping, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    return mapping


@router.patch("/group-mappings/{mapping_id}", response_model=GroupRoleMappingPublic)
def update_group_mapping(
    mapping_id: int,
    payload: GroupRoleMappingUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    mapping = _get_mapping_or_404(session, mapping_id)
    if payload.order is not None:
        mapping.order = payload.order
    if payload.group_name is not None:
        mapping.group_name = payload.group_name
    if payload.role_id is not None:
        _require_role_exists(session, payload.role_id)
        mapping.role_id = payload.role_id
    session.add(mapping)

    record_audit_event(
        session,
        user_id=user.id,
        action="sso_group_mapping.update",
        target_type="sso_group_mapping",
        target_id=mapping.id,
    )
    session.commit()
    session.refresh(mapping)
    role = _require_role_exists(session, mapping.role_id)
    return _mapping_public(mapping, role.name)


@router.delete("/group-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group_mapping(
    mapping_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    mapping = _get_mapping_or_404(session, mapping_id)
    session.delete(mapping)

    record_audit_event(
        session,
        user_id=user.id,
        action="sso_group_mapping.delete",
        target_type="sso_group_mapping",
        target_id=mapping_id,
    )
    session.commit()


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
    if payload.group_claim is not None:
        raw["group_claim"] = payload.group_claim or None

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
