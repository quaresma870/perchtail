from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.db import get_session
from app.models import Customer, Folder, Source

router = APIRouter(prefix="/customers", tags=["customers"])

require_manage = require_global_capability(GlobalCapability.create_source, get_current_active_user)


class CustomerPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class CustomerCreate(BaseModel):
    name: str


class CustomerUpdate(BaseModel):
    name: str


@router.get("", response_model=list[CustomerPublic])
def list_customers(user: User = Depends(require_manage), session: Session = Depends(get_session)):
    # Customer/folder organization is an admin surface (CLAUDE.md: "folder
    # management ... is its own small admin surface") — gated by the
    # create_source global capability rather than per-customer grants, same
    # as create/update/delete below.
    return session.exec(select(Customer)).all()


@router.post("", response_model=CustomerPublic, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
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
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

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
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    has_sources = session.exec(select(Source).where(Source.customer_id == customer_id)).first()
    has_folders = session.exec(select(Folder).where(Folder.customer_id == customer_id)).first()
    if has_sources or has_folders:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Customer still has sources or folders; remove them first",
        )

    session.delete(customer)
    record_audit_event(
        session,
        user_id=user.id,
        action="customer.delete",
        target_type="customer",
        target_id=customer_id,
    )
    session.commit()
