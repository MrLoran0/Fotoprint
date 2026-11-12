"""Транзакционные действия: точный снимок, версия, хеш и блокировка при подтверждении."""

import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from . import audit, kpi, mailer, notify
from .access import can_access_event, deny, get_meeting, get_protocol, get_task, meeting_filter, task_filter
from .config import get_settings
from .evidence import quote_matches, resolve_due, resolve_owner, suspicious
from .models import (
    ROLE_DIRECTOR,
    ActionProposal,
    CalendarEvent,
    DeliveryJob,
    Employee,
    Meeting,
    Protocol,
    Task,
    Transcript,
    User,
    new_id,
)
from .schemas import (
    ActionOut,
    CreateDecisionPayload,
    CreateEventPayload,
    DelegateTaskPayload,
    FreeSlotsIn,
    ProtocolItemOut,
    SendSummaryPayload,
    SlotOut,
    SourceRef,
    SummaryLetter,
    TaskChangeIn,
)


def send_message(msg) -> str:
    return mailer.send_message(msg)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PublishPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_id: str
    protocol_revision: int
    items: list[ProtocolItemOut]


class UpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    expected_hash: str
    changes: TaskChangeIn


def task_snapshot(task: Task) -> dict:
    fields = ("title", "expected_result", "owner_employee_id", "owner_origin", "due_at", "due_origin", "status", "result")
    return {k: (v.isoformat() if isinstance(v := getattr(task, k), datetime) else v) for k in fields}


def validate_owner(db: Session, user: User, owner_id: str | None):
    if owner_id is None:
        return
    employee = db.get(Employee, owner_id)
    if employee is None or (user.role != ROLE_DIRECTOR and employee.department_id != user.department_id):
        raise HTTPException(422, "Выберите доступного сотрудника из справочника")


def validate_due(value: datetime | None):
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise HTTPException(422, "Срок должен содержать часовой пояс")


def validate_changes(db: Session, user: User, task: Task, changes: TaskChangeIn):
    fields = changes.model_fields_set
    if not fields:
        raise HTTPException(422, "Нет изменений")
    for key in fields - {"owner_employee_id", "due_at"}:
        if getattr(changes, key) is None:
            raise HTTPException(422, f"Поле {key} не может быть null")
    if "title" in fields and not changes.title.strip():
        raise HTTPException(422, "Название поручения не может быть пустым")
    validate_owner(db, user, changes.owner_employee_id)
    validate_due(changes.due_at)
    transitions = {"assigned": {"accepted", "cancelled"}, "accepted": {"in_progress", "done", "cancelled"},
                   "in_progress": {"done", "cancelled"}, "done": set(), "cancelled": set()}
    if changes.status is not None and changes.status != task.status and changes.status not in transitions[task.status]:
        raise HTTPException(422, "Недопустимый переход статуса поручения")
    if (changes.status or task.status) == "done" and not (changes.result if "result" in fields else task.result).strip():
        raise HTTPException(422, "Для выполненного поручения укажите результат")


def validate_publish(db: Session, user: User, payload: dict, *, lock: bool = False):
    try:
        parsed = PublishPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "Некорректное содержимое карточки") from exc
    protocol = get_protocol(db, user, parsed.protocol_id, lock=lock)
    if protocol.status != "draft" or protocol.revision != parsed.protocol_revision:
        raise HTTPException(409, "Протокол изменился. Создайте карточку заново")
    originals = {item.id: item for item in protocol.items}
    if len(parsed.items) != len(originals) or {i.id for i in parsed.items} != set(originals):
        raise HTTPException(422, "Карточка должна содержать все пункты протокола")
    transcript = db.get(Transcript, protocol.transcript_id)
    segments = {s["n"]: s["text"] for s in transcript.segments}
    immutable = ("position", "segment_id", "evidence_quote", "quote_verified", "owner_raw", "due_raw")
    for item in parsed.items:
        original = originals[item.id]
        if any(getattr(item, field) != getattr(original, field) for field in immutable):
            raise HTTPException(422, "Основание пункта менять нельзя; исключите пункт без достоверной цитаты")
        if not item.statement.strip() or len(item.statement) > 500:
            raise HTTPException(422, "Формулировка должна содержать от 1 до 500 символов")
        validate_owner(db, user, item.owner_employee_id)
        validate_due(item.due_at)
        if not item.removed:
            if not quote_matches(item.evidence_quote, segments.get(item.segment_id, "")):
                raise HTTPException(422, "Исключите пункты с неподтверждённой цитатой")
            if suspicious(segments.get(item.segment_id, "")):
                raise HTTPException(422, "Исключите пункт с подозрительной инструкцией из источника")
        # Происхождение и предупреждения пересчитываются сервером, а не принимаются из payload.
        item.owner_origin = ("user_entered" if item.owner_employee_id else "unknown") if item.owner_employee_id != original.owner_employee_id else original.owner_origin
        item.due_origin = ("user_entered" if item.due_at else "unknown") if item.due_at != original.due_at else original.due_origin
        item.needs_clarification = [f for f in original.needs_clarification if f not in {"owner_employee_id", "due_at"}]
        if item.kind == "task":
            if item.owner_employee_id is None:
                item.needs_clarification.append("owner_employee_id")
            if item.due_at is None:
                item.needs_clarification.append("due_at")
        if item.kind == "proposal":
            item.decision_status = "proposed"
    return protocol, parsed.model_dump(mode="json")


