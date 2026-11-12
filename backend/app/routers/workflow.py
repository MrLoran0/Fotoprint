"""Путь первого дня: текст → черновик → правка → карточка → подтверждённые поручения + календарь и рассылка."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..access import can_access_event, get_meeting, get_protocol, get_task, meeting_filter, task_filter
from ..actions import (
    action_out,
    build_decision_proposals,
    build_summary_payload,
    can_access_action,
    create_action,
    execute,
    find_free_slots,
    get_action,
    payload_hash,
    task_snapshot,
    validate_due,
    validate_owner,
    validate_payload,
)
from ..config import get_settings
from ..db import get_db
from ..evidence import sensitive_request
from ..ingest import parse_transcript
from ..llm import LLMError, get_gateway
from ..models import (
    ROLE_DIRECTOR,
    ActionProposal,
    CalendarEvent,
    Employee,
    Meeting,
    Protocol,
    Task,
    Transcript,
    User,
    new_id,
)
from ..pipelines.protocol import build_item, extract
from ..schemas import (
    ActionEditIn,
    ActionOut,
    ActionType,
    ConfirmIn,
    CreateEventPayload,
    DraftPatchIn,
    EventOut,
    FreeSlotsIn,
    MeetingOut,
    ProtocolDraftOut,
    ProtocolItemOut,
    SendSummaryPayload,
    SlotOut,
    TaskChangeIn,
    TaskOut,
    TranscriptIn,
    TranscriptSegment,
)
from ..security import current_user

router = APIRouter(tags=["workflow"])

_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "publish_protocol": ("protocol_id", "protocol_revision"),
    "update_task": ("task_id", "expected_hash"),
    "delegate_task": ("task_id", "expected_hash"),
    "send_summary": ("protocol_id",),
    "create_event": ("meeting_id",),
    "send_brief": ("recipient_id",),
}


class ActionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ActionType
    payload: dict


def draft_out(db: Session, protocol: Protocol) -> ProtocolDraftOut:
    out = ProtocolDraftOut.model_validate(protocol)
    out.segments = [TranscriptSegment.model_validate(s) for s in db.get(Transcript, protocol.transcript_id).segments]
    return out


def task_out(task: Task) -> TaskOut:
    out = TaskOut.model_validate(task)
    out.overdue = task.status not in {"done", "cancelled"} and task.due_at is not None and task.due_at < get_settings().now()
    return out


@router.get("/meetings", response_model=list[MeetingOut])
def meetings(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Meeting).where(meeting_filter(user)).order_by(Meeting.starts_at.desc())).all()
    return [MeetingOut.model_validate(m) for m in rows]


@router.post("/calendar/free-slots", response_model=list[SlotOut])
def calendar_free_slots(body: FreeSlotsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return find_free_slots(db, user, body)


@router.get("/calendar/events", response_model=list[EventOut])
def calendar_events(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(CalendarEvent).order_by(CalendarEvent.starts_at.asc())).all()
    return [EventOut.model_validate(ev) for ev in rows if can_access_event(db, user, ev)]


@router.post("/calendar/events/proposal", response_model=ActionOut)
@router.post("/calendar/events", response_model=ActionOut)
def propose_calendar_event(body: CreateEventPayload, user: User = Depends(current_user), db: Session = Depends(get_db)):
    payload = body.model_dump(mode="json")
    key = payload_hash({"user": user.id, "create_event": payload})
    return action_out(create_action(db, user, "create_event", payload, key=key))


@router.post("/meetings/{meeting_id}/transcript", response_model=ProtocolDraftOut)
async def transcript(meeting_id: str, body: TranscriptIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    meeting = get_meeting(db, user, meeting_id)
    segments = parse_transcript(body.text)
    if not segments:
        raise HTTPException(422, "Введите текст расшифровки")
    if sensitive_request(body.text):
        raise HTTPException(422, "Удалите оклады и персональные данные перед обработкой AI")
    source = Transcript(id=new_id("tr"), meeting_id=meeting.id, source="text", raw_text=body.text, segments=segments)
    db.add(source)
    # Сохраняем ввод до обращения к модели: сбой не уничтожает расшифровку.
    audit.write(db, user, "transcript.upload", object_type="transcript", object_id=source.id,
                details={"meeting_id": meeting.id, "segments": len(segments)})
    employee_query = select(Employee)
    if user.role != ROLE_DIRECTOR:
        employee_query = employee_query.where(Employee.department_id == user.department_id)
    employees = list(db.scalars(employee_query).all())
    db.commit()
    try:
        extraction, result = await extract(get_gateway(), segments)
    except LLMError:
        audit.write(db, user, "protocol.extract", object_type="transcript", object_id=source.id, result="error")
        raise
    # Права могли измениться во время генерации.
    db.refresh(user)
    meeting = get_meeting(db, user, meeting_id)
    protocol = Protocol(id=new_id("protocol"), meeting_id=meeting.id, transcript_id=source.id,
                        summary=extraction.summary, route=result.route, model=result.model,
                        status="draft", revision=1, created_by=user.id)
    protocol.items = [build_item(item, i, segments, meeting, employees) for i, item in enumerate(extraction.items)]
    db.add(protocol)
    audit.write(db, user, "protocol.extract", object_type="protocol", object_id=protocol.id,
                details={"route": result.route, "model": result.model, "items": len(protocol.items)})
    return draft_out(db, protocol)


@router.get("/meetings/{meeting_id}/transcripts")
def transcripts(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    get_meeting(db, user, meeting_id)
    rows = db.scalars(select(Transcript).where(Transcript.meeting_id == meeting_id).order_by(Transcript.created_at.desc())).all()
    return [{"id": row.id, "text": row.raw_text, "segments": row.segments} for row in rows]


@router.get("/drafts/{draft_id}", response_model=ProtocolDraftOut)
def draft(draft_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return draft_out(db, get_protocol(db, user, draft_id))


@router.patch("/drafts/{draft_id}", response_model=ProtocolDraftOut)
def patch_draft(draft_id: str, body: DraftPatchIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    protocol = get_protocol(db, user, draft_id, lock=True)
    if protocol.status != "draft" or protocol.revision != body.revision:
        raise HTTPException(409, "Черновик изменился. Обновите его перед правкой")
    by_id = {i.id: i for i in protocol.items}
    if len({p.id for p in body.items}) != len(body.items) or any(p.id not in by_id for p in body.items):
        raise HTTPException(422, "Неизвестные или повторяющиеся пункты")
    for patch in body.items:
        item = by_id[patch.id]
        fields = patch.model_fields_set - {"id"}
        for field in fields:
            value = getattr(patch, field)
            if field not in {"owner_employee_id", "due_at", "decision_status"} and value is None:
                raise HTTPException(422, f"Поле {field} не может быть null")
            if field == "statement" and (not value.strip() or len(value) > 500):
                raise HTTPException(422, "Формулировка должна содержать от 1 до 500 символов")
            if field == "owner_employee_id":
                validate_owner(db, user, value)
                item.owner_origin = "user_entered" if value else "unknown"
            if field == "due_at":
                validate_due(value)
                item.due_origin = "user_entered" if value else "unknown"
            setattr(item, field, value)
        if item.kind == "proposal":
            item.decision_status = "proposed"
        item.needs_clarification = [f for f in item.needs_clarification if f not in {"owner_employee_id", "due_at"}]
        if item.kind == "task":
            if item.owner_employee_id is None:
                item.needs_clarification.append("owner_employee_id")
            if item.due_at is None:
                item.needs_clarification.append("due_at")
    protocol.revision += 1
    audit.write(db, user, "protocol.edit", object_type="protocol", object_id=protocol.id,
                details={"revision": protocol.revision})
    return draft_out(db, protocol)


@router.post("/drafts/{draft_id}/proposals", response_model=list[ActionOut])
def propose(draft_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    protocol = get_protocol(db, user, draft_id, lock=True)
    if protocol.status == "published":
        out: list[ActionOut] = []
        payload = build_summary_payload(db, user, protocol)
        if payload is not None:
            key = payload_hash({"user": user.id, "send_summary": protocol.id})
            out.append(action_out(create_action(db, user, "send_summary", payload, key=key)))
        for item_id, dec_payload in build_decision_proposals(db, user, protocol):
            dec_key = payload_hash({"user": user.id, "protocol_decision": protocol.id, "item": item_id})
            out.append(action_out(create_action(db, user, "create_decision", dec_payload, key=dec_key, origin="ai")))
        if not out:
            raise HTTPException(422, "Нет получателей для рассылки итогов")
        return out
    payload = {"protocol_id": protocol.id, "protocol_revision": protocol.revision,
               "items": [ProtocolItemOut.model_validate(i).model_dump(mode="json") for i in protocol.items]}
    key = payload_hash({"user": user.id, "protocol": protocol.id, "revision": protocol.revision})
    return [action_out(create_action(db, user, "publish_protocol", payload, key=key))]


@router.post("/drafts/{draft_id}/summary-proposal", response_model=ActionOut)
def propose_summary(
    draft_id: str,
    body: SendSummaryPayload | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    protocol = get_protocol(db, user, draft_id, lock=True)
    if protocol.status != "published":
        raise HTTPException(409, "Сначала опубликуйте протокол")
    if body is not None:
        if body.protocol_id != protocol.id:
            raise HTTPException(422, "Нельзя менять объект и исходную версию действия")
        payload = body.model_dump(mode="json")
        key = payload_hash({"user": user.id, "send_summary": protocol.id, "custom": payload})
    else:
        payload = build_summary_payload(db, user, protocol)
        if payload is None:
            raise HTTPException(422, "Нет получателей для рассылки итогов")
        key = payload_hash({"user": user.id, "send_summary": protocol.id})
    return action_out(create_action(db, user, "send_summary", payload, key=key))


@router.get("/actions", response_model=list[ActionOut])
def actions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(ActionProposal).where(ActionProposal.created_by == user.id).order_by(ActionProposal.created_at.desc())).all()
    return [action_out(a) for a in rows if can_access_action(db, user, a)]


@router.post("/actions", response_model=ActionOut)
def create_action_proposal(body: ActionCreateIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return action_out(create_action(db, user, body.type, body.payload))


@router.get("/actions/{action_id}", response_model=ActionOut)
def action(action_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return action_out(get_action(db, user, action_id))


@router.patch("/actions/{action_id}", response_model=ActionOut)
def edit_action(action_id: str, body: ActionEditIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_action(db, user, action_id, lock=True)
    if row.status != "pending" or row.revision != body.revision:
        raise HTTPException(409, "Карточка изменилась или уже обработана")
    identity = _IDENTITY_KEYS.get(row.type, ())
    if any(body.payload.get(key) != row.payload.get(key) for key in identity):
        raise HTTPException(422, "Нельзя менять объект и исходную версию действия")
    _, payload = validate_payload(db, user, row.type, body.payload)
    row.payload = payload
    row.payload_hash = payload_hash(payload)
    row.revision += 1
    audit.write(db, user, "action.edit", object_type="action", object_id=row.id, details={"revision": row.revision})
    return action_out(row)


@router.post("/actions/{action_id}/confirm", response_model=ActionOut)
def confirm(action_id: str, body: ConfirmIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_action(db, user, action_id, lock=True)
    if row.revision != body.revision or row.payload_hash != body.payload_hash:
        raise HTTPException(409, "Карточка изменилась. Проверьте новый предпросмотр")
    if row.status == "executed":
        return action_out(row)
    if row.status not in {"pending", "partially_failed", "failed"}:
        raise HTTPException(409, "Действие уже обработано")
    execute(db, user, row)
    return action_out(row)


@router.post("/actions/{action_id}/retry", response_model=ActionOut)
def retry_action(action_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_action(db, user, action_id, lock=True)
    if row.status == "executed":
        return action_out(row)
    if row.status not in {"partially_failed", "failed"}:
        raise HTTPException(409, "Повтор возможен только после ошибки доставки")
    execute(db, user, row)
    return action_out(row)


@router.post("/actions/{action_id}/cancel", response_model=ActionOut)
def cancel(action_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_action(db, user, action_id, lock=True)
    if row.status == "cancelled":
        return action_out(row)
    if row.status != "pending":
        raise HTTPException(409, "Действие уже обработано")
    row.status = "cancelled"
    audit.write(db, user, "action.cancel", object_type="action", object_id=row.id, result="cancelled")
    return action_out(row)


@router.get("/tasks", response_model=list[TaskOut])
def tasks(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [task_out(t) for t in db.scalars(select(Task).where(task_filter(user)).order_by(Task.created_at.desc())).all()]


@router.get("/tasks/{task_id}", response_model=TaskOut)
def task(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return task_out(get_task(db, user, task_id))


@router.post("/tasks/{task_id}/change-proposal", response_model=ActionOut)
def change_task(task_id: str, body: TaskChangeIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_task(db, user, task_id, lock=True)
    payload = {"task_id": row.id, "expected_hash": payload_hash(task_snapshot(row)),
               "changes": body.model_dump(mode="json", exclude_unset=True)}
    return action_out(create_action(db, user, "update_task", payload))
