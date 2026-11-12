"""Подграф вокруг фокуса (сотрудник, сущность, поручение, встреча) — только из того, что пользователь может видеть.

Каждое ребро несёт первоисточник (SourceRef): откуда известна связь. Нет первоисточника — нет ребра.
"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..access import get_meeting, get_task, meeting_filter, task_filter
from ..models import (
    Document,
    DocumentChunk,
    Employee,
    Entity,
    Meeting,
    Mention,
    Protocol,
    ProtocolItem,
    Task,
    Transcript,
    User,
)
from ..schemas import GraphEdge, GraphNode, GraphOut, SourceRef
from ..security import readable_document_filter
from .mentions import build_matchers, find_mentions

MAX_DOCS = 40


class _G:
    def __init__(self, focus: str):
        self.focus = focus
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[tuple, GraphEdge] = {}

    def node(self, type_: str, ref: str, label: str, **meta) -> str:
        nid = f"{type_}:{ref}"
        if nid not in self.nodes:
            self.nodes[nid] = GraphNode(id=nid, type=type_, label=label[:90], ref_id=ref, meta=meta)
        return nid

    def edge(self, src: str, dst: str, type_: str, label: str, evidence: SourceRef | None = None):
        self.edges.setdefault((src, dst, type_), GraphEdge(source=src, target=dst, type=type_, label=label, evidence=evidence))

    def out(self) -> GraphOut:
        return GraphOut(focus=self.focus, nodes=list(self.nodes.values()), edges=list(self.edges.values()))


def _chunk_ref(doc: Document, chunk: DocumentChunk) -> SourceRef:
    return SourceRef(source_type=doc.kind, source_id=doc.id, title=doc.title, version=doc.version, source_date=doc.doc_date,
                     chunk_id=chunk.id, quote=chunk.text[:240], author=chunk.author_raw, sent_at=chunk.sent_at,
                     channel_id=doc.channel_id)


def _doc_node(g: _G, doc: Document) -> str:
    return g.node(doc.kind, doc.id, doc.title, date=doc.doc_date.isoformat(), channel_id=doc.channel_id)


def _employee_node(g: _G, e: Employee) -> str:
    return g.node("employee", e.id, e.full_name, position=e.position, department_id=e.department_id)


def _task_node(g: _G, db: Session, user: User, t: Task) -> str:
    nid = g.node("task", t.id, t.title, status=t.status, due_at=t.due_at.isoformat() if t.due_at else None)
    if t.owner_employee_id and (owner := db.get(Employee, t.owner_employee_id)):
        g.edge(_employee_node(g, owner), nid, "owns", "ответственный")
    if t.meeting_id:
        m = db.scalar(select(Meeting).where(Meeting.id == t.meeting_id, meeting_filter(user)))
        if m:
            g.edge(nid, g.node("meeting", m.id, m.title, starts_at=m.starts_at.isoformat() if m.starts_at else None),
                   "from_meeting", "поручено на встрече")
    return nid


def _mentions_of(db: Session, user: User, target_ids: list[str]):
    return db.execute(
        select(Document, DocumentChunk).join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .join(Mention, Mention.chunk_id == DocumentChunk.id)
        .where(Mention.target_id.in_(target_ids), readable_document_filter(user, for_ai=False))
        .order_by(DocumentChunk.sent_at.desc().nullslast(), Document.doc_date.desc())
        .limit(MAX_DOCS)
    ).all()


def _add_mentions(g: _G, db: Session, user: User, focus_node: str, target_ids: list[str]):
    for doc, chunk in _mentions_of(db, user, target_ids):
        ref = _chunk_ref(doc, chunk)
        dnode = _doc_node(g, doc)
        g.edge(dnode, focus_node, "mentions", "упоминает", ref)
        if chunk.author_employee_id and (author := db.get(Employee, chunk.author_employee_id)):
            g.edge(_employee_node(g, author), dnode, "authored", "написал(а)", ref)


def _item_ref(db: Session, item: ProtocolItem, protocol: Protocol) -> SourceRef:
    tr = db.get(Transcript, protocol.transcript_id)
    return SourceRef(source_type="transcript", source_id=protocol.transcript_id, title=f"Расшифровка встречи {protocol.meeting_id}",
                     segment_id=item.segment_id, quote=item.evidence_quote, source_date=tr.created_at.date() if tr else None)


def build_graph(db: Session, user: User, focus: str) -> GraphOut:
    kind, _, ref = focus.partition(":")
    g = _G(focus)
    if kind == "employee":
        e = db.get(Employee, ref)
        if e is None:
            raise HTTPException(404, "Сотрудник не найден")
        center = _employee_node(g, e)
        _add_mentions(g, db, user, center, [e.id])
        for t in db.scalars(select(Task).where(task_filter(user), Task.owner_employee_id == e.id)).all():
            _task_node(g, db, user, t)
        for m in db.scalars(select(Meeting).where(meeting_filter(user))).all():
            if e.id in (m.participant_ids or []):
                g.edge(center, g.node("meeting", m.id, m.title, starts_at=m.starts_at.isoformat() if m.starts_at else None),
                       "participates", "участник")
    elif kind == "entity":
        ent = db.get(Entity, ref)
        if ent is None:
            raise HTTPException(404, "Сущность не найдена")
        center = g.node("entity", ent.id, ent.name, entity_type=ent.type)
        _add_mentions(g, db, user, center, [ent.id])
        matchers = [m for m in build_matchers(db) if m.target_type == "entity" and m.target_id == ent.id]
        for t in db.scalars(select(Task).where(task_filter(user))).all():
            if find_mentions(t.title, matchers):
                g.edge(_task_node(g, db, user, t), center, "about", "про")
        rows = db.execute(select(ProtocolItem, Protocol, Meeting).join(Protocol, Protocol.id == ProtocolItem.protocol_id)
                          .join(Meeting, Meeting.id == Protocol.meeting_id)
                          .where(meeting_filter(user), Protocol.status == "published", ProtocolItem.removed.is_(False))).all()
        for item, protocol, meeting in rows:
            if find_mentions(item.statement + " " + item.evidence_quote, matchers):
                inode = g.node("protocol_item", item.id, item.statement, kind=item.kind, decision_status=item.decision_status)
                g.edge(inode, center, "about", "про", _item_ref(db, item, protocol))
                mnode = g.node("meeting", meeting.id, meeting.title, starts_at=meeting.starts_at.isoformat() if meeting.starts_at else None)
                g.edge(inode, mnode, "item_of", "решено/обсуждено на встрече")
    elif kind == "task":
        t = get_task(db, user, ref)
        center = _task_node(g, db, user, t)
        if t.protocol_item_id and (item := db.get(ProtocolItem, t.protocol_item_id)):
            protocol = db.get(Protocol, item.protocol_id)
            g.edge(center, g.node("protocol_item", item.id, item.statement, kind=item.kind), "from_item", "основание",
                   _item_ref(db, item, protocol))
        targets = [i for typ, i, _ in find_mentions(t.title, build_matchers(db)) if typ == "entity"]
        for ent_id in targets:
            ent = db.get(Entity, ent_id)
            g.edge(center, g.node("entity", ent.id, ent.name, entity_type=ent.type), "about", "про")
        # контекст: сообщения и письма про те же сущности (что говорили о поручении вне встреч)
        for ent_id in targets:
            _add_mentions(g, db, user, f"entity:{ent_id}", [ent_id])
    elif kind == "meeting":
        m = get_meeting(db, user, ref)
        center = g.node("meeting", m.id, m.title, starts_at=m.starts_at.isoformat() if m.starts_at else None)
        for e in db.scalars(select(Employee).where(Employee.id.in_(m.participant_ids or []))).all():
            g.edge(_employee_node(g, e), center, "participates", "участник")
        for t in db.scalars(select(Task).where(task_filter(user), Task.meeting_id == m.id)).all():
            _task_node(g, db, user, t)
    else:
        raise HTTPException(422, "Центр графа: employee:<id> | entity:<id> | task:<id> | meeting:<id>")
    return g.out()


def focus_options(db: Session) -> list[dict]:
    """Что можно выбрать центром графа: сотрудники и сущности (справочники открыты всем)."""
    out = [{"focus": f"employee:{e.id}", "label": e.full_name, "type": "employee"}
           for e in db.scalars(select(Employee).order_by(Employee.full_name)).all()]
    out += [{"focus": f"entity:{e.id}", "label": e.name, "type": e.type}
            for e in db.scalars(select(Entity).order_by(Entity.type, Entity.name)).all()]
    return out