def find_free_slots(db: Session, user: User, body: FreeSlotsIn) -> list[SlotOut]:
    if not body.participant_ids:
        raise HTTPException(422, "Укажите хотя бы одного участника")
    if body.duration_minutes < 1 or body.duration_minutes > 480:
        raise HTTPException(422, "Длительность встречи должна быть от 1 до 480 минут")
    if body.days_ahead < 1 or body.days_ahead > 60:
        raise HTTPException(422, "Горизонт поиска должен быть от 1 до 60 дней")

    pids = list(dict.fromkeys(body.participant_ids))
    employees: list[Employee] = []
    for pid in pids:
        emp = db.get(Employee, pid)
        if emp is None:
            raise HTTPException(422, "Выберите доступного сотрудника из справочника")
        employees.append(emp)

    work_start = max(time.fromisoformat(e.work_start) for e in employees)
    work_end = min(time.fromisoformat(e.work_end) for e in employees)
    duration = timedelta(minutes=body.duration_minutes)

    s = get_settings()
    tz = s.tz
    now = s.now().astimezone(tz)
    pids_set = set(pids)

    slots: list[SlotOut] = []
    counted_days = 0
    day_offset = 0
    while counted_days < body.days_ahead and day_offset < body.days_ahead + 7:
        day = now.date() + timedelta(days=day_offset)
        day_offset += 1
        if day.weekday() >= 5:  # пятидневка: субботу и воскресенье не предлагаем
            continue
        day_start = datetime.combine(day, work_start, tzinfo=tz)
        day_end = datetime.combine(day, work_end, tzinfo=tz)
        if day == now.date() and now > day_start:
            base = now.replace(second=0, microsecond=0)
            rem = base.minute % 30
            if rem or now.second or now.microsecond:
                base += timedelta(minutes=30 - rem)
            day_start = max(day_start, base)
        if day_start + duration > day_end:
            continue
        counted_days += 1

        events = db.scalars(
            select(CalendarEvent)
            .where(CalendarEvent.starts_at < day_end, CalendarEvent.ends_at > day_start)
            .order_by(CalendarEvent.starts_at.asc())
        ).all()

        busy: list[tuple[datetime, datetime]] = []
        for ev in events:
            if pids_set.intersection(ev.attendee_ids or []):
                b_start = max(day_start, ev.starts_at.astimezone(tz))
                b_end = min(day_end, ev.ends_at.astimezone(tz))
                if b_start < b_end:
                    busy.append((b_start, b_end))
        busy.sort(key=lambda x: (x[0], x[1]))

        merged: list[tuple[datetime, datetime]] = []
        for b_start, b_end in busy:
            if merged and b_start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b_end))
            else:
                merged.append((b_start, b_end))

        free_windows: list[tuple[datetime, datetime]] = []
        cursor = day_start
        for b_start, b_end in merged:
            if cursor < b_start:
                free_windows.append((cursor, b_start))
            cursor = max(cursor, b_end)
        if cursor < day_end:
            free_windows.append((cursor, day_end))

        for f_start, f_end in free_windows:
            cur = f_start
            while cur + duration <= f_end:
                slots.append(SlotOut(starts_at=cur, ends_at=cur + duration))
                cur += duration

    return slots


def validate_create_event(
    db: Session,
    user: User,
    payload: dict,
    *,
    lock: bool = False,
    ignore_event_id: str | None = None,
):
    try:
        parsed = CreateEventPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "Некорректное содержимое карточки") from exc
    if not parsed.title.strip():
        raise HTTPException(422, "Название встречи не может быть пустым")
    try:
        ZoneInfo(parsed.timezone)
    except Exception as exc:
        raise HTTPException(422, "Некорректный часовой пояс") from exc
    validate_due(parsed.starts_at)
    validate_due(parsed.ends_at)
    if parsed.ends_at <= parsed.starts_at:
        raise HTTPException(422, "Время окончания должно быть позже времени начала")

    parsed.attendee_ids = list(dict.fromkeys(parsed.attendee_ids))
    sorted_ids = sorted(parsed.attendee_ids)
    emp_query = select(Employee).where(Employee.id.in_(sorted_ids)).order_by(Employee.id)
    if lock:
        emp_query = emp_query.with_for_update()
    found_emps = {e.id: e for e in db.scalars(emp_query).all()}
    if len(found_emps) != len(sorted_ids):
        raise HTTPException(422, "Выберите доступного сотрудника из справочника")

    meeting = get_meeting(db, user, parsed.meeting_id, lock=lock) if parsed.meeting_id else None
    # Прошедшую или отменённую встречу не переписываем: её дата, участники и расшифровка — история.
    # Для продолжения темы создаётся новая встреча (meeting_id=None).
    if meeting is not None and meeting.status != "planned":
        raise HTTPException(409, "Эта встреча уже прошла или отменена. Создайте новую встречу")

    ev_query = select(CalendarEvent).where(
        CalendarEvent.starts_at < parsed.ends_at,
        CalendarEvent.ends_at > parsed.starts_at,
    )
    if ignore_event_id is not None:
        ev_query = ev_query.where(CalendarEvent.id != ignore_event_id)
    if lock:
        ev_query = ev_query.with_for_update()
    overlapping = db.scalars(ev_query).all()
    attendee_set = set(parsed.attendee_ids)
    for ev in overlapping:
        if attendee_set.intersection(ev.attendee_ids or []):
            raise HTTPException(409, "Выбранный слот уже занят")

    return meeting, parsed.model_dump(mode="json")


