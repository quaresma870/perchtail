from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.db import get_session
from app.models import Customer, Folder, Source

router = APIRouter(prefix="/folders", tags=["folders"])

require_manage = require_global_capability(GlobalCapability.create_source, get_current_active_user)


class FolderPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    customer_id: int
    parent_folder_id: int | None


class FolderCreate(BaseModel):
    name: str
    customer_id: int
    parent_folder_id: int | None = None


class FolderUpdate(BaseModel):
    name: str | None = None
    parent_folder_id: int | None = None


def _get_customer_or_404(session: Session, customer_id: int) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer


def _validate_parent(
    session: Session, *, customer_id: int, parent_folder_id: int | None, folder_id: int | None
) -> None:
    if parent_folder_id is None:
        return
    parent = session.get(Folder, parent_folder_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found")
    if parent.customer_id != customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A folder can only nest under a folder of the same customer",
        )
    # Prevent a folder becoming its own ancestor when moving.
    walk = parent
    while walk is not None:
        if folder_id is not None and walk.id == folder_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot move a folder under its own descendant",
            )
        walk = session.get(Folder, walk.parent_folder_id) if walk.parent_folder_id else None


@router.get("", response_model=list[FolderPublic])
def list_folders(
    customer_id: int | None = None,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    query = select(Folder)
    if customer_id is not None:
        query = query.where(Folder.customer_id == customer_id)
    return session.exec(query).all()


@router.post("", response_model=FolderPublic, status_code=status.HTTP_201_CREATED)
def create_folder(
    payload: FolderCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    _get_customer_or_404(session, payload.customer_id)
    _validate_parent(
        session,
        customer_id=payload.customer_id,
        parent_folder_id=payload.parent_folder_id,
        folder_id=None,
    )

    folder = Folder(
        name=payload.name,
        customer_id=payload.customer_id,
        parent_folder_id=payload.parent_folder_id,
    )
    session.add(folder)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="folder.create",
        target_type="folder",
        target_id=folder.id,
        metadata={"name": folder.name, "customer_id": folder.customer_id},
    )
    session.commit()
    session.refresh(folder)
    return folder


@router.patch("/{folder_id}", response_model=FolderPublic)
def update_folder(
    folder_id: int,
    payload: FolderUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    folder = session.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    if payload.name is not None:
        folder.name = payload.name

    if "parent_folder_id" in payload.model_fields_set:
        _validate_parent(
            session,
            customer_id=folder.customer_id,
            parent_folder_id=payload.parent_folder_id,
            folder_id=folder.id,
        )
        folder.parent_folder_id = payload.parent_folder_id

    session.add(folder)
    record_audit_event(
        session,
        user_id=user.id,
        action="folder.update",
        target_type="folder",
        target_id=folder.id,
    )
    session.commit()
    session.refresh(folder)
    return folder


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    folder = session.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    has_children = session.exec(select(Folder).where(Folder.parent_folder_id == folder_id)).first()
    has_sources = session.exec(select(Source).where(Source.folder_id == folder_id)).first()
    if has_children or has_sources:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Folder still has sub-folders or sources; remove them first",
        )

    session.delete(folder)
    record_audit_event(
        session,
        user_id=user.id,
        action="folder.delete",
        target_type="folder",
        target_id=folder_id,
    )
    session.commit()
