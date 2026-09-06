from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from app.api.auth import get_current_active_user
from app.auth.models import Capability, User
from app.auth.rbac import visible_source_ids
from app.db import get_session
from app.search_index import search as run_search

router = APIRouter(prefix="/search", tags=["search"])


class SearchHitPublic(BaseModel):
    source_id: int
    file_path: str
    line_number: int
    snippet_html: str
    matched_field: str


@router.get("", response_model=list[SearchHitPublic])
def search(
    q: str = "",
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    query = q.strip()
    if not query:
        return []
    source_ids = visible_source_ids(session, user, Capability.view)
    hits = run_search(session, query, source_ids)
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