def build_summary_payload(db: Session, user: User, protocol: Protocol) -> dict | None:
    meeting = db.get(Meeting, protocol.meeting_id)
    if meeting is None:
        return None
    tz = ZoneInfo(meeting.timezone or get_settings().company_tz)
    decisions = [i.statement for i in protocol.items if not i.removed and i.kind == "decision"]
    protocol_item_ids = {i.id for i in protocol.items if not i.removed and i.kind == "task"}
    tasks: list[Task] = []
    if protocol_item_ids:
        tasks = list(
            db.scalars(
                select(Task)
                .where(Task.meeting_id == protocol.meeting_id, Task.protocol_item_id.in_(protocol_item_ids))
                .order_by(Task.created_at.asc())
            ).all()
        )

    recipient_ids: list[str] = list(dict.fromkeys(meeting.participant_ids or []))
    for t in tasks:
        if t.owner_employee_id and t.owner_employee_id not in recipient_ids:
            recipient_ids.append(t.owner_employee_id)

    letters: list[SummaryLetter] = []
    for emp_id in recipient_ids:
        emp = db.get(Employee, emp_id)
        if emp is None or not emp.email:
            continue
        if user.role != ROLE_DIRECTOR and emp.department_id != user.department_id and emp.id not in (meeting.participant_ids or []):
            continue
        emp_tasks = [t for t in tasks if t.owner_employee_id == emp.id]
        lines = [
            f"Здравствуйте, {emp.full_name}!",
            "",
            f"Итоги встречи «{meeting.title}».",
        ]
        if protocol.summary.strip():
            lines.extend(["", f"Резюме: {protocol.summary.strip()}"])
        lines.append("")
        lines.append("Общие решения:")
        if decisions:
            for d in decisions:
                lines.append(f"- {d}")
        else:
            lines.append("- Зафиксированных общих решений нет")
        lines.append("")
        lines.append("Ваши поручения:")
        if emp_tasks:
            for t in emp_tasks:
                due_str = f" (срок: {t.due_at.astimezone(tz).strftime('%d.%m.%Y %H:%M')})" if t.due_at else ""
                lines.append(f"- {t.title}{due_str}")
        else:
            lines.append("- Личных поручений по итогам встречи нет")

        letters.append(
            SummaryLetter(
                recipient_id=emp.id,
                subject=f"Итоги встречи: {meeting.title}"[:300],
                body="\n".join(lines),
                task_ids=[t.id for t in emp_tasks],
            )
        )

    if not letters:
        return None
    return SendSummaryPayload(protocol_id=protocol.id, letters=letters).model_dump(mode="json")


_KPI_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("defect_rate", re.compile(r"(?<!\w)(?:брак\w*|defect_rate)(?!\w)", re.I)),
    (
        "orders_on_time",
        re.compile(
            r"(?<!\w)(?:срок(?:и|ов|ам|ами|ах)|в\s+срок(?!\s+(?:до|к)\b)|"
            r"срок\w*\s+(?:заказ\w*|отгруз\w*|сдач\w*|выполнен\w*|производств\w*|печат\w*|тираж\w*|доставк\w*|изготовлен\w*)|"
            r"просроч\w*\s+заказ\w*|orders_on_time)(?!\w)",
            re.I,
        ),
    ),
    ("support_sla", re.compile(r"(?<!\w)(?:sla|перв\w+\s+ответ\w*|support_sla)(?!\w)", re.I)),
]


def match_kpi_metric(text: str) -> str | None:
    """Определить показатель из kpi.CATALOG, если в формулировке решения явно говорится про брак, сроки или SLA."""
    for metric_id, pattern in _KPI_PATTERNS:
        if metric_id in kpi.CATALOG and pattern.search(text or ""):
            return metric_id
    return None


def _emp_tokens(emp: Employee) -> set[str]:
    tokens: set[str] = set()
    for s in [emp.full_name, *(emp.aliases or [])]:
        for w in re.findall(r"[а-яё]{3,}", s.casefold().replace("ё", "е")):
            tokens.add(w)
    return tokens


