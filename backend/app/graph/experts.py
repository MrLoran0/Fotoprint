"""«Кто может знать»: когда в доступных источниках ответа нет, подсказываем людей — по графу и зонам ответственности.

Используются только данные, доступные текущему пользователю: справочник сотрудников, его поручения
и сообщения/документы, которые он и так может открыть. Модель здесь не участвует.
"""

import re
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..access import task_filter
from ..models import Document, DocumentChunk, Employee, Mention, Task, User
from ..schemas import ExpertHint
from ..security import readable_document_filter
from .mentions import build_matchers, find_mentions


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яёa-z]{4,}", text.casefold())}


def suggest_experts(db: Session, user: User, question: str, limit: int = 3) -> list[ExpertHint]:
    employees = {e.id: e for e in db.scalars(select(Employee)).all()}
    matchers = build_matchers(db)
    targets = find_mentions(question, matchers)
    score: dict[str, float] = defaultdict(float)
    reason: dict[str, str] = {}

    def add(emp_id: str | None, points: float, why: str):
        if emp_id and emp_id in employees and emp_id != user.employee_id:
            score[emp_id] += points
            reason.setdefault(emp_id, why)

    for t, i, _ in targets:
        if t == "employee":
            add(i, 3, "упомянут(а) в вопросе")
    entity_ids = [i for t, i, _ in targets if t == "entity"]
    if entity_ids:
        # авторы сообщений и писем об этих сущностях — только из того, что разрешено ИИ (ai_allowed) и доступно пользователю:
        # подсказка «кто может знать» — тоже вывод ИИ, закрытые для ИИ документы (оклады, ПДн) в неё не попадают
        rows = db.execute(
            select(DocumentChunk.author_employee_id, func.count())
            .join(Mention, Mention.chunk_id == DocumentChunk.id).join(Document, Document.id == DocumentChunk.document_id)
            .where(Mention.target_id.in_(entity_ids), DocumentChunk.author_employee_id.is_not(None),
                   readable_document_filter(user, for_ai=True))
            .group_by(DocumentChunk.author_employee_id)
        ).all()
        for emp_id, n in rows:
            add(emp_id, 2 * n, "писал(а) об этом в доступных вам сообщениях")
        entity_matchers = [m for m in matchers if m.target_type == "entity" and m.target_id in entity_ids]
        for task in db.scalars(select(Task).where(task_filter(user), Task.owner_employee_id.is_not(None))).all():
            if find_mentions(task.title, entity_matchers):
                add(task.owner_employee_id, 2, f"владелец поручения «{task.title}»")
    q = _stems(question)
    for e in employees.values():
        hits = [r for r in e.responsibilities if _stems(r) & q]
        if hits:
            add(e.id, 1.5 * len(hits), f"зона ответственности: {', '.join(hits)}")

    best = sorted(score.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [ExpertHint(employee_id=i, full_name=employees[i].full_name, position=employees[i].position, reason=reason[i])
            for i, _ in best]
