"""Search the file-name index (#304) across every source the user can view.

Only names, sizes and times from the index; nothing is listed on the
servers. Results are paged (`after`, `limit`), with no cap on how many match
in total. The answer also says, for each source the user can view, whether
its index is on and when it last ran, so the explorer can tell which
sources the results don't cover yet."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app import system_settings
from app.api.auth import get_current_active_user
from app.auth.models import Capability, User
from app.auth.rbac import visible_source_ids
from app.db import get_session
from app.models import Rule, Source
from app.name_index_store import InvalidPattern, compile_matcher, search_entries
from app.rules import is_visible, rules_case_insensitive

router = APIRouter(prefix="/files", tags=["files"])

MAX_PAGE = 1000


class FileHit(BaseModel):
    source_id: int
    path: str
    size: int
    mtime: float | None


class IndexedSource(BaseModel):
    source_id: int
    name_index_enabled: bool
    name_indexed_at: datetime | None
    name_index_files: int | None
    name_index_message: str | None


class FileSearchResult(BaseModel):
    hits: list[FileHit]
    # Pass as `after` to get the next page; None when there are no more.
    next_after: int | None
    index_enabled: bool
    sources: list[IndexedSource]


@router.get("", response_model=FileSearchResult)
def search_files(
    q: str = Query(default="", max_length=500),
    regex: bool = False,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=MAX_PAGE),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    try:
        matcher = compile_matcher(q, regex)
    except InvalidPattern as exc:
        raise HTTPException(status_code=422, detail="Invalid regex") from exc

    visible = visible_source_ids(session, user, Capability.view)
    query = select(Source).where(Source.enabled.is_(True))
    if visible is not None:  # None: a super admin, who sees every source
        query = query.where(Source.id.in_(visible))
    sources = session.exec(query.order_by(Source.id)).all()
    source_ids = {s.id for s in sources}

    index_enabled = system_settings.get_bool(session, "name_index_enabled")
    hits: list[FileHit] = []
    next_after = None
    if index_enabled and source_ids:
        rows, next_after = search_entries(session, matcher, source_ids, after, limit)
        # The index is as of its last pass; the rules are checked now, so a
        # file a rule hides since then isn't shown by name either.
        by_id = {s.id: s for s in sources}
        hit_ids = {sid for _id, sid, _p, _s, _m in rows}
        rules: dict[int, list[Rule]] = {sid: [] for sid in hit_ids}
        for rule in session.exec(select(Rule).where(Rule.source_id.in_(hit_ids))).all():
            rules[rule.source_id].append(rule)
        hits = [
            FileHit(source_id=sid, path=path, size=size, mtime=mtime)
            for _id, sid, path, size, mtime in rows
            if is_visible(path, rules[sid], case_insensitive=rules_case_insensitive(by_id[sid]))
        ]

    return FileSearchResult(
        hits=hits,
        next_after=next_after,
        index_enabled=index_enabled,
        sources=[
            IndexedSource(
                source_id=s.id,
                name_index_enabled=s.name_index_enabled,
                name_indexed_at=s.name_indexed_at,
                name_index_files=s.name_index_files,
                name_index_message=s.name_index_message,
            )
            for s in sources
        ],
    )
