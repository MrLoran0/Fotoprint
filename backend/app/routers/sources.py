"""Источники данных: откуда AI-ассистент берёт материалы и в каком они состоянии."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..connectors.sync import sync_channel
from ..db import get_db
from ..models import ACCESS_DEPARTMENT, ACCESS_PARTICIPANTS, ROLE_DIRECTOR, SourceChannel, User
from ..schemas import SourceOut
from ..security import current_user, require_role

router = APIRouter(tags=["sources"])


def channel_visible(user: User, ch: SourceChannel) -> bool:
    if ch.access_level == ACCESS_PARTICIPANTS:
        return user.employee_id is not None and user.employee_id in (ch.member_ids or [])
    if ch.access_level == ACCESS_DEPARTMENT:
        return user.role == ROLE_DIRECTOR or ch.department_id == user.department_id
    return True


def source_out(user: User, ch: SourceChannel) -> SourceOut:
    return SourceOut(
        id=ch.id, kind=ch.kind, name=ch.name, connector=ch.connector, access_level=ch.access_level, status=ch.status,
        last_sync_at=ch.last_sync_at, items_count=ch.items_count, member_count=len(ch.member_ids or []),
        last_error=ch.last_error if user.role == ROLE_DIRECTOR else None,
    )


@router.get("/sources", response_model=list[SourceOut])
def sources(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[SourceOut]:
    rows = db.scalars(select(SourceChannel).order_by(SourceChannel.kind, SourceChannel.name)).all()
    return [source_out(user, ch) for ch in rows if channel_visible(user, ch)]


@router.post("/sources/{channel_id}/sync", response_model=SourceOut)
def sync(channel_id: str, user: User = Depends(require_role(ROLE_DIRECTOR)), db: Session = Depends(get_db)) -> SourceOut:
    ch = db.get(SourceChannel, channel_id)
    if ch is None or not channel_visible(user, ch):
        audit.write(db, user, "source.sync", object_type="source", object_id=channel_id, result="denied")
        raise HTTPException(403, "Источник недоступен")
    sync_channel(db, ch, get_settings().data_dir / "sources")
    audit.write(db, user, "source.sync", object_type="source", object_id=ch.id,
                result="ok" if ch.status == "ok" else "error", details={"items": ch.items_count}, commit=False)
    db.commit()
    return source_out(user, ch)