def _resolve_owner_in_text(text: str, employees: list[Employee]) -> str | None:
    body = text.split(":", 1)[1] if ":" in text else text
    for candidate_text in (body, text):
        norm = (candidate_text or "").lower().replace("ё", "е")
        matched: list[str] = []
        for emp in employees:
            if any(re.search(rf"(?<!\w){re.escape(name[: max(len(name) - 1, 3)])}\w*", norm) for name in _emp_tokens(emp)):
                matched.append(emp.id)
        if len(matched) == 1:
            return matched[0]
    return None


def build_decision_proposals(db: Session, user: User, protocol: Protocol) -> list[tuple[str, dict]]:
    """Предложить карточки create_decision для опубликованных пунктов «решение», явно связанных с KPI из kpi.CATALOG."""
    from .decisions import default_due
    from .delegation import extract_due

    meeting = db.get(Meeting, protocol.meeting_id)
    now = get_settings().now()
    ref_time = (meeting.starts_at if meeting and meeting.starts_at else now)
    tz_name = (meeting.timezone if meeting and meeting.timezone else get_settings().company_tz)

    emp_q = select(Employee)
    if user.role != ROLE_DIRECTOR:
        emp_q = emp_q.where(Employee.department_id == user.department_id)
    allowed_employees = list(db.scalars(emp_q.order_by(Employee.id.asc())).all())
    allowed_ids = {e.id for e in allowed_employees}
    if not allowed_employees:
        return []

    proposals: list[tuple[str, dict]] = []
    for item in protocol.items:
        if item.removed or item.kind != "decision" or item.decision_status == "proposed":
            continue
        metric_id = match_kpi_metric(item.statement) or match_kpi_metric(item.evidence_quote)
        if not metric_id:
            continue
        k = kpi.CATALOG.get(metric_id)
        if k is None or (k.director_only and user.role != ROLE_DIRECTOR):
            continue

        owner_id = item.owner_employee_id if item.owner_employee_id in allowed_ids else None
        if not owner_id and item.owner_raw:
            owner_id = resolve_owner(item.owner_raw, allowed_employees)
        if not owner_id:
            owner_id = (
                _resolve_owner_in_text(item.statement, allowed_employees)
                or _resolve_owner_in_text(item.evidence_quote, allowed_employees)
            )
        if not owner_id:
            for sib in protocol.items:
                if not sib.removed and sib.segment_id == item.segment_id and sib.owner_employee_id in allowed_ids:
                    owner_id = sib.owner_employee_id
                    break
        if not owner_id:
            if k.owner_employee_id in allowed_ids:
                owner_id = k.owner_employee_id
            elif user.employee_id in allowed_ids:
                owner_id = user.employee_id
            else:
                owner_id = allowed_employees[0].id

        due_at = item.due_at
        if due_at is None and item.due_raw:
            due_at = resolve_due(item.due_raw, ref_time, tz_name)
        if due_at is None:
            due_at = extract_due(item.statement, ref_time, tz_name)[0] or extract_due(item.evidence_quote, ref_time, tz_name)[0]
        if due_at is None:
            for sib in protocol.items:
                if not sib.removed and sib.segment_id == item.segment_id and sib.due_at is not None:
                    due_at = sib.due_at
                    break
        if due_at is None:
            due_at = default_due(now)
        due_at = due_at.astimezone(ZoneInfo(tz_name))

        start, end, _ = kpi.default_period(metric_id, now)
        val = kpi.compute(db, metric_id, start, end)
        status = kpi.status_of(k, val.value)
        cause_sources = [
            SourceRef(
                source_type="transcript",
                source_id=protocol.transcript_id,
                title=meeting.title if meeting else "Протокол встречи",
                segment_id=item.segment_id,
                quote=(item.evidence_quote or item.statement)[:240],
            )
        ]
        dec_payload = CreateDecisionPayload(
            title=f"{k.name}: {item.statement.strip()}"[:300],
            metric_id=metric_id,
            cause_hypothesis=f"Решение принято на встрече «{meeting.title}»" if meeting else "Решение из протокола встречи",
            cause_sources=cause_sources,
            action_text=item.statement.strip()[:1000],
            owner_employee_id=owner_id,
            due_at=due_at,
            priority="high" if status == "critical" else "medium",
            create_task=True,
        ).model_dump(mode="json")
        proposals.append((item.id, dec_payload))

    return proposals


def validate_send_summary(db: Session, user: User, payload: dict, *, lock: bool = False):
    try:
        parsed = SendSummaryPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "Некорректное содержимое карточки") from exc
    protocol = get_protocol(db, user, parsed.protocol_id, lock=lock)
    if protocol.status != "published":
        raise HTTPException(409, "Сначала опубликуйте протокол")
    meeting = db.get(Meeting, protocol.meeting_id)
    valid_task_ids = set(db.scalars(select(Task.id).where(Task.meeting_id == protocol.meeting_id)).all())

    for letter in parsed.letters:
        if not letter.subject.strip() or not letter.body.strip():
            raise HTTPException(422, "Тема и текст письма не могут быть пустыми")
        emp = db.get(Employee, letter.recipient_id)
        if emp is None or not emp.email:
            raise HTTPException(422, "Выберите доступного сотрудника из справочника")
        if user.role != ROLE_DIRECTOR and emp.department_id != user.department_id and emp.id not in (meeting.participant_ids or []):
            raise HTTPException(422, "Выберите доступного сотрудника из справочника")
        if any(tid not in valid_task_ids for tid in letter.task_ids):
            raise HTTPException(422, "Поручение в письме не относится к этой встрече")

    return protocol, parsed.model_dump(mode="json")


