"""Каталог KPI: одна формула, один источник, один владелец на показатель (принцип «единого словаря» из трека 1).

Все значения считает код по сырым данным за произвольный период — это нужно и для «Моего дня»,
и для разбора отклонения (разрезы, тренд), и для повторной проверки решения «до/после».
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import DataSource, Order, QualityCheck, SupportTicket


@dataclass(frozen=True)
class KpiDef:
    id: str
    name: str
    unit: str
    plan: float
    higher_is_better: bool
    critical_ratio: float
    formula: str
    source_id: str
    owner_employee_id: str  # кто по справочнику отвечает за показатель
    director_only: bool = False


CATALOG: dict[str, KpiDef] = {k.id: k for k in [
    KpiDef("defect_rate", "Брак фотокниг", "%", 2.0, False, 1.5, "бракованные / проверенные × 100%", "mes_quality", "e-kozlova"),
    KpiDef("orders_on_time", "Заказы в срок", "%", 95.0, True, 0.85,
           "завершённые не позже обещанного срока / завершённые × 100%", "erp_orders", "e-morozov"),
    KpiDef("support_sla", "SLA первого ответа", "%", 90.0, True, 0.85,
           "ответ в пределах SLA / обращения с наступившим сроком × 100%; без ответа к сроку — нарушение",
           "crm_support", "e-belov", director_only=True),
]}


@dataclass
class Value:
    value: float | None
    numerator: int
    denominator: int


def _pct(num: int, den: int) -> Value:
    return Value(round(num / den * 100, 1) if den else None, num, den)


def fmt(value: float | None, unit: str = "") -> str:
    """Число для текста руководителю: «4,1%», «80%», «4 шт»; нет данных — так и пишем."""
    if value is None:
        return "нет данных"
    s = f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{s}{unit}" if unit in ("%", "") else f"{s} {unit}"


def _dt(d: date, end: bool = False) -> datetime:
    return datetime.combine(d, time.max if end else time.min, tzinfo=get_settings().tz)


def compute(db: Session, metric_id: str, start: date, end: date, *, where=None) -> Value:
    """Значение KPI за [start, end] включительно. where — фильтр разреза (dimension, key)."""
    dim, key = where or (None, None)
    if metric_id == "defect_rate":
        rows = db.scalars(select(QualityCheck).where(QualityCheck.checked_on >= start, QualityCheck.checked_on <= end)).all()
        if dim:
            rows = [r for r in rows if str(getattr(r, dim)) == key]
        return _pct(sum(r.defective_units for r in rows), sum(r.checked_units for r in rows))
    if metric_id == "orders_on_time":
        rows = [o for o in db.scalars(select(Order).where(Order.completed_at.is_not(None))).all()
                if _dt(start) <= o.completed_at <= _dt(end, True)]
        if dim:
            rows = [o for o in rows if str(getattr(o, dim)) == key]
        return _pct(sum(o.completed_at <= o.promised_at for o in rows), len(rows))
    if metric_id == "support_sla":
        now = get_settings().now()
        rows = [t for t in db.scalars(select(SupportTicket)).all()
                if _dt(start) <= t.sla_due_at <= min(_dt(end, True), now)]
        if dim:
            rows = [t for t in rows if str(getattr(t, dim)) == key]
        return _pct(sum(t.first_response_at is not None and t.first_response_at <= t.sla_due_at for t in rows), len(rows))
    raise KeyError(metric_id)


def status_of(k: KpiDef, value: float | None) -> str:
    if value is None:
        return "no_data"
    if k.higher_is_better:
        return "ok" if value >= k.plan else ("critical" if value < k.plan * k.critical_ratio else "warning")
    return "ok" if value <= k.plan else ("critical" if value > k.plan * k.critical_ratio else "warning")


def better(k: KpiDef, after: float, before: float) -> bool:
    return after > before if k.higher_is_better else after < before


def source_info(db: Session, k: KpiDef, now: datetime) -> tuple[str, datetime, bool]:
    src = db.get(DataSource, k.source_id)
    if src is None:
        return "источник не подключён", now, True
    return src.name, src.updated_at, now - src.updated_at > timedelta(hours=src.stale_after_hours)


def default_period(metric_id: str, now: datetime) -> tuple[date, date, str]:
    """Текущий период показателя для «Моего дня»."""
    if metric_id == "defect_rate":
        start = now.date().replace(day=1)
        return start, now.date(), f"{start:%d.%m}–{now:%d.%m.%Y}"
    if metric_id == "orders_on_time":
        return date(2000, 1, 1), now.date(), "вся выгрузка ERP"
    return date(2000, 1, 1), now.date(), "обращения с наступившим сроком"


def month_start(d: date) -> date:
    return d.replace(day=1)


def prev_month(d: date) -> tuple[date, date]:
    first = month_start(d)
    last_prev = first - timedelta(days=1)
    return month_start(last_prev), last_prev


DIMENSIONS = {
    "defect_rate": [("paper_supplier", "Поставщик бумаги"), ("line", "Линия"), ("product", "Продукт")],
    "orders_on_time": [("product", "Продукт"), ("channel", "Канал заказа")],
    "support_sla": [("topic", "Тема обращения")],
}


def breakdown(db: Session, metric_id: str, dim: str, start: date, end: date) -> list[dict]:
    if metric_id == "defect_rate":
        keys = {str(getattr(r, dim)) for r in db.scalars(select(QualityCheck).where(
            QualityCheck.checked_on >= start, QualityCheck.checked_on <= end)).all()}
    elif metric_id == "orders_on_time":
        keys = {str(getattr(o, dim)) for o in db.scalars(select(Order)).all()}
    else:
        keys = {str(getattr(t, dim)) for t in db.scalars(select(SupportTicket)).all()}
    rows = []
    for key in sorted(keys):
        v = compute(db, metric_id, start, end, where=(dim, key))
        if v.denominator:
            rows.append({"key": key, "value": v.value, "numerator": v.numerator, "denominator": v.denominator})
    total = sum(r["denominator"] for r in rows) or 1
    for r in rows:
        r["share"] = round(r["denominator"] / total * 100, 1)
    return rows


def trend(db: Session, metric_id: str, now: datetime, months: int = 4) -> list[dict]:
    out, cursor = [], month_start(now.date())
    periods = []
    for _ in range(months):
        end = min(now.date(), (cursor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
        periods.append((cursor, end))
        cursor = prev_month(cursor)[0]
    for start, end in reversed(periods):
        v = compute(db, metric_id, start, end)
        out.append({"period": f"{start:%Y-%m}", "label": f"{start:%m.%Y}" + (" (тек.)" if end == now.date() else ""),
                    "value": v.value, "numerator": v.numerator, "denominator": v.denominator})
    return out


def group_count(items, key) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for i in items:
        out[key(i)] += 1
    return dict(out)
