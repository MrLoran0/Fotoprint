"""Загрузка синтетических данных из каталога data/ (формат — data/README.md).

Отсутствующие файлы пропускаются: можно запускаться, пока набор ещё пишется.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .config import get_settings
from .ingest import chunk_text, parse_front_matter, parse_transcript
from .evidence import suspicious
from .security import hash_password

log = logging.getLogger("fokus.seed")


def _load(path: Path) -> list | dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def is_seeded(db: Session) -> bool:
    return db.scalar(select(m.User.id).limit(1)) is not None


def seed_all(db: Session, data_dir: Path | None = None) -> dict:
    s = get_settings()
    root = data_dir or s.data_dir
    counts: dict[str, int] = {}

    for d in _load(root / "departments.json") or []:
        db.merge(m.Department(**d))
    db.flush()

    for e in _load(root / "employees.json") or []:
        db.merge(m.Employee(**e))
    db.flush()

    pwd = hash_password(s.demo_password)
    for u in _load(root / "users.json") or []:
        db.merge(m.User(password_hash=pwd, **u))
    db.flush()

    for ent in _load(root / "glossary.json") or []:
        db.merge(m.Entity(**ent))
    db.flush()

    counts["documents"] = _seed_documents(db, root / "documents")
    # упоминания людей и сущностей в документах — рёбра графа
    from .graph.mentions import index_chunks

    index_chunks(db, list(db.scalars(select(m.DocumentChunk).join(m.Document).where(m.Document.channel_id.is_(None))).all()))
    counts["sources"] = _seed_sources(db, root)
    counts["meetings"] = _seed_meetings(db, root)

    for t in _load(root / "tasks.json") or []:
        t = dict(t)
        t["due_at"] = _dt(t.get("due_at"))
        t["completed_at"] = _dt(t.get("completed_at"))
        db.merge(m.Task(**t))

    for dec in _load(root / "decisions.json") or []:
        dec = dict(dec)
        for key in ("due_at", "created_at", "effective_from"):
            dec[key] = _dt(dec.get(key))
        db.merge(m.Decision(**dec))
    db.flush()

    for ev in _load(root / "calendar.json") or []:
        ev = dict(ev)
        ev["starts_at"], ev["ends_at"] = _dt(ev["starts_at"]), _dt(ev["ends_at"])
        db.merge(m.CalendarEvent(**ev))

    _seed_kpi(db, root / "kpi")
    db.commit()

    for name, model in [("departments", m.Department), ("employees", m.Employee), ("users", m.User), ("tasks", m.Task), ("events", m.CalendarEvent)]:
        counts[name] = len(db.scalars(select(model.id)).all())
    log.info("seed: %s", counts)
    return counts


def _seed_documents(db: Session, folder: Path) -> int:
    if not folder.exists():
        return 0
    n = 0
    for path in sorted(folder.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        if "id" not in meta:
            log.warning("документ без id в шапке пропущен: %s", path.name)
            continue
        doc = m.Document(
            id=str(meta["id"]),
            title=str(meta.get("title", path.stem)),
            version=int(meta.get("version", 1)),
            doc_date=meta.get("date") if isinstance(meta.get("date"), date) else date.today(),
            author=str(meta.get("author", "")),
            access_level=str(meta.get("access", m.ACCESS_PUBLIC)),
            department_id=meta.get("department") or None,
            ai_allowed=bool(meta.get("ai_allowed", True)),
            content=body,
            source_path=f"data/documents/{path.name}",
            tags=meta.get("tags", []) if isinstance(meta.get("tags"), list) else [],
        )
        existing = db.get(m.Document, doc.id)
        if existing:
            db.delete(existing)
            db.flush()
        doc.chunks = [
            m.DocumentChunk(id=f"{doc.id}#{c.ordinal}", ordinal=c.ordinal, text=c.text, char_start=c.start, char_end=c.end,
                            injection_flag=suspicious(c.text))
            for c in chunk_text(body)
        ]
        db.add(doc)
        n += 1
    db.flush()
    return n


def _seed_sources(db: Session, root: Path) -> int:
    """Каналы из data/sources/channels.json и их синхронизация (почта, Telegram, чаты)."""
    from .connectors.sync import sync_channel

    channels = _load(root / "sources" / "channels.json") or []
    for ch in channels:
        channel = db.merge(m.SourceChannel(**ch))
        db.flush()
        sync_channel(db, channel, root / "sources")
    return len(channels)


def _seed_meetings(db: Session, root: Path) -> int:
    meetings = _load(root / "meetings.json") or []
    for mt in meetings:
        mt = dict(mt)
        transcript_file = mt.pop("transcript_file", None)
        mt["starts_at"], mt["ends_at"] = _dt(mt.get("starts_at")), _dt(mt.get("ends_at"))
        db.merge(m.Meeting(**mt))
        db.flush()
        if transcript_file:
            path = root / "transcripts" / transcript_file
            if path.exists():
                meta, raw = parse_front_matter(path.read_text(encoding="utf-8"))
                db.merge(
                    m.Transcript(id=f"tr-{mt['id']}", meeting_id=mt["id"], source="text", raw_text=raw, segments=parse_transcript(raw))
                )
    db.flush()
    return len(meetings)


def _seed_kpi(db: Session, folder: Path) -> None:
    if not folder.exists():
        return
    for src in _load(folder / "sources.json") or []:
        src = dict(src)
        src["updated_at"] = _dt(src["updated_at"])
        db.merge(m.DataSource(**src))
    for q in _load(folder / "quality_checks.json") or []:
        q = dict(q)
        q["checked_on"] = date.fromisoformat(q["checked_on"])
        db.merge(m.QualityCheck(**q))
    for o in _load(folder / "orders.json") or []:
        o = dict(o)
        for k in ("created_at", "promised_at", "completed_at"):
            o[k] = _dt(o.get(k))
        db.merge(m.Order(**o))
    for t in _load(folder / "support_tickets.json") or []:
        t = dict(t)
        for k in ("created_at", "sla_due_at", "first_response_at"):
            t[k] = _dt(t.get(k))
        db.merge(m.SupportTicket(**t))
    db.flush()