def validate_payload(db: Session, user: User, action_type: str, payload: dict, *, lock: bool = False):
    if action_type == "publish_protocol":
        return validate_publish(db, user, payload, lock=lock)
    if action_type == "update_task":
        try:
            parsed = UpdatePayload.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(422, "Некорректное содержимое карточки") from exc
        task = get_task(db, user, parsed.task_id, lock=lock)
        if payload_hash(task_snapshot(task)) != parsed.expected_hash:
            raise HTTPException(409, "Поручение изменилось. Создайте карточку заново")
        validate_changes(db, user, task, parsed.changes)
        return task, parsed.model_dump(mode="json", exclude_unset=True)
    if action_type == "create_event":
        return validate_create_event(db, user, payload, lock=lock)
    if action_type == "send_summary":
        return validate_send_summary(db, user, payload, lock=lock)
    if action_type == "delegate_task":
        return validate_delegate(db, user, payload, lock=lock)
    if action_type == "send_reminders":
        return notify.validate_reminders(db, user, payload, lock=lock)
    if action_type == "send_brief":
        return notify.validate_brief(db, user, payload, lock=lock)
    if action_type == "create_decision":
        from .decisions import validate_create

        return validate_create(db, user, payload, lock=lock)
    raise HTTPException(422, "Этот тип действия пока не поддерживается")


def validate_delegate(db: Session, user: User, payload: dict, *, lock: bool = False):
    """delegate_task: назначить владельца поручению без владельца (или поставить на контроль новое обещание из переписки)."""
    try:
        parsed = DelegateTaskPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "Некорректное содержимое карточки") from exc
    if parsed.expected_hash == "new":
        existing = db.get(Task, parsed.task_id)
        if existing is not None:
            raise HTTPException(409, "Поручение уже создано")
        task = None
    else:
        task = get_task(db, user, parsed.task_id, lock=lock)
        if payload_hash(task_snapshot(task)) != parsed.expected_hash:
            raise HTTPException(409, "Поручение изменилось. Создайте карточку заново")
        if task.status in {"done", "cancelled"}:
            raise HTTPException(409, "Поручение уже закрыто")
    if not parsed.reason.strip():
        raise HTTPException(422, "Укажите основание выбора исполнителя")
    validate_owner(db, user, parsed.owner_employee_id)
    validate_due(parsed.due_at)
    return task, parsed.model_dump(mode="json")


def can_access_action(db: Session, user: User, action: ActionProposal) -> bool:
    if action.created_by != user.id:
        return False
    if action.type in {"send_reminders", "send_brief", "create_decision"}:
        return True  # содержимое перепроверяется при каждом исполнении (права на каждое поручение)
    if action.type in {"publish_protocol", "send_summary"}:
        return db.scalar(select(Protocol.id).join(Meeting, Protocol.meeting_id == Meeting.id).where(
            Protocol.id == action.payload.get("protocol_id"), meeting_filter(user))) is not None
    if action.type in {"update_task", "delegate_task"}:
        if action.type == "delegate_task" and action.payload.get("expected_hash") == "new":
            return True
        return db.scalar(select(Task.id).where(Task.id == action.payload.get("task_id"), task_filter(user))) is not None
    if action.type == "create_event":
        meeting_id = action.payload.get("meeting_id") or (action.result or {}).get("meeting_id")
        if meeting_id:
            return db.scalar(select(Meeting.id).where(Meeting.id == meeting_id, meeting_filter(user))) is not None
        event_id = (action.result or {}).get("event_id")
        if event_id:
            ev = db.get(CalendarEvent, event_id)
            return ev is not None and can_access_event(db, user, ev)
        return True
    return False


def get_action(db: Session, user: User, action_id: str, *, lock: bool = False) -> ActionProposal:
    query = select(ActionProposal).where(ActionProposal.id == action_id)
    if lock:
        query = query.with_for_update()
    action = db.scalar(query)
    if action is None or not can_access_action(db, user, action):
        deny(db, user, "action", action_id)
    return action


