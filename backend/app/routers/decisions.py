"""Контур решений: разбор отклонения KPI → карточка решения (подтверждение) → повторная проверка."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, kpi
from ..actions import action_out, create_action, payload_hash
from ..config import get_settings
from ..db import get_db
from ..decisions import decision_filter, decision_out, default_due, get_decision, recheck
from ..llm import LLMError
from ..models import ROLE_DIRECTOR, Decision, Employee, User
from ..pipelines.deviation import analyse
from ..schemas import ActionOut, DecisionOut, MetricAnalysisOut
from ..security import current_user

router = APIRouter(tags=["decisions"])


@router.get("/metrics/{metric_id}/analysis", response_model=MetricAnalysisOut)
async def metric_analysis(metric_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        out = await analyse(db, user, metric_id)
    except LLMError:
        audit.write(db, user, "metric.analysis", object_type="metric", object_id=metric_id, result="error")
        raise
    audit.write(db, user, "metric.analysis", object_type="metric", object_id=metric_id,
                details={"route": out.route, "evidence": len(out.evidence), "top": out.top_contributor})
    return out


@router.post("/metrics/{metric_id}/decision-proposal", response_model=ActionOut)
async def decision_proposal(metric_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Карточка решения из разбора отклонения: ассистент заполняет, руководитель правит и подтверждает."""
    try:
        a = await analyse(db, user, metric_id)
    except LLMError:
        audit.write(db, user, "metric.analysis", object_type="metric", object_id=metric_id, result="error")
        raise
    k = kpi.CATALOG[metric_id]
    now = get_settings().now()
    start, end, _ = kpi.default_period(metric_id, now)
    v = kpi.compute(db, metric_id, start, end)
    if v.value is None:
        raise HTTPException(422, "Нет данных за период — решение по показателю не на чем основать")
    if k.higher_is_better:
        gap = round(k.plan / 100 * v.denominator - v.numerator)
        impact = f"{v.denominator - v.numerator} из {v.denominator} вне нормы; до плана не хватает ≈{max(gap, 0)} за период {a.metric.period}"
    else:
        gap = round(v.numerator - k.plan / 100 * v.denominator)
        impact = f"{v.numerator} из {v.denominator} вне нормы; сверх плана ≈{max(gap, 0)} за период {a.metric.period}"
    owner = db.get(Employee, k.owner_employee_id)
    if owner is None or (user.role != ROLE_DIRECTOR and owner.department_id != user.department_id):
        owner = db.get(Employee, user.employee_id) if user.employee_id else None
    if owner is None:
        raise HTTPException(422, "Не удалось определить ответственного — укажите вручную")
    hyp = a.hypothesis
    payload = {"title": f"{k.name}: {kpi.fmt(v.value, k.unit)} при плане {kpi.fmt(k.plan, k.unit)}", "metric_id": metric_id, "impact": impact,
               "cause_hypothesis": hyp.text if hyp else "", "cause_sources": [s.model_dump(mode="json") for s in (hyp.sources if hyp and hyp.sources else a.evidence[:2])],
               "action_text": a.suggested_action or f"Разобрать причины отклонения «{k.name}» и устранить их",
               "owner_employee_id": owner.id, "due_at": default_due(now).isoformat(),
               "priority": "high" if a.metric.status == "critical" else "medium", "create_task": True}
    key = payload_hash({"user": user.id, "decision": metric_id, "day": now.date().isoformat(), "value": v.value})
    return action_out(create_action(db, user, "create_decision", payload, key=key, origin="ai"))


@router.get("/decisions", response_model=list[DecisionOut])
def decisions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Decision).where(decision_filter(user)).order_by(Decision.created_at.desc())).all()
    return [decision_out(d) for d in rows]


@router.get("/decisions/{decision_id}", response_model=DecisionOut)
def decision(decision_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return decision_out(get_decision(db, user, decision_id))


@router.post("/decisions/{decision_id}/recheck", response_model=DecisionOut)
def decision_recheck(decision_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Повторная проверка — расчёт кодом, не внешнее действие: подтверждение не требуется, пишется в аудит."""
    return decision_out(recheck(db, user, get_decision(db, user, decision_id, lock=True)))
