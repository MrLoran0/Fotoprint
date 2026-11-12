"""Подготовка встречи (С1, Д1), делегирование (Д2), «Мой день» (С6), напоминания и утренний брифинг (P1)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, delegation, kpi
from ..access import get_task, task_filter
from ..actions import action_out, create_action, payload_hash, task_snapshot
from ..config import get_settings
from ..db import get_db
from ..llm import LLMError
from ..models import Employee, Task, User
from ..myday import my_day
from ..pipelines.prep import prepare
from ..schemas import ActionOut, MeetingPrepOut, MyDayOut, PrepareIn
from ..security import current_user

router = APIRouter(tags=["planning"])


@router.post("/meetings/prepare", response_model=MeetingPrepOut)
async def meetings_prepare(body: PrepareIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        out = await prepare(db, user, body)
    except LLMError:
        audit.write(
            db,
            user,
            "meeting.prepare",
            object_type="meeting",
            object_id=body.previous_meeting_id or "",
            result="error",
            details={"topic": body.topic, "participant_ids": list(body.participant_ids)},
        )
        raise
    audit.write(db, user, "meeting.prepare", object_type="meeting", object_id=out.meeting_id,
                details={"route": out.route, "model": out.model, "agenda": len(out.agenda),
                         "returned": sum(1 for a in out.agenda if a.returned_task_id)})
    return out


class DelegateIn(BaseModel):
    task_id: str


@router.post("/delegation/propose", response_model=ActionOut)
def delegation_propose(body: DelegateIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = get_task(db, user, body.task_id)
    if task.owner_employee_id:
        raise HTTPException(409, "У поручения уже есть владелец")
    action = delegation.propose(db, user, task)
    if action is None:
        raise HTTPException(422, "Нет подходящего кандидата: укажите исполнителя вручную")
    return action_out(action)


@router.get("/my-day", response_model=MyDayOut)
def get_my_day(user: User = Depends(current_user), db: Session = Depends(get_db)) -> MyDayOut:
    out = my_day(db, user)
    audit.write(db, user, "my_day.view", details={"needs": len(out.needs_decision), "suggestions": len(out.delegation_suggestions)})
    return out


@router.post("/reminders/propose", response_model=ActionOut)
def reminders_propose(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Пакет напоминаний: каждому владельцу просроченных поручений — одно письмо. Отправка — после подтверждения."""
    now = get_settings().now()
    tz = get_settings().tz
    overdue = db.scalars(select(Task).where(task_filter(user), Task.status.in_(("assigned", "accepted", "in_progress")),
                                            Task.due_at < now, Task.owner_employee_id.is_not(None))
                         .order_by(Task.owner_employee_id, Task.due_at)).all()
    by_owner: dict[str, list[Task]] = {}
    for t in overdue:
        by_owner.setdefault(t.owner_employee_id, []).append(t)
    letters = []
    for owner_id, tasks in by_owner.items():
        emp = db.get(Employee, owner_id)
        if emp is None or not emp.email:
            continue
        lines = [f"{emp.full_name}, добрый день!", "", "Напоминаем о просроченных поручениях:"]
        lines += [f"- {t.title} — срок был {t.due_at.astimezone(tz):%d.%m.%Y %H:%M}" for t in tasks]
        lines += ["", "Если срок нужно перенести или поручение передать — ответьте на это письмо с новым сроком."]
        letters.append({"recipient_id": owner_id, "subject": f"Напоминание: просроченные поручения ({len(tasks)})",
                        "body": "\n".join(lines), "task_ids": [t.id for t in tasks]})
    if not letters:
        raise HTTPException(422, "Нет просроченных поручений с владельцем")
    key = payload_hash({"user": user.id, "reminders": [task_snapshot(t) | {"id": t.id} for t in overdue]})
    return action_out(create_action(db, user, "send_reminders", {"letters": letters}, key=key, origin="ai"))


def brief_text(user: User, day: MyDayOut) -> str:
    tz = get_settings().tz
    lines = [f"{user.full_name}, «Мой день» на {day.now.astimezone(tz):%d.%m.%Y %H:%M}", ""]
    lines.append("Показатели:")
    for m in day.metrics:
        value = kpi.fmt(m.value, m.unit)
        mark = {"ok": "в норме", "warning": "внимание", "critical": "критично", "no_data": "нет данных"}[m.status]
        lines.append(f"- {m.name}: {value} (план {kpi.fmt(m.plan, m.unit)}) — {mark}{'; данные устарели' if m.stale else ''}")
    if day.needs_decision:
        lines += ["", "Требует вашего решения:"] + [f"- {n.title}" for n in day.needs_decision[:7]]
    if day.delegation_suggestions:
        lines += ["", f"Предлагается делегировать: {len(day.delegation_suggestions)} поручений — подтвердите в AI-ассистенте."]
    if day.task_results:
        lines += ["", "Выполнено за неделю:"] + [f"- {t.title}: {t.result}" for t in day.task_results[:5]]
    if day.upcoming_meetings:
        lines += ["", "Ближайшие встречи:"] + [
            f"- {b.starts_at.astimezone(tz):%d.%m %H:%M} {b.title}" for b in day.upcoming_meetings[:5] if b.starts_at]
    lines += ["", "Все цифры посчитаны по данным источников; формулы и свежесть — в AI-ассистенте."]
    return "\n".join(lines)


@router.post("/my-day/brief", response_model=ActionOut)
def my_day_brief(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Утренний брифинг себе на почту: текст готовит код из «Моего дня», отправка — после подтверждения."""
    if not user.employee_id:
        raise HTTPException(422, "У учётной записи нет почты сотрудника")
    day = my_day(db, user)
    payload = {"recipient_id": user.employee_id, "subject": f"«Мой день» — {day.now.astimezone(get_settings().tz):%d.%m.%Y}",
               "body": brief_text(user, day)}
    key = payload_hash({"user": user.id, "brief": payload})
    return action_out(create_action(db, user, "send_brief", payload, key=key, origin="ai"))
