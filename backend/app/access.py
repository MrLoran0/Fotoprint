"""Права на связанные бизнес-объекты проверяются при каждом чтении и исполнении."""

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, true
from sqlalchemy.orm import Session

from . import audit
from .models import ROLE_DIRECTOR, CalendarEvent, Employee, Meeting, Protocol, Task, User


def meeting_filter(user: User):
    if user.role == ROLE_DIRECTOR:
        return true()
    return and_(Meeting.department_id.is_not(None), Meeting.department_id == user.department_id)


def can_access_event(db: Session, user: User, event: CalendarEvent) -> bool:
    if user.role == ROLE_DIRECTOR:
        return True
    if event.meeting_id is not None:
        return db.scalar(select(Meeting.id).where(Meeting.id == event.meeting_id, meeting_filter(user))) is not None
    if not event.attendee_ids:
        return False
    dept_ids = set(db.scalars(select(Employee.id).where(Employee.department_id == user.department_id)).all())
    if user.employee_id:
        dept_ids.add(user.employee_id)
    return bool(dept_ids.intersection(event.attendee_ids))


def task_filter(user: User):
    if user.role == ROLE_DIRECTOR:
        return true()
    return and_(
        Task.department_id.is_not(None),
        Task.department_id == user.department_id,
        or_(Task.meeting_id.is_(None), Task.meeting_id.in_(select(Meeting.id).where(meeting_filter(user)))),
    )


def deny(db: Session, user: User, kind: str, object_id: str):
    audit.write(db, user, f"{kind}.access", object_type=kind, object_id=object_id, result="denied")
    raise HTTPException(403, "Объект недоступен")


def get_meeting(db: Session, user: User, meeting_id: str, *, lock: bool = False) -> Meeting:
    query = select(Meeting).where(Meeting.id == meeting_id, meeting_filter(user))
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        deny(db, user, "meeting", meeting_id)
    return row


def get_protocol(db: Session, user: User, protocol_id: str, *, lock: bool = False) -> Protocol:
    query = select(Protocol).where(Protocol.id == protocol_id)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        deny(db, user, "protocol", protocol_id)
    get_meeting(db, user, row.meeting_id)
    if row.status == "draft" and row.created_by != user.id:
        deny(db, user, "protocol", protocol_id)
    return row


def get_task(db: Session, user: User, task_id: str, *, lock: bool = False) -> Task:
    query = select(Task).where(Task.id == task_id, task_filter(user))
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        deny(db, user, "task", task_id)
    return row