def action_out(action: ActionProposal) -> ActionOut:
    out = ActionOut.model_validate(action)
    if action.type == "publish_protocol":
        included = [i for i in action.payload["items"] if not i["removed"]]
        out.preview = {"title": "Опубликовать протокол и создать поручения", "protocol_id": action.payload["protocol_id"],
                       "items": included, "task_count": sum(i["kind"] == "task" for i in included)}
    elif action.type == "create_event":
        out.preview = {
            "title": f"Назначить встречу «{action.payload['title']}» и отправить приглашения",
            **action.payload,
            "attendee_count": len(action.payload.get("attendee_ids", [])),
            "agenda_count": len(action.payload.get("agenda", [])),
        }
    elif action.type == "send_summary":
        letters = action.payload.get("letters", [])
        out.preview = {
            "title": "Отправить итоги встречи и поручения участникам",
            "protocol_id": action.payload["protocol_id"],
            "letters": letters,
            "recipient_count": len(letters),
            "task_count": sum(len(l.get("task_ids", [])) for l in letters),
        }
    elif action.type == "send_reminders":
        letters = action.payload.get("letters", [])
        out.preview = {"title": "Отправить напоминания о просроченных поручениях", "letters": letters,
                       "recipient_count": len(letters), "task_count": sum(len(l.get("task_ids", [])) for l in letters)}
    elif action.type == "create_decision":
        out.preview = {"title": "Зафиксировать решение и поручить исполнение", **action.payload}
    elif action.type == "send_brief":
        out.preview = {"title": "Отправить утренний брифинг себе на почту", **action.payload}
    elif action.type == "delegate_task":
        p = action.payload
        name = _task_title(action, p.get("task_id"))
        out.preview = {"title": f"Поручить «{name}»" if name else "Поручить исполнителю", **p,
                       "evidence": (action.result or {}).get("evidence")}
    else:
        name = _task_title(action, action.payload.get("task_id"))
        out.preview = {"title": f"Изменить поручение «{name}»" if name else "Изменить поручение", **action.payload,
                       "evidence": (action.result or {}).get("evidence")}
    return out


def _task_title(action: ActionProposal, task_id: str | None) -> str | None:
    """Название поручения для заголовка карточки (права на поручение уже проверены при создании и чтении карточки)."""
    db = object_session(action)
    task = db.get(Task, task_id) if db is not None and task_id else None
    if task is not None:
        return task.title
    return (action.result or {}).get("proposed_title") or (action.payload.get("expected_result") or "").strip() or None


def create_action(
    db: Session,
    user: User,
    action_type: str,
    payload: dict,
    *,
    key: str | None = None,
    origin: str = "user",
    commit: bool = True,
) -> ActionProposal:
    _, payload = validate_payload(db, user, action_type, payload)
    key = key or new_id("change")
    previous = db.scalar(select(ActionProposal).where(ActionProposal.idempotency_key == key))
    if previous is not None:
        if previous.status == "pending":
            return previous
        # После отмены пользователь вправе создать новую карточку того же черновика.
        key = new_id("retry")
    action = ActionProposal(id=new_id("action"), type=action_type, payload=payload,
                            payload_hash=payload_hash(payload), status="pending", origin=origin,
                            revision=1, created_by=user.id, idempotency_key=key)
    db.add(action)
    audit.write(db, user, "action.propose", object_type="action", object_id=action.id,
                result="proposed", details={"type": action_type}, commit=False)
    if commit:
        db.commit()
    else:
        db.flush()
    return action


def _execute_create_event(db: Session, user: User, action: ActionProposal):
    existing_job = db.scalar(select(DeliveryJob).where(DeliveryJob.action_id == action.id).with_for_update())
    if existing_job is not None and existing_job.event_id is not None:
        _, payload = validate_create_event(db, user, action.payload, lock=True, ignore_event_id=existing_job.event_id)
        parsed = CreateEventPayload.model_validate(payload)
        event = db.get(CalendarEvent, existing_job.event_id)
        meeting_id = event.meeting_id
        job = existing_job
        action.status = "executing"
        db.flush()
    else:
        target_meeting, payload = validate_create_event(db, user, action.payload, lock=True)
        parsed = CreateEventPayload.model_validate(payload)
        action.status = "executing"
        action.confirmed_by = user.id
        action.confirmed_at = datetime.now(timezone.utc)
        db.flush()

        if target_meeting is None:
            target_meeting = Meeting(
                id=new_id("m"),
                title=parsed.title,
                topic=parsed.topic or parsed.title,
                starts_at=parsed.starts_at,
                ends_at=parsed.ends_at,
                timezone=parsed.timezone,
                organizer_user_id=user.id,
                department_id=user.department_id,
                participant_ids=parsed.attendee_ids,
                status="planned",
                agenda=[a.model_dump(mode="json") for a in parsed.agenda],
            )
            db.add(target_meeting)
            db.flush()
        else:
            target_meeting.title = parsed.title
            if parsed.topic:
                target_meeting.topic = parsed.topic
            target_meeting.starts_at = parsed.starts_at
            target_meeting.ends_at = parsed.ends_at
            target_meeting.timezone = parsed.timezone
            target_meeting.participant_ids = parsed.attendee_ids
            if parsed.agenda:
                target_meeting.agenda = [a.model_dump(mode="json") for a in parsed.agenda]
            db.flush()

        event = CalendarEvent(
            id=new_id("ev"),
            title=parsed.title,
            starts_at=parsed.starts_at,
            ends_at=parsed.ends_at,
            timezone=parsed.timezone,
            attendee_ids=parsed.attendee_ids,
            meeting_id=target_meeting.id,
            source="fokus",
        )
        db.add(event)
        db.flush()

        recipients = [db.get(Employee, eid).email for eid in parsed.attendee_ids]
        job = DeliveryJob(
            id=new_id("dj"),
            action_id=action.id,
            event_id=event.id,
            channel="email",
            recipients=recipients,
            subject=f"Приглашение: {parsed.title}"[:300],
            status="queued",
            attempts=0,
        )
        db.add(job)
        db.flush()
        meeting_id = target_meeting.id

    org_emp = db.get(Employee, user.employee_id) if user.employee_id else None
    organizer_email = org_emp.email if org_emp else get_settings().mail_from
    msg = mailer.build_invite_email(
        event_id=event.id,
        payload=parsed,
        organizer_email=organizer_email,
        recipient_emails=job.recipients,
    )
    job.attempts += 1
    job.updated_at = datetime.now(timezone.utc)
    try:
        msg_id = send_message(msg)
        job.status = "sent"
        job.last_error = None
        job.message_id = msg_id
        action.status = "executed"
        action.error = None
        action.result = {
            "event_id": event.id,
            "meeting_id": meeting_id,
            "delivery_job_id": job.id,
            "delivery_status": "sent",
        }
        audit.write(
            db,
            user,
            "action.confirm",
            object_type="action",
            object_id=action.id,
            result="confirmed",
            details={"type": action.type, "revision": action.revision, "event_id": event.id, "delivery_status": "sent"},
            commit=False,
        )
    except Exception as exc:
        job.status = "failed"
        job.last_error = str(exc)
        action.status = "partially_failed"
        action.error = f"Событие создано, но отправка приглашений не удалась: {exc}"
        action.result = {
            "event_id": event.id,
            "meeting_id": meeting_id,
            "delivery_job_id": job.id,
            "delivery_status": "failed",
        }
        audit.write(
            db,
            user,
            "action.confirm",
            object_type="action",
            object_id=action.id,
            result="error",
            details={"type": action.type, "revision": action.revision, "event_id": event.id, "delivery_status": "failed"},
            commit=False,
        )
    db.commit()


