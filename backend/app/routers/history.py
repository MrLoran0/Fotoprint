"""Аудит без текстов источников; доступ к связанным объектам проверяется заново."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..access import meeting_filter, task_filter
from ..actions import can_access_action
from ..db import get_db
from ..models import ActionProposal, AuditLog, Document, Meeting, Protocol, Transcript, User, Task, ROLE_DIRECTOR
from ..schemas import AuditOut
from ..security import can_read_document, current_user

router = APIRouter(tags=["audit"])


def visible(db: Session, user: User, entry: AuditLog) -> bool:
    if user.role == ROLE_DIRECTOR or entry.result == "denied":
        return True
    kind, object_id = entry.object_type, entry.object_id
    if kind == "document":
        doc = db.get(Document, object_id)
        return doc is not None and can_read_document(user, doc)
    if kind == "meeting":
        return db.scalar(select(Meeting.id).where(Meeting.id == object_id, meeting_filter(user))) is not None
    if kind in {"protocol", "transcript"}:
        model = Protocol if kind == "protocol" else Transcript
        return db.scalar(select(model.id).join(Meeting, model.meeting_id == Meeting.id).where(
            model.id == object_id, meeting_filter(user))) is not None
    if kind == "task":
        return db.scalar(select(Task.id).where(Task.id == object_id, task_filter(user))) is not None
    if kind == "action":
        action = db.get(ActionProposal, object_id)
        return action is not None and can_access_action(db, user, action)
    return True


@router.get("/audit", response_model=list[AuditOut])
def history(limit: int = Query(default=100, ge=1, le=500), user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(AuditLog).order_by(AuditLog.id.desc())
    if user.role != ROLE_DIRECTOR:
        query = query.where(AuditLog.user_id == user.id)
    rows = db.scalars(query.limit(limit)).all()
    # Чужой вопрос ассистенту и ссылки на найденную переписку — личные данные автора: директор видит факт, маршрут
    # и результат, но не текст вопроса и не ID чужих фрагментов (иначе журнал раскрыл бы закрытые чаты).
    private = {"question", "candidate_chunks", "cited_chunks"}
    names = {u.id: u.full_name for u in db.scalars(select(User)).all()}
    out = []
    for row in rows:
        if visible(db, user, row):
            item = AuditOut.model_validate(row)
            if row.user_id != user.id and private & set(item.details or {}):
                item.details = {k: v for k, v in item.details.items() if k not in private} | {"скрыто": "текст запроса и источники видны только автору"}
            item.user_name = names.get(row.user_id)
            out.append(item)
    return out
