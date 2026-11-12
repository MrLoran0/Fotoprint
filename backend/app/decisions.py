"""Реестр решений (трек 1, «главный критерий»: решение назначено, сохранено и проверено повторно).

create_decision — карточка через движок подтверждений; базовое значение KPI фиксирует сервер при исполнении.
Повторная проверка — расчёт кодом «до/после» и вердикт: effective (план достигнут), partial (стало лучше,
но плана нет), ineffective (не лучше), no_data (данных после решения нет).
"""

from datetime import datetime, timedelta

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, select, true
from sqlalchemy.orm import Session

from . import audit, kpi
from .config import get_settings
from .models import ROLE_DIRECTOR, Decision, Employee, Task, User, new_id
from .schemas import CreateDecisionPayload, DecisionOut


def decision_filter(user: User):
    if user.role == ROLE_DIRECTOR:
        return true()
    return and_(Decision.department_id.is_not(None), Decision.department_id == user.department_id)


def get_decision(db: Session, user: User, decision_id: str, *, lock: bool = False) -> Decision:
    q = select(Decision).where(Decision.id == decision_id, decision_filter(user))
    if lock:
        q = q.with_for_update()
    d = db.scalar(q)
    if d is None:
        audit.write(db, user, "decision.access", object_type="decision", object_id=decision_id, result="denied")
        raise HTTPException(403, "Решение недоступно")
    return d


def decision_out(d: Decision) -> DecisionOut:
    out = DecisionOut.model_validate(d)
    out.check_due = d.status == "in_progress" and d.due_at is not None and d.due_at <= get_settings().now()
    return out


def validate_create(db: Session, user: User, payload: dict, *, lock: bool = False):
    try:
        parsed = CreateDecisionPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "Некорректное содержимое карточки решения") from exc
    k = kpi.CATALOG.get(parsed.metric_id) if parsed.metric_id else None
    if parsed.metric_id and (k is None or (k.director_only and user.role != ROLE_DIRECTOR)):
        raise HTTPException(422, "Показатель недоступен")
    emp = db.get(Employee, parsed.owner_employee_id)
    if emp is None or (user.role != ROLE_DIRECTOR and emp.department_id != user.department_id):
        raise HTTPException(422, "Выберите ответственного из своего подразделения")
    if parsed.due_at.tzinfo is None:
        raise HTTPException(422, "Срок должен содержать часовой пояс")
    from_protocol = any(s.source_type == "transcript" for s in parsed.cause_sources)
    if not from_protocol and parsed.due_at <= get_settings().now():
        raise HTTPException(422, "Срок решения должен быть в будущем")
    return None, parsed.model_dump(mode="json")


def baseline(db: Session, metric_id: str, now: datetime) -> dict:
    start, end, period = kpi.default_period(metric_id, now)
    v = kpi.compute(db, metric_id, start, end)
    k = kpi.CATALOG[metric_id]
    return {"value": v.value, "numerator": v.numerator, "denominator": v.denominator, "period": period,
            "start": start.isoformat(), "end": end.isoformat(), "plan": k.plan, "unit": k.unit, "computed_at": now.isoformat()}


def execute_create(db: Session, user: User, action) -> dict:
    _, payload = validate_create(db, user, action.payload, lock=True)
    p = CreateDecisionPayload.model_validate(payload)
    now = get_settings().now()
    d = Decision(id=new_id("dec"), title=p.title, metric_id=p.metric_id, impact=p.impact, cause_hypothesis=p.cause_hypothesis,
                 cause_sources=[s.model_dump(mode="json") for s in p.cause_sources], action_text=p.action_text,
                 owner_employee_id=p.owner_employee_id, due_at=p.due_at, priority=p.priority, status="in_progress",
                 baseline=baseline(db, p.metric_id, now) if p.metric_id else None, department_id=user.department_id,
                 created_by=user.id, effective_from=now,
                 history=[{"at": now.isoformat(), "user_id": user.id, "event": "создано", "detail": p.title}])
    db.add(d)
    db.flush()
    if p.create_task:
        t = Task(id=new_id("task"), title=p.action_text[:500], expected_result=f"Решение: {p.title}"[:1000],
                 owner_employee_id=p.owner_employee_id, owner_origin="user_entered", due_at=p.due_at, due_origin="user_entered",
                 status="assigned", department_id=user.department_id, decision_id=d.id, created_by=user.id,
                 delegation_reason="поручение по решению")
        db.add(t)
        db.flush()
        d.task_ids = [t.id]
    return {"decision_id": d.id, "task_ids": d.task_ids}


def recheck(db: Session, user: User, d: Decision) -> Decision:
    """Повторная проверка: KPI за период после решения против базового значения."""
    if not d.metric_id or not d.baseline:
        raise HTTPException(422, "У решения нет показателя для проверки")
    now = get_settings().now()
    k = kpi.CATALOG[d.metric_id]
    since = (d.effective_from or d.created_at).astimezone(get_settings().tz).date()
    after = kpi.compute(db, d.metric_id, since, now.date())
    before = d.baseline.get("value")
    if after.value is None or before is None:
        verdict = "no_data"
    elif kpi.status_of(k, after.value) == "ok":
        verdict = "effective"
    elif kpi.better(k, after.value, before):
        verdict = "partial"
    else:
        verdict = "ineffective"
    d.recheck = {"value": after.value, "numerator": after.numerator, "denominator": after.denominator,
                 "period": f"{since:%d.%m}–{now:%d.%m.%Y}", "verdict": verdict, "plan": k.plan, "unit": k.unit,
                 "delta": None if after.value is None or before is None else round(after.value - before, 1),
                 "checked_at": now.isoformat(),
                 # честно: вердикт по маленькой выборке — предварительный
                 "low_sample": bool(d.baseline.get("denominator")) and after.denominator < 0.3 * d.baseline["denominator"],
                 "baseline_denominator": d.baseline.get("denominator")}
    d.status = verdict
    d.history = [*(d.history or []), {"at": now.isoformat(), "user_id": user.id, "event": "повторная проверка",
                                      "detail": f"{before}{k.unit} → {after.value}{k.unit}: {verdict}"}]
    audit.write(db, user, "decision.recheck", object_type="decision", object_id=d.id, result=verdict,
                details={"before": before, "after": after.value}, commit=False)
    db.commit()
    return d


VERDICT_RU = {"effective": "сработало — план достигнут", "partial": "частично — стало лучше, но плана нет",
              "ineffective": "не сработало — показатель не улучшился", "no_data": "нет данных после решения"}


def default_due(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=7), datetime.min.time(), tzinfo=now.tzinfo).replace(hour=18)

