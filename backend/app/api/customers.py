from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.alerts import drop_scoped_alerts
from app.api.auth import get_current_active_user, require_super_admin
from app.audit import record_audit_event
from app.auth.models import Capability, GlobalCapability, RoleGrant, ScopeType, User
from app.auth.rbac import (
    concealed,
    customer_in_reach,
    delete_scope_grants,
    forbidden,
    org_reach,
    require_any_global_capability,
    require_global_capability,
)
from app.db import get_session
from app.models import Customer, Folder, Source

router = APIRouter(prefix="/customers", tags=["customers"])

require_manage = require_global_capability(GlobalCapability.create_source, get_current_active_user)
# The list also backs the role editor, so a delegated admin with
# manage_roles but not create_source can pick the customers in their reach.
require_list = require_any_global_capability(
    (GlobalCapability.create_source, GlobalCapability.manage_roles), get_current_active_user
)

CUSTOMER_NOT_FOUND = "Customer not found"
MANAGE_CUSTOMER_REFUSED = "You need manage sources on this customer to change it."


class CustomerPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class CustomerCreate(BaseModel):
    name: str


class CustomerUpdate(BaseModel):
    name: str


def _get_customer_in_reach(session: Session, user: User, customer_id: int) -> Customer:
    """The customer, if it exists and the caller's grants reach it. One
    outside their reach answers exactly like one that doesn't exist."""
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CUSTOMER_NOT_FOUND)
    if not customer_in_reach(session, user, customer_id):
        raise concealed(CUSTOMER_NOT_FOUND, reason="customer_out_of_reach", user=user)
    return customer


def _require_manage_customer(session: Session, user: User, customer: Customer) -> None:
    """Renaming or deleting a customer needs manage_source in a grant on the
    customer itself: a grant on one of its folders or sources covers only
    that part of it."""
    if user.role.is_super_admin:
        return
    grant = session.exec(
        select(RoleGrant).where(
            RoleGrant.role_id == user.role_id,
            RoleGrant.scope_type == ScopeType.customer,
            RoleGrant.scope_id == customer.id,
        )
    ).first()
    if grant is None or Capability.manage_source not in grant.capabilities:
        raise forbidden(
            MANAGE_CUSTOMER_REFUSED,
            reason=f"missing_capability:{Capability.manage_source}",
            user=user,
        )


@router.get("", response_model=list[CustomerPublic])
def list_customers(user: User = Depends(require_list), session: Session = Depends(get_session)):
    """Every customer for a super admin; for anyone else, the customers
    their role has a grant reaching (rbac.org_reach)."""
    customers = session.exec(select(Customer)).all()
    reach = org_reach(session, user)
    if reach is None:
        return customers
    return [customer for customer in customers if customer.id in reach.customer_ids]


@router.post("", response_model=CustomerPublic, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate,
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    # A customer is the boundary between customers: only a super admin adds
    # one. A delegated admin would otherwise create customers they then had
    # no access to.
    if session.exec(select(Customer).where(Customer.name == payload.name)).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already in use")

    customer = Customer(name=payload.name)
    session.add(customer)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="customer.create",
        target_type="customer",
        target_id=customer.id,
        metadata={"name": customer.name},
    )
    session.commit()
    session.refresh(customer)
    return customer


@router.patch("/{customer_id}", response_model=CustomerPublic)
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    customer = _get_customer_in_reach(session, user, customer_id)
    _require_manage_customer(session, user, customer)

    # Checked before the name is set: the unique index would otherwise
    # answer with a server error on the next query's autoflush.
    taken = session.exec(
        select(Customer).where(Customer.name == payload.name, Customer.id != customer.id)
    ).first()
    if taken is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already in use")
    customer.name = payload.name
    session.add(customer)
    record_audit_event(
        session,
        user_id=user.id,
        action="customer.update",
        target_type="customer",
        target_id=customer.id,
        metadata={"name": customer.name},
    )
    session.commit()
    session.refresh(customer)
    return customer


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    customer = _get_customer_in_reach(session, user, customer_id)
    _require_manage_customer(session, user, customer)

    has_sources = session.exec(select(Source).where(Source.customer_id == customer_id)).first()
    has_folders = session.exec(select(Folder).where(Folder.customer_id == customer_id)).first()
    if has_sources or has_folders:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Customer still has sources or folders; remove them first",
        )

    delete_scope_grants(session, ScopeType.customer, customer_id)
    drop_scoped_alerts(session, customer_id=customer_id)
    session.delete(customer)
    record_audit_event(
        session,
        user_id=user.id,
        action="customer.delete",
        target_type="customer",
        target_id=customer_id,
    )
    session.commit()