def _execute_send_summary(db: Session, user: User, action: ActionProposal):
    protocol, payload = validate_send_summary(db, user, action.payload, lock=True)
    parsed = SendSummaryPayload.model_validate(payload)
    action.status = "executing"
    if action.confirmed_by is None:
        action.confirmed_by = user.id
        action.confirmed_at = datetime.now(timezone.utc)
    db.flush()

    job = db.scalar(select(DeliveryJob).where(DeliveryJob.action_id == action.id).with_for_update())
    if job is None:
        recipients = [db.get(Employee, l.recipient_id).email for l in parsed.letters]
        subject = parsed.letters[0].subject if len(parsed.letters) == 1 else f"Итоги встречи ({len(parsed.letters)} писем)"
        job = DeliveryJob(
            id=new_id("dj"),
            action_id=action.id,
            event_id=None,
            channel="email",
            recipients=recipients,
            subject=subject[:300],
            status="queued",
            attempts=0,
        )
        db.add(job)
        db.flush()

    already_sent = list((action.result or {}).get("sent_recipient_ids", []))
    msg_ids = list((action.result or {}).get("message_ids", []))
    job.attempts += 1
    job.updated_at = datetime.now(timezone.utc)
    try:
        for letter in parsed.letters:
            if letter.recipient_id in already_sent:
                continue
            emp = db.get(Employee, letter.recipient_id)
            msg = mailer.build_summary_email(recipient_email=emp.email, subject=letter.subject, body=letter.body)
            mid = send_message(msg)
            already_sent.append(letter.recipient_id)
            msg_ids.append(mid)

        job.status = "sent"
        job.last_error = None
        job.message_id = msg_ids[-1] if msg_ids else None
        action.status = "executed"
        action.error = None
        action.result = {
            "protocol_id": protocol.id,
            "delivery_job_id": job.id,
            "delivery_status": "sent",
            "sent_count": len(already_sent),
            "sent_recipient_ids": already_sent,
            "message_ids": msg_ids,
        }
        audit.write(
            db,
            user,
            "action.confirm",
            object_type="action",
            object_id=action.id,
            result="confirmed",
            details={"type": action.type, "revision": action.revision, "protocol_id": protocol.id, "sent_count": len(already_sent)},
            commit=False,
        )
    except Exception as exc:
        job.status = "failed"
        job.last_error = str(exc)
        action.status = "partially_failed" if already_sent else "failed"
        action.error = f"Не удалось отправить письма с итогами: {exc}"
        action.result = {
            "protocol_id": protocol.id,
            "delivery_job_id": job.id,
            "delivery_status": "failed",
            "sent_count": len(already_sent),
            "sent_recipient_ids": already_sent,
            "message_ids": msg_ids,
        }
        audit.write(
            db,
            user,
            "action.confirm",
            object_type="action",
            object_id=action.id,
            result="error",
            details={"type": action.type, "revision": action.revision, "protocol_id": protocol.id, "sent_count": len(already_sent)},
            commit=False,
        )
    db.commit()


