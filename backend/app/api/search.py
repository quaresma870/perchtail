from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.auth import get_current_active_user
from app.auth.models import Capability, User
from app.auth.rbac import visible_source_ids
from app.db import get_session
from app.models import Folder, Source
from app.search_index import search as run_search

router = APIRouter(prefix="/search", tags=["search"])

_MAX_SCOPE_IDS = 200


class SearchHitPublic(BaseModel):
    source_id: int
    file_path: str
    line_number: int
    snippet_html: str
    matched_field: str


def _scope_source_ids(
    session: Session,
    customer_ids: list[int],
    folder_ids: list[int],
    source_ids: list[int],
) -> set[int]:
    """Every source under the chosen customers, folders (with all their
    sub-folders) and sources (#163). Not an access check: the caller
    intersects this with what the user may view."""
    scope = set(source_ids)
    if customer_ids:
        scope |= set(
            session.exec(select(Source.id).where(Source.customer_id.in_(customer_ids))).all()
        )
    if folder_ids:
        children: dict[int, list[int]] = {}
        for folder in session.exec(select(Folder)).all():
            if folder.parent_folder_id is not None:
                children.setdefault(folder.parent_folder_id, []).append(folder.id)
        pending, folders = list(folder_ids), set()
        while pending:
            folder_id = pending.pop()
            if folder_id in folders:
                continue
            folders.add(folder_id)
            pending.extend(children.get(folder_id, []))
        scope |= set(session.exec(select(Source.id).where(Source.folder_id.in_(folders))).all())
    return scope


@router.get("", response_model=list[SearchHitPublic])
def search(
    q: str = "",
    customer_id: list[int] = Query(default=[], max_length=_MAX_SCOPE_IDS),
    folder_id: list[int] = Query(default=[], max_length=_MAX_SCOPE_IDS),
    source_id: list[int] = Query(default=[], max_length=_MAX_SCOPE_IDS),
    path: str = Query(default="", max_length=512),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    """Searches the indexed content of the sources the user can view,
    optionally narrowed to some customers, folders (recursively) or sources
    and to file paths containing `path` (or matching it, with `*`/`?`). The
    scope is applied in SQL, before the result limit, so narrowing it can
    surface matches a broad search would have cut off."""
    query = q.strip()
    if not query:
        return []
    source_ids = visible_source_ids(session, user, Capability.view)
    if customer_id or folder_id or source_id:
        scope = _scope_source_ids(session, customer_id, folder_id, source_id)
        source_ids = scope if source_ids is None else source_ids & scope
    hits = run_search(session, query, source_ids, path_filter=path.strip())
    return [
        SearchHitPublic(
            source_id=hit.source_id,
            file_path=hit.file_path,
            line_number=hit.line_number,
            snippet_html=hit.snippet_html,
            matched_field=hit.matched_field,
        )
        for hit in hits
    ]
