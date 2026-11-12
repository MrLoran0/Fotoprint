"""Синхронизация канала: сообщения → документы с правами участников, фрагменты, упоминания.

Синхронизация полная и идемпотентная: документы канала пересоздаются. Ошибка разбора не роняет стенд —
канал получает status=error и текст ошибки, остальные источники продолжают работать.
"""

import hashlib
import logging
from collections import defaultdict
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..evidence import normalize, suspicious
from ..graph.mentions import build_matchers, index_chunks
from ..ingest import chunk_text
from ..models import ACCESS_PARTICIPANTS, Document, DocumentChunk, Employee, SourceChannel
from . import PARSERS
from .base import SourceMessage

log = logging.getLogger("fokus.sources")


def _resolve_author(msg: SourceMessage, employees: list[Employee]) -> str | None:
    if msg.author_email:
        for e in employees:
            if e.email.lower() == msg.author_email.lower():
                return e.id
    name = normalize(msg.author_raw)
    hits = [e.id for e in employees if name in {normalize(e.full_name), *(normalize(a) for a in e.aliases)}]
    return hits[0] if len(hits) == 1 else None


def sync_channel(db: Session, channel: SourceChannel, data_dir: Path | None = None) -> int:
    s = get_settings()
    root = data_dir or s.data_dir
    try:
        # SAVEPOINT: сбой одного источника откатывает только его, а не весь seed
        with db.begin_nested():
            parser = PARSERS[channel.connector]
            messages = parser(root / channel.path, s.tz)
            employees = list(db.scalars(select(Employee)).all())
            by_email = {e.email.lower(): e.id for e in employees}
            db.execute(delete(Document).where(Document.channel_id == channel.id))
            db.flush()
            docs = (_emails(channel, messages, employees, by_email) if channel.kind == "email"
                    else _chat_days(channel, messages, employees))
            for doc in docs:
                db.add(doc)
            db.flush()
            index_chunks(db, [c for d in docs for c in d.chunks], build_matchers(db))
        channel.status, channel.last_error, channel.items_count = "ok", None, len(messages)
    except Exception as e:  # noqa: BLE001 — любая ошибка источника видна в его статусе, стенд продолжает работать
        channel.status, channel.last_error = "error", f"{type(e).__name__}: {e}"[:500]
        log.warning("source %s failed: %s", channel.id, e)
    channel.last_sync_at = s.now()
    db.flush()
    return channel.items_count


def _doc_id(channel: SourceChannel, key: str) -> str:
    return f"{channel.id}:{hashlib.sha1(key.encode()).hexdigest()[:12]}"


def _emails(channel: SourceChannel, messages: list[SourceMessage], employees, by_email) -> list[Document]:
    docs = []
    for m in messages:
        author_id = _resolve_author(m, employees)
        allowed = set(channel.member_ids or [])
        allowed |= {by_email[a] for a in m.recipients if a in by_email}
        if author_id:
            allowed.add(author_id)
        doc = Document(
            id=_doc_id(channel, m.external_id), title=m.subject or "(без темы)", version=1, doc_date=m.sent_at.date(),
            author=m.author_raw, access_level=channel.access_level, department_id=channel.department_id,
            ai_allowed=True, content=m.text, source_path=channel.path, tags=["email"], kind="email",
            channel_id=channel.id, external_id=m.external_id,
            allowed_employee_ids=sorted(allowed) if channel.access_level == ACCESS_PARTICIPANTS else [],
        )
        doc.chunks = [
            DocumentChunk(id=f"{doc.id}#{c.ordinal}", ordinal=c.ordinal, text=c.text, char_start=c.start, char_end=c.end,
                          injection_flag=suspicious(c.text), author_employee_id=author_id, author_raw=m.author_raw,
                          sent_at=m.sent_at)
            for c in chunk_text(m.text)
        ]
        docs.append(doc)
    return docs


def _chat_days(channel: SourceChannel, messages: list[SourceMessage], employees) -> list[Document]:
    """Чат режем по дням: документ = канал за день, фрагмент = одно сообщение (точная цитата и автор)."""
    by_day: dict = defaultdict(list)
    for m in sorted(messages, key=lambda x: x.sent_at):
        by_day[m.sent_at.date()].append(m)
    docs = []
    for day, msgs in by_day.items():
        doc = Document(
            id=f"{channel.id}:{day.isoformat()}", title=f"{channel.name} — {day:%d.%m.%Y}", version=1, doc_date=day,
            author=channel.name, access_level=channel.access_level, department_id=channel.department_id,
            ai_allowed=True, content="", source_path=channel.path, tags=[channel.kind], kind=channel.kind,
            channel_id=channel.id, external_id=f"{channel.id}/{day.isoformat()}",
            allowed_employee_ids=sorted(channel.member_ids or []) if channel.access_level == ACCESS_PARTICIPANTS else [],
        )
        lines, chunks, pos = [], [], 0
        for i, m in enumerate(msgs):
            line = f"[{m.sent_at:%H:%M}] {m.author_raw}: {m.text}"
            chunks.append(DocumentChunk(
                id=f"{doc.id}#{i}", ordinal=i, text=line, char_start=pos, char_end=pos + len(line),
                injection_flag=suspicious(m.text), author_employee_id=_resolve_author(m, employees),
                author_raw=m.author_raw, sent_at=m.sent_at,
            ))
            lines.append(line)
            pos += len(line) + 1
        doc.content = "\n".join(lines)
        doc.chunks = chunks
        docs.append(doc)
    return docs