def execute(db: Session, user: User, action: ActionProposal):
    if action.type == "create_event":
        _execute_create_event(db, user, action)
        return
    if action.type == "send_summary":
        _execute_send_summary(db, user, action)
        return
    if action.type == "create_decision":
        from .decisions import execute_create

        action.status, action.confirmed_by, action.confirmed_at = "executing", user.id, datetime.now(timezone.utc)
        db.flush()
        action.result = execute_create(db, user, action)
        action.status = "executed"
        audit.write(db, user, "action.confirm", object_type="action", object_id=action.id, result="confirmed",
                    details={"type": action.type, "revision": action.revision, **action.result}, commit=False)
        db.commit()
        return
    if action.type in {"send_reminders", "send_brief"}:
        _, payload = validate_payload(db, user, action.type, action.payload, lock=True)
        letters = ([(l["recipient_id"], l["subject"], l["body"]) for l in payload["letters"]]
                   if action.type == "send_reminders" else [(payload["recipient_id"], payload["subject"], payload["body"])])
        notify.execute_letters(db, user, action, letters)
        return

    target, payload = validate_payload(db, user, action.type, action.payload, lock=True)
    action.status = "executing"
    action.confirmed_by = user.id
    action.confirmed_at = datetime.now(timezone.utc)
    db.flush()
    if action.type == "publish_protocol":
        items = {i.id: i for i in target.items}
        tasks = []
        meeting = db.get(Meeting, target.meeting_id)
        for snapshot in payload["items"]:
            item = items[snapshot["id"]]
            parsed = ProtocolItemOut.model_validate(snapshot)
            for field in ("kind", "statement", "decision_status", "owner_employee_id", "owner_origin", "due_at", "due_origin", "needs_clarification", "removed"):
                setattr(item, field, getattr(parsed, field))
            if not item.removed and item.kind == "task":
                task = Task(id=new_id("task"), title=item.statement, owner_employee_id=item.owner_employee_id,
                            owner_origin=item.owner_origin, due_at=item.due_at, due_origin=item.due_origin,
                            status="assigned", meeting_id=target.meeting_id, protocol_item_id=item.id,
                            department_id=meeting.department_id, created_by=user.id)
                db.add(task)
                tasks.append(task.id)
        target.status = "published"
        target.revision += 1
        target.published_at = datetime.now(timezone.utc)
        db.flush()
        summary_payload = build_summary_payload(db, user, target)
        if summary_payload is not None:
            summary_key = payload_hash({"user": user.id, "send_summary": target.id})
            create_action(db, user, "send_summary", summary_payload, key=summary_key, origin="ai", commit=False)
        decision_action_ids: list[str] = []
        for item_id, dec_payload in build_decision_proposals(db, user, target):
            dec_key = payload_hash({"user": user.id, "protocol_decision": target.id, "item": item_id})
            dec_action = create_action(db, user, "create_decision", dec_payload, key=dec_key, origin="ai", commit=False)
            decision_action_ids.append(dec_action.id)
        action.result = {"protocol_id": target.id, "task_ids": tasks}
        if decision_action_ids:
            action.result["decision_action_ids"] = decision_action_ids
    elif action.type == "delegate_task":
        parsed = DelegateTaskPayload.model_validate(payload)
        origin_kind = "suggested" if action.origin == "ai" else "user_entered"
        if target is None:
            emp = db.get(Employee, parsed.owner_employee_id)
            title = (action.result or {}).get("proposed_title") or parsed.expected_result.strip() or parsed.reason[:200]
            target = Task(
                id=parsed.task_id,
                title=title[:500],
                expected_result=parsed.expected_result,
                owner_employee_id=parsed.owner_employee_id,
                owner_origin=origin_kind,
                due_at=parsed.due_at,
                due_origin=origin_kind if parsed.due_at else "unknown",
                status="assigned",
                department_id=(emp.department_id if emp else None) or user.department_id,
                delegation_reason=parsed.reason,
                created_by=user.id,
            )
            db.add(target)
            db.flush()
        else:
            target.owner_employee_id = parsed.owner_employee_id
            target.owner_origin = origin_kind
            target.delegation_reason = parsed.reason
            if parsed.due_at is not None and parsed.due_at != target.due_at:
                target.due_at = parsed.due_at
                target.due_origin = origin_kind
            if parsed.expected_result.strip():
                target.expected_result = parsed.expected_result
        action.result = {**(action.result or {}), "task_id": target.id, "owner_employee_id": target.owner_employee_id}
    else:
        changes = TaskChangeIn.model_validate(payload["changes"])
        for field in changes.model_fields_set:
            setattr(target, field, getattr(changes, field))
        if "owner_employee_id" in changes.model_fields_set:
            target.owner_origin = "user_entered" if target.owner_employee_id else "unknown"
        if "due_at" in changes.model_fields_set:
            target.due_origin = "user_entered" if target.due_at else "unknown"
        if target.status == "done" and target.completed_at is None:
            target.completed_at = get_settings().now()
        action.result = {**(action.result or {}), "task_id": target.id}
    action.status = "executed"
    audit.write(db, user, "action.confirm", object_type="action", object_id=action.id,
                result="confirmed", details={"type": action.type, "revision": action.revision}, commit=False)
    db.commit()
