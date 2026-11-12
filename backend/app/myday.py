"""С6 «Мой день»: что требует решения, кому делегировать, что выполнено, ближайшие встречи и KPI.

Все цифры считает код по описанным формулам; «нет данных» при нулевом знаменателе; свежесть — по data_source.
Директор видит компанию, руководитель подразделения — своё подразделение (поручения, встречи, свои KPI).
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import delegation, kpi
from .actions import action_out, can_access_action
from .config import get_settings
from .models import (
    ROLE_DIRECTOR,
    ActionProposal,
    Decision,
    CalendarEvent,
    Employee,
    Meeting,
    SourceChannel,
    Task,
    User,
)
from .access import meeting_filter, task_filter
from .pipelines.prep import OPEN, task_out
from .routers.sources import channel_visible
from .schemas import DecisionNeeded, MeetingBrief, MetricOut, MyDayOut


def metric_out(db: Session, k: kpi.KpiDef, now: datetime) -> MetricOut:
    start, end, period = kpi.default_period(k.id, now)
    v = kpi.compute(db, k.id, start, end)
    name, updated, stale = kpi.source_info(db, k, now)
    return MetricOut(id=k.id, name=k.name, value=v.value, unit=k.unit, plan=k.plan, period=period, formula=k.formula,
                     source_id=k.source_id, source_name=name, updated_at=updated, stale=stale, status=kpi.status_of(k, v.value))


def metric_overdue(overdue: list[Task], now: datetime) -> MetricOut:
    n = len(overdue)
    return MetricOut(id="overdue_tasks", name="Просроченные поручения", value=float(n), unit="шт", plan=0.0,
                     period=f"на {now:%d.%m.%Y %H:%M}", formula="незавершённые поручения с известным сроком раньше текущего времени",
                     source_id="tasks", source_name="Реестр поручений AI-ассистента", updated_at=now, stale=False,
                     status="ok" if n == 0 else ("critical" if n > 3 else "warning"))


def returned_for(db: Session, user: User, meeting: Meeting) -> list[str]:
    ids, prev, seen = [], meeting.previous_meeting_id, set()
    while prev and prev not in seen:
        seen.add(prev)
        ids.append(prev)
        m = db.get(Meeting, prev)
        prev = m.previous_meeting_id if m else None
    if not ids:
        return []
    return list(db.scalars(select(Task.id).where(task_filter(user), Task.meeting_id.in_(ids), Task.status.in_(OPEN))).all())


def my_day(db: Session, user: User) -> MyDayOut:
    now = get_settings().now()
    tasks = list(db.scalars(select(Task).where(task_filter(user))).all())
    open_tasks = [t for t in tasks if t.status in OPEN]
    overdue = sorted([t for t in open_tasks if t.due_at and t.due_at < now], key=lambda t: t.due_at)
    names = {e.id: e.full_name for e in db.scalars(select(Employee)).all()}

    metrics = [metric_out(db, k, now) for k in kpi.CATALOG.values() if user.role == ROLE_DIRECTOR or not k.director_only]
    metrics.append(metric_overdue(overdue, now))

    needs: list[DecisionNeeded] = []
    for t in overdue:
        who = names.get(t.owner_employee_id, "без владельца")
        needs.append(DecisionNeeded(kind="overdue_task", title=f"Просрочено: {t.title}",
                                    detail=f"{who}, срок {t.due_at.astimezone(now.tzinfo):%d.%m %H:%M}",
                                    object_type="task", object_id=t.id,
                                    severity="critical" if t.owner_employee_id is None else "warning"))
    for t in open_tasks:
        if t.due_at is None:
            needs.append(DecisionNeeded(kind="clarification", title=f"Нет срока: {t.title}",
                                        detail=names.get(t.owner_employee_id, "без владельца"),
                                        object_type="task", object_id=t.id, severity="info"))
    for m in metrics:
        # просрочки уже перечислены по одной выше — сводный показатель не дублируем
        if m.status == "critical" and m.id != "overdue_tasks":
            needs.append(DecisionNeeded(kind="critical_metric", title=f"{m.name}: {kpi.fmt(m.value, m.unit)} при плане {kpi.fmt(m.plan, m.unit)}",
                                        detail=m.period, object_type="metric", object_id=m.id, severity="critical"))
        if m.stale:
            needs.append(DecisionNeeded(kind="stale_source", title=f"Данные устарели: {m.source_name}",
                                        detail=f"обновлено {m.updated_at.astimezone(now.tzinfo):%d.%m %H:%M}",
                                        object_type="data_source", object_id=m.source_id, severity="warning"))
    for ch in db.scalars(select(SourceChannel).where(SourceChannel.status == "error")).all():
        if channel_visible(user, ch):
            needs.append(DecisionNeeded(kind="stale_source", title=f"Источник не загружается: {ch.name}",
                                        detail=ch.last_error or "", object_type="data_source", object_id=ch.id))

    from .decisions import VERDICT_RU, decision_filter

    for d in db.scalars(select(Decision).where(decision_filter(user))).all():
        if d.status == "in_progress" and d.due_at and d.due_at <= now:
            needs.append(DecisionNeeded(kind="decision_check", title=f"Пора проверить решение: {d.title}",
                                        detail=f"срок {d.due_at.astimezone(now.tzinfo):%d.%m} прошёл — пересчитать показатель «до/после»",
                                        object_type="decision", object_id=d.id, severity="warning"))
        elif d.status in ("partial", "ineffective") and d.recheck:
            needs.append(DecisionNeeded(kind="decision_check", title=f"Нужно следующее решение: {d.title}",
                                        detail=f"повторная проверка: {VERDICT_RU[d.status]}",
                                        object_type="decision", object_id=d.id,
                                        severity="critical" if d.status == "ineffective" else "warning"))

    suggestions = []
    for t in sorted(open_tasks, key=lambda t: (t.due_at is None, t.due_at or now)):
        if t.owner_employee_id is None:
            action = delegation.propose(db, user, t)
            if action is not None and action.status == "pending":
                suggestions.append(action_out(action))
    watch_needs, watched_action_ids = delegation.watch_commitments(db, user, open_tasks)
    needs.extend(watch_needs)
    suggested_ids = {a.id for a in suggestions} | watched_action_ids
    for a in db.scalars(select(ActionProposal).where(ActionProposal.created_by == user.id, ActionProposal.status == "pending")
                        .order_by(ActionProposal.created_at.desc())).all():
        if a.id not in suggested_ids and can_access_action(db, user, a):
            needs.append(DecisionNeeded(kind="pending_action", title=f"Ждёт подтверждения: {action_out(a).preview.get('title', a.type)}",
                                        object_type="action", object_id=a.id, severity="info"))

    week_ago = now - timedelta(days=7)
    results = sorted([t for t in tasks if t.status == "done" and t.completed_at and week_ago <= t.completed_at <= now],
                     key=lambda t: t.completed_at, reverse=True)

    upcoming = [MeetingBrief(id=m.id, title=m.title, starts_at=m.starts_at, participant_ids=m.participant_ids or [],
                             returned_task_ids=returned_for(db, user, m))
                for m in db.scalars(select(Meeting).where(meeting_filter(user), Meeting.status == "planned",
                                                          Meeting.starts_at >= now, Meeting.starts_at <= now + timedelta(days=7))
                                    .order_by(Meeting.starts_at)).all()]
    if user.employee_id:
        for ev in db.scalars(select(CalendarEvent).where(CalendarEvent.starts_at >= now, CalendarEvent.starts_at <= now + timedelta(days=3),
                                                         CalendarEvent.meeting_id.is_(None)).order_by(CalendarEvent.starts_at)).all():
            if user.employee_id in (ev.attendee_ids or []):
                upcoming.append(MeetingBrief(id=ev.id, title=ev.title, starts_at=ev.starts_at, participant_ids=ev.attendee_ids or []))
    upcoming.sort(key=lambda b: b.starts_at or now)

    order = {"critical": 0, "warning": 1, "info": 2}
    needs.sort(key=lambda n: order[n.severity])
    return MyDayOut(now=now, needs_decision=needs, delegation_suggestions=suggestions,
                    task_results=[task_out(t) for t in results], upcoming_meetings=upcoming, metrics=metrics,
                    overdue=[task_out(t) for t in overdue])
