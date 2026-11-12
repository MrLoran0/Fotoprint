"""Кому поручить задачу без владельца и «Сторож договорённостей». Правила детерминированные, модель не участвует.

1. Человек сам взял задачу на себя в переписке («запрошу сама, пришлю до пятницы») — самый сильный довод;
   ищем в доступных пользователю сообщениях (readable_document_filter).
2. Зона ответственности из справочника совпадает с формулировкой задачи.
3. «Сторож договорённостей» (watch_commitments):
   - находит в доступных сообщениях почты/Telegram/чатов (DocumentChunk с sent_at) обещания со сроком
     и формирует DecisionNeeded(kind="commitment") с цитатой и карточкой delegate_task или update_task;
   - сверяет договорённости в переписке с реестром поручений (владелец, срок, статус)
     и формирует DecisionNeeded(kind="contradiction") с двумя первоисточниками.
Кандидаты — только те, кому пользователь вправе поручать (своё подразделение; директор — все).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .actions import create_action, payload_hash, task_snapshot
from .config import get_settings
from .evidence import resolve_due
from .graph.mentions import build_matchers, find_mentions
from .models import (
    ROLE_DIRECTOR,
    ActionProposal,
    Document,
    DocumentChunk,
    Employee,
    Meeting,
    Mention,
    ProtocolItem,
    Task,
    Transcript,
    User,
)
from .schemas import AgendaPoint, DecisionNeeded, SourceRef
from .security import readable_document_filter

_COMMIT = re.compile(
    r"(?<!\w)(запрошу|сделаю|пришлю|подготовлю|проверю|возьму|беру(?:\s+\S+){0,3}\s+на\s+себя|"
    r"займусь|организую|согласую|отправлю|проведу|соберу|сверю|договорюсь|узнаю|"
    r"восстановлю|направлю|перенес\w*)(?!\w)",
    re.IGNORECASE,
)

_DUE_RE = re.compile(
    r"(?<!\w)(?:до|к|на)\s+"
    r"(?:(?:вечера|утра|конца\s+дня)\s+)?"
    r"(?:"
    r"(?:понедельника|понедельник|вторника|вторник|среды|среду|четверга|четверг|пятницы|пятницу|субботы|субботу|воскресенья|воскресенье)"
    r"\s*,\s*(\d{1,2}\.\d{1,2}(?:\.\d{4})?)"
    r"|"
    r"(понедельника|понедельник|вторника|вторник|среды|среду|четверга|четверг|пятницы|пятницу|субботы|субботу|воскресенья|воскресенье"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\.\d{1,2}(?:\.\d{4})?"
    r"|сегодня|завтра|послезавтра)"
    r")",
    re.IGNORECASE,
)

_NO_TASK_RE = re.compile(
    r"отдельн\w+\s+(?:задач\w+|поручен\w+)\s+в\s+реестре\s+пока\s+нет",
    re.IGNORECASE,
)

_DONE_CLAIM_RE = re.compile(
    r"считаю\s+задач\w+\s+выполненн\w+|задач\w+\s+выполнен\w+|поручен\w+\s+выполнен\w+",
    re.IGNORECASE,
)


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яё]{4,}", text.casefold().replace("ё", "е"))}


def _chunk_body(doc: Document, chunk: DocumentChunk) -> str:
    text = chunk.text.strip()
    if doc.kind == "email" and text.startswith("Тема:") and "\n\n" in text:
        text = text.split("\n\n", 1)[1].strip()
    elif doc.kind in {"telegram", "chat"} and ": " in text:
        text = text.split(": ", 1)[1].strip()
    return text


def _chunk_quote(doc: Document, chunk: DocumentChunk) -> str:
    return " ".join(_chunk_body(doc, chunk).split())[:240]


def extract_due(
    text: str,
    sent_at: datetime | None,
    timezone: str = "Europe/Minsk",
) -> tuple[datetime | None, str | None]:
    """Выделить срок из текста сообщения относительно времени отправки (sent_at)."""
    if not text or sent_at is None:
        return None, None
    resolved: tuple[datetime, str] | None = None
    for m in _DUE_RE.finditer(text):
        token = m.group(1) or m.group(2)
        due = resolve_due(token, sent_at, timezone)
        if due is not None:
            resolved = (due, m.group(0).strip())
    return resolved if resolved is not None else (None, None)


@dataclass
class Suggestion:
    employee: Employee
    reason: str
    evidence: SourceRef | None = None
    due_at: datetime | None = None


def candidates(db: Session, user: User) -> dict[str, Employee]:
    q = select(Employee)
    if user.role != ROLE_DIRECTOR:
        q = q.where(Employee.department_id == user.department_id)
    return {e.id: e for e in db.scalars(q).all()}


def suggest(db: Session, user: User, task: Task) -> Suggestion | None:
    people = candidates(db, user)
    if not people:
        return None
    s_cfg = get_settings()
    tz = s_cfg.tz
    tz_name = s_cfg.company_tz
    matchers = build_matchers(db)
    entity_ids = [i for t, i, _ in find_mentions(f"{task.title} {task.expected_result}", matchers) if t == "entity"]

    rows: list[tuple[Document, DocumentChunk]] = []
    seen_chunks: set[str] = set()
    if entity_ids:
        mention_rows = db.execute(
            select(Document, DocumentChunk)
            .join(DocumentChunk, DocumentChunk.document_id == Document.id)
            .join(Mention, Mention.chunk_id == DocumentChunk.id)
            .where(
                Mention.target_id.in_(entity_ids),
                DocumentChunk.author_employee_id.in_(list(people)),
                DocumentChunk.injection_flag.is_(False),
                readable_document_filter(user, for_ai=True),
            )
            .order_by(DocumentChunk.sent_at.desc().nullslast())
        ).all()
        for d, c in mention_rows:
            if c.id not in seen_chunks:
                seen_chunks.add(c.id)
                rows.append((d, c))

    corr_rows = db.execute(
        select(Document, DocumentChunk)
        .join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(
            Document.kind.in_(("email", "telegram", "chat")),
            DocumentChunk.sent_at.is_not(None),
            DocumentChunk.author_employee_id.in_(list(people)),
            DocumentChunk.injection_flag.is_(False),
            readable_document_filter(user, for_ai=True),
        )
        .order_by(DocumentChunk.sent_at.desc(), DocumentChunk.id.asc())
    ).all()
    for d, c in corr_rows:
        if c.id not in seen_chunks:
            seen_chunks.add(c.id)
            rows.append((d, c))

    if rows:
        # Обещание должно быть про ЭТО действие: ранжируем по совпадению слов с формулировкой задачи
        # («запросить сертификаты» ↔ «сертификаты … запрошу»), при равенстве — более свежее сообщение.
        task_words = _stems(f"{task.title} {task.expected_result}")
        scored = []
        for i, (d, c) in enumerate(rows):
            body = _chunk_body(d, c)
            if _NO_TASK_RE.search(body) or not _COMMIT.search(body):
                continue
            overlap = len(_stems(body) & task_words)
            if task.id in c.text or overlap >= 2:
                scored.append((overlap if task.id not in c.text else max(overlap, 5), i, d, c))
        scored.sort(key=lambda s: (-s[0], s[1]))
        if scored:
            _, _, doc, chunk = scored[0]
            who = people[chunk.author_employee_id]
            when = chunk.sent_at.astimezone(tz).strftime("%d.%m %H:%M") if chunk.sent_at else doc.doc_date.strftime("%d.%m")
            quote = _chunk_quote(doc, chunk)
            promised_due, _ = extract_due(_chunk_body(doc, chunk), chunk.sent_at, tz_name)
            ref = SourceRef(
                source_type=doc.kind,
                source_id=doc.id,
                title=doc.title,
                source_date=doc.doc_date,
                chunk_id=chunk.id,
                quote=quote,
                author=chunk.author_raw,
                sent_at=chunk.sent_at,
                channel_id=doc.channel_id,
            )
            return Suggestion(
                who,
                f"Взято на себя в переписке — {who.full_name} ({doc.title}, {when}): «{quote}»",
                ref,
                due_at=promised_due,
            )

    words = _stems(f"{task.title} {task.expected_result}")
    best, best_hits = None, []
    for e in people.values():
        hits = [r for r in e.responsibilities if _stems(r) & words]
        if len(hits) > len(best_hits):
            best, best_hits = e, hits
    if best is not None:
        return Suggestion(best, f"зона ответственности: {', '.join(best_hits)}")
    return None


def propose(db: Session, user: User, task: Task) -> ActionProposal | None:
    """Карточка delegate_task (origin=ai) — идемпотентна для одного и того же состояния задачи."""
    if task.owner_employee_id or task.status in {"done", "cancelled"}:
        return None
    s = suggest(db, user, task)
    if s is None:
        return None
    snapshot = payload_hash(task_snapshot(task))
    due_val = task.due_at or s.due_at
    payload = {
        "task_id": task.id,
        "expected_hash": snapshot,
        "owner_employee_id": s.employee.id,
        "due_at": due_val.isoformat() if due_val else None,
        "expected_result": task.expected_result or "",
        "reason": s.reason,
    }
    key = payload_hash({"user": user.id, "delegate": task.id, "snapshot": snapshot})
    action = create_action(db, user, "delegate_task", payload, key=key, origin="ai")
    if s.evidence is not None and not (action.result or {}).get("evidence"):
        # первоисточник довода показываем в предпросмотре; в payload он не входит (не исполняется)
        action.result = {"evidence": s.evidence.model_dump(mode="json")}
        db.commit()
    return action


def _match_task_for_chunk(doc: Document, chunk: DocumentChunk, body: str, open_tasks: list[Task]) -> Task | None:
    if _NO_TASK_RE.search(body):
        return None
    for t in open_tasks:
        if t.id in chunk.text:
            return t
    body_stems = _stems(body)
    full_stems = _stems(f"{doc.title} {body}")
    best_task: Task | None = None
    best_score: tuple[int, int, int, int] = (0, 0, 0, 0)
    for t in open_tasks:
        t_stems = _stems(f"{t.title} {t.expected_result}")
        body_overlap = len(body_stems & t_stems)
        full_overlap = len(full_stems & t_stems)
        if body_overlap >= 2:
            score = (
                full_overlap,
                body_overlap,
                1 if t.meeting_id is not None else 0,
                1 if t.owner_employee_id is None else 0,
            )
            if score > best_score:
                best_score = score
                best_task = t
    return best_task


def _matching_tasks_for_chunk(chunk: DocumentChunk, body: str, open_tasks: list[Task]) -> list[Task]:
    if _NO_TASK_RE.search(body):
        return []
    explicit = [t for t in open_tasks if t.id in chunk.text]
    if explicit:
        return explicit
    body_stems = _stems(body)
    matched: list[Task] = []
    for t in open_tasks:
        t_stems = _stems(f"{t.title} {t.expected_result}")
        if len(body_stems & t_stems) >= 2:
            matched.append(t)
    return matched


def _extract_commitment_title(doc: Document, body: str) -> str:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", body) if s.strip()]
    for sent in sentences:
        if _NO_TASK_RE.search(sent):
            continue
        if _COMMIT.search(sent):
            cleaned = re.sub(r"\s+", " ", sent).strip(" .,:;-")
            return cleaned[:160]
    return doc.title[:160]


def _task_protocol_source(db: Session, task: Task) -> tuple[str, str]:
    """Возвращает описание первоисточника задачи из протокола/расшифровки встречи (для блока противоречий)."""
    if not task.meeting_id:
        return "", ""
    meeting = db.get(Meeting, task.meeting_id)
    meeting_note = f", встреча «{meeting.title}»" if meeting else ""
    if task.protocol_item_id:
        item = db.get(ProtocolItem, task.protocol_item_id)
        if item and item.evidence_quote:
            return meeting_note, f" (протокол: «{item.evidence_quote[:180]}»)"
    transcript = db.scalar(
        select(Transcript).where(Transcript.meeting_id == task.meeting_id).order_by(Transcript.created_at.desc())
    )
    if transcript and transcript.segments:
        t_stems = _stems(task.title)
        best_seg = None
        best_overlap = 0
        for seg in transcript.segments:
            overlap = len(_stems(seg.get("text", "")) & t_stems)
            if overlap >= 2 and overlap > best_overlap:
                best_overlap = overlap
                best_seg = seg
        if best_seg is not None:
            return meeting_note, f" (протокол/расшифровка: «{best_seg['text'][:180]}»)"
    return meeting_note, ""


def watch_commitments(
    db: Session,
    user: User,
    open_tasks: list[Task],
) -> tuple[list[DecisionNeeded], set[str]]:
    """Сторож договорённостей: обещания со сроком (kind='commitment') и противоречия с реестром (kind='contradiction')."""
    s_cfg = get_settings()
    tz: ZoneInfo = s_cfg.tz
    tz_name = s_cfg.company_tz
    people = candidates(db, user)
    names = {e.id: e.full_name for e in db.scalars(select(Employee)).all()}

    rows = db.execute(
        select(Document, DocumentChunk)
        .join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(
            Document.kind.in_(("email", "telegram", "chat")),
            DocumentChunk.sent_at.is_not(None),
            DocumentChunk.injection_flag.is_(False),
            readable_document_filter(user, for_ai=True),
        )
        .order_by(DocumentChunk.sent_at.desc(), DocumentChunk.id.asc())
    ).all()

    needs: list[DecisionNeeded] = []
    watched_action_ids: set[str] = set()
    seen_commitment_tasks: set[str] = set()
    chunks_by_task: dict[str, list[tuple[Document, DocumentChunk, str, str, datetime | None]]] = {}

    for doc, chunk in rows:
        body = _chunk_body(doc, chunk)
        quote = _chunk_quote(doc, chunk)
        promised_due, _ = extract_due(body, chunk.sent_at, tz_name)
        for mt in _matching_tasks_for_chunk(chunk, body, open_tasks):
            chunks_by_task.setdefault(mt.id, []).append((doc, chunk, body, quote, promised_due))
        matched_task = _match_task_for_chunk(doc, chunk, body, open_tasks)

        # 1. Обещание со сроком (commitment) -> карточка delegate_task или update_task
        if chunk.author_employee_id not in people or not _COMMIT.search(body) or promised_due is None:
            continue

        who = people[chunk.author_employee_id]
        when = chunk.sent_at.astimezone(tz).strftime("%d.%m %H:%M") if chunk.sent_at else doc.doc_date.strftime("%d.%m")
        ref = SourceRef(
            source_type=doc.kind,
            source_id=doc.id,
            title=doc.title,
            source_date=doc.doc_date,
            chunk_id=chunk.id,
            quote=quote,
            author=chunk.author_raw,
            sent_at=chunk.sent_at,
            channel_id=doc.channel_id,
        )

        if matched_task is not None:
            if matched_task.id in seen_commitment_tasks:
                continue
            same_owner = matched_task.owner_employee_id == who.id
            same_due = (
                matched_task.due_at is not None
                and matched_task.due_at.astimezone(tz).date() == promised_due.astimezone(tz).date()
            )
            if same_owner and same_due:
                seen_commitment_tasks.add(matched_task.id)
                continue

            action: ActionProposal | None = None
            if matched_task.owner_employee_id is None:
                action = propose(db, user, matched_task)
                if action is not None and action.status == "pending" and action.payload.get("due_at") is None:
                    action.payload = {**action.payload, "due_at": promised_due.isoformat()}
                    action.payload_hash = payload_hash(action.payload)
                    db.commit()
            else:
                changes: dict[str, str] = {}
                if not same_owner:
                    changes["owner_employee_id"] = who.id
                if not same_due:
                    changes["due_at"] = promised_due.isoformat()
                if changes:
                    snapshot = payload_hash(task_snapshot(matched_task))
                    payload = {"task_id": matched_task.id, "expected_hash": snapshot, "changes": changes}
                    key = payload_hash({"user": user.id, "commit_update": matched_task.id, "snapshot": snapshot})
                    action = create_action(db, user, "update_task", payload, key=key, origin="ai")
                    if not (action.result or {}).get("evidence"):
                        action.result = {**(action.result or {}), "evidence": ref.model_dump(mode="json")}
                        db.commit()

            seen_commitment_tasks.add(matched_task.id)
            if action is not None and action.status == "pending":
                watched_action_ids.add(action.id)
                needs.append(
                    DecisionNeeded(
                        kind="commitment",
                        title=f"Обещание в переписке: {matched_task.title}",
                        detail=(
                            f"{who.full_name} ({doc.title}, {when}, срок до {promised_due.astimezone(tz):%d.%m.%Y}): "
                            f"«{quote}»"
                        ),
                        object_type="action",
                        object_id=action.id,
                        severity="warning",
                    )
                )
        else:
            proposed_title = _extract_commitment_title(doc, body)
            task_id = f"t-commit-{payload_hash({'doc': doc.id, 'chunk': chunk.id})[:10]}"
            if db.get(Task, task_id) is not None:
                continue
            reason = f"Обещание в переписке — {who.full_name} ({doc.title}, {when}): «{quote}»"
            payload = {
                "task_id": task_id,
                "expected_hash": "new",
                "owner_employee_id": who.id,
                "due_at": promised_due.isoformat(),
                "expected_result": proposed_title,
                "reason": reason,
            }
            key = payload_hash({"user": user.id, "commit_new": task_id})
            action = create_action(db, user, "delegate_task", payload, key=key, origin="ai")
            if not (action.result or {}).get("evidence"):
                action.result = {"evidence": ref.model_dump(mode="json"), "proposed_title": proposed_title}
                db.commit()
            if action.status == "pending":
                watched_action_ids.add(action.id)
                needs.append(
                    DecisionNeeded(
                        kind="commitment",
                        title=f"Обещание в переписке: {proposed_title}",
                        detail=(
                            f"{who.full_name} ({doc.title}, {when}, срок до {promised_due.astimezone(tz):%d.%m.%Y}): "
                            f"«{quote}»"
                        ),
                        object_type="action",
                        object_id=action.id,
                        severity="warning",
                    )
                )

    # 2. Противоречия между реестром поручений и перепиской (kind="contradiction") с двумя первоисточниками
    tasks_by_id = {t.id: t for t in open_tasks}
    for task_id, matched_list in chunks_by_task.items():
        task = tasks_by_id[task_id]
        t_title_stems = _stems(task.title)
        conflicting: list[tuple[Document, DocumentChunk, str, str, datetime | None, list[str]]] = []
        for doc, chunk, body, quote, msg_due in matched_list:
            reasons: list[str] = []
            strong_match = task.id in chunk.text or len(_stems(body) & t_title_stems) >= 3 or msg_due is not None
            if (
                strong_match
                and _COMMIT.search(body)
                and chunk.author_employee_id
                and chunk.author_employee_id != task.owner_employee_id
            ):
                who_name = names.get(chunk.author_employee_id, chunk.author_employee_id)
                reg_owner = names.get(task.owner_employee_id, "не назначен") if task.owner_employee_id else "не назначен"
                reasons.append(f"исполнитель в переписке — {who_name} (в реестре: {reg_owner})")
            if msg_due is not None and (
                task.due_at is None or task.due_at.astimezone(tz).date() != msg_due.astimezone(tz).date()
            ):
                reg_due = task.due_at.astimezone(tz).strftime("%d.%m.%Y") if task.due_at else "не указан"
                reasons.append(f"срок в переписке — {msg_due.astimezone(tz):%d.%m.%Y} (в реестре: {reg_due})")
            if _DONE_CLAIM_RE.search(body) and task.status != "done":
                reasons.append(f"в переписке заявлено о выполнении (в реестре статус: {task.status})")
            if reasons:
                conflicting.append((doc, chunk, body, quote, msg_due, reasons))

        if not conflicting:
            continue

        reg_owner = names.get(task.owner_employee_id, "не назначен") if task.owner_employee_id else "не назначен"
        reg_due = task.due_at.astimezone(tz).strftime("%d.%m.%Y") if task.due_at else "не указан"
        meeting_note, proto_quote = _task_protocol_source(db, task)
        src1 = (
            f"Первоисточник 1 (реестр поручений {task.id}{meeting_note}): "
            f"владелец — {reg_owner}, срок — {reg_due}, статус — {task.status}{proto_quote}."
        )
        src2_parts = []
        for doc, chunk, _, quote, msg_due, reasons in conflicting[:3]:
            author_label = names.get(chunk.author_employee_id) or chunk.author_raw or doc.title
            when = chunk.sent_at.astimezone(tz).strftime("%d.%m %H:%M") if chunk.sent_at else doc.doc_date.strftime("%d.%m")
            src2_parts.append(f"{author_label} ({doc.title}, {when}; {', '.join(reasons)}): «{quote}»")
        src2 = "Первоисточник 2 (переписка): " + "; ".join(src2_parts)

        needs.append(
            DecisionNeeded(
                kind="contradiction",
                title=f"Противоречие с реестром: {task.title}",
                detail=f"{src1} {src2}",
                object_type="task",
                object_id=task.id,
                severity="warning",
            )
        )

    return needs, watched_action_ids


def agenda_watch_points(
    db: Session,
    user: User,
    participant_ids: list[str],
    open_tasks: list[Task],
) -> list[AgendaPoint]:
    """Пункты повестки встречи по обещаниям и противоречиям участников встречи (с цитатой-источником)."""
    s_cfg = get_settings()
    tz: ZoneInfo = s_cfg.tz
    tz_name = s_cfg.company_tz
    people = candidates(db, user)
    names = {e.id: e.full_name for e in db.scalars(select(Employee)).all()}
    participant_set = set(participant_ids)

    rows = db.execute(
        select(Document, DocumentChunk)
        .join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(
            Document.kind.in_(("email", "telegram", "chat")),
            DocumentChunk.sent_at.is_not(None),
            DocumentChunk.injection_flag.is_(False),
            readable_document_filter(user, for_ai=True),
        )
        .order_by(DocumentChunk.sent_at.desc(), DocumentChunk.id.asc())
    ).all()

    points: list[AgendaPoint] = []
    seen_commitment_tasks: set[str] = set()
    chunks_by_task: dict[str, list[tuple[Document, DocumentChunk, str, str, datetime | None]]] = {}

    for doc, chunk in rows:
        body = _chunk_body(doc, chunk)
        quote = _chunk_quote(doc, chunk)
        promised_due, _ = extract_due(body, chunk.sent_at, tz_name)
        for mt in _matching_tasks_for_chunk(chunk, body, open_tasks):
            chunks_by_task.setdefault(mt.id, []).append((doc, chunk, body, quote, promised_due))
        matched_task = _match_task_for_chunk(doc, chunk, body, open_tasks)

        if chunk.author_employee_id not in people or not _COMMIT.search(body) or promised_due is None:
            continue
        if chunk.author_employee_id not in participant_set and (
            matched_task is None or matched_task.owner_employee_id not in participant_set
        ):
            continue

        who = people[chunk.author_employee_id]
        ref = SourceRef(
            source_type=doc.kind,
            source_id=doc.id,
            title=doc.title,
            version=doc.version,
            source_date=doc.doc_date,
            chunk_id=chunk.id,
            quote=quote,
            author=chunk.author_raw,
            sent_at=chunk.sent_at,
            channel_id=doc.channel_id,
        )
        if matched_task is not None:
            if matched_task.id in seen_commitment_tasks:
                continue
            same_owner = matched_task.owner_employee_id == who.id
            same_due = (
                matched_task.due_at is not None
                and matched_task.due_at.astimezone(tz).date() == promised_due.astimezone(tz).date()
            )
            seen_commitment_tasks.add(matched_task.id)
            if same_owner and same_due:
                continue
            points.append(
                AgendaPoint(
                    title=f"Обещание в переписке ({who.full_name}, до {promised_due.astimezone(tz):%d.%m.%Y}): {matched_task.title}"[:200],
                    minutes=5,
                    sources=[ref],
                )
            )
        else:
            proposed_title = _extract_commitment_title(doc, body)
            task_id = f"t-commit-{payload_hash({'doc': doc.id, 'chunk': chunk.id})[:10]}"
            if db.get(Task, task_id) is not None:
                continue
            points.append(
                AgendaPoint(
                    title=f"Обещание в переписке ({who.full_name}, до {promised_due.astimezone(tz):%d.%m.%Y}): {proposed_title}"[:200],
                    minutes=5,
                    sources=[ref],
                )
            )

    tasks_by_id = {t.id: t for t in open_tasks}
    for task_id, matched_list in chunks_by_task.items():
        task = tasks_by_id[task_id]
        t_title_stems = _stems(task.title)
        conflicting: list[tuple[Document, DocumentChunk, str, str, datetime | None, list[str]]] = []
        for doc, chunk, body, quote, msg_due in matched_list:
            reasons: list[str] = []
            strong_match = task.id in chunk.text or len(_stems(body) & t_title_stems) >= 3 or msg_due is not None
            if (
                strong_match
                and _COMMIT.search(body)
                and chunk.author_employee_id
                and chunk.author_employee_id != task.owner_employee_id
            ):
                who_name = names.get(chunk.author_employee_id, chunk.author_employee_id)
                reg_owner = names.get(task.owner_employee_id, "не назначен") if task.owner_employee_id else "не назначен"
                reasons.append(f"исполнитель в переписке — {who_name} (в реестре: {reg_owner})")
            if msg_due is not None and (
                task.due_at is None or task.due_at.astimezone(tz).date() != msg_due.astimezone(tz).date()
            ):
                reg_due = task.due_at.astimezone(tz).strftime("%d.%m.%Y") if task.due_at else "не указан"
                reasons.append(f"срок в переписке — {msg_due.astimezone(tz):%d.%m.%Y} (в реестре: {reg_due})")
            if _DONE_CLAIM_RE.search(body) and task.status != "done":
                reasons.append(f"в переписке заявлено о выполнении (в реестре статус: {task.status})")
            if reasons:
                conflicting.append((doc, chunk, body, quote, msg_due, reasons))

        if not conflicting:
            continue
        if task.owner_employee_id not in participant_set and not any(
            c.author_employee_id in participant_set for _, c, _, _, _, _ in conflicting
        ):
            continue

        sources: list[SourceRef] = []
        reason_parts: list[str] = []
        for doc, chunk, _, quote, _, reasons in conflicting[:3]:
            sources.append(
                SourceRef(
                    source_type=doc.kind,
                    source_id=doc.id,
                    title=doc.title,
                    version=doc.version,
                    source_date=doc.doc_date,
                    chunk_id=chunk.id,
                    quote=quote,
                    author=chunk.author_raw,
                    sent_at=chunk.sent_at,
                    channel_id=doc.channel_id,
                )
            )
            for r in reasons:
                if r not in reason_parts:
                    reason_parts.append(r)

        points.append(
            AgendaPoint(
                title=f"Противоречие с реестром: {task.title} ({'; '.join(reason_parts[:2])})"[:200],
                minutes=5,
                sources=sources,
            )
        )

    return points
