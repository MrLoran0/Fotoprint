"""Поиск по разрешённым источникам: полнотекстовый PostgreSQL + расширение по графу упоминаний.

ACL и ai_allowed применяются ДО ранжирования — в SQL обоих путей. Расширение по графу добавляет фрагменты,
где упомянуты сотрудники и сущности из вопроса («что с БелЛистом?» → сообщения и письма про «БелЛист»),
даже если в них нет тех же слов.
"""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .graph.mentions import entities_in
from .models import Document, DocumentChunk, Mention, User
from .security import readable_document_filter

_STOPWORDS = {"какой", "какая", "какие", "какое", "когда", "почему", "который", "которые", "чему", "чего", "есть", "было", "были", "для", "как", "что", "это", "мне", "про", "или", "так", "при", "пожалуйста", "расскажи"}


def _allowed(user: User):
    return (select(DocumentChunk.id).join(Document).where(readable_document_filter(user, for_ai=True))
            .cte("allowed_chunks").prefix_with("MATERIALIZED"))


def search(db: Session, user: User, question: str, limit: int = 5) -> list[tuple[Document, DocumentChunk]]:
    terms = [t for t in re.findall(r"[\w-]{3,}", question.casefold()) if t not in _STOPWORDS][:40]
    rows: list[tuple[Document, DocumentChunk]] = []
    if terms:
        # OR обеспечивает кандидатов для естественного вопроса; ts_rank_cd ранжирует совпадения.
        query = func.websearch_to_tsquery("russian", " OR ".join(terms))
        allowed = _allowed(user)
        rows = list(db.execute(
            select(Document, DocumentChunk).join(DocumentChunk, DocumentChunk.document_id == Document.id)
            .join(allowed, allowed.c.id == DocumentChunk.id)
            .where(DocumentChunk.tsv.op("@@")(query))
            .order_by(func.ts_rank_cd(DocumentChunk.tsv, query).desc(), Document.doc_date.desc(), DocumentChunk.id)
            .limit(limit)
        ).all())
    return _merge(rows, graph_neighbors(db, user, question, limit=limit), limit + 2)


def graph_neighbors(db: Session, user: User, question: str, limit: int = 5) -> list[tuple[Document, DocumentChunk]]:
    """Фрагменты, где упомянуты сущности/люди из вопроса. Чем больше совпавших сущностей — тем выше."""
    targets = entities_in(question, db)
    if not targets:
        return []
    ids = [t_id for _, t_id in targets]
    allowed = _allowed(user)
    hits = func.count(func.distinct(Mention.target_id)).label("hits")
    ranked = (select(Mention.chunk_id, hits).join(allowed, allowed.c.id == Mention.chunk_id)
              .where(Mention.target_id.in_(ids)).group_by(Mention.chunk_id).subquery())
    return [(d, c) for d, c in db.execute(
        select(Document, DocumentChunk).join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .join(ranked, ranked.c.chunk_id == DocumentChunk.id)
        .order_by(ranked.c.hits.desc(), func.coalesce(DocumentChunk.sent_at, func.cast(Document.doc_date, DocumentChunk.sent_at.type)).desc())
        .limit(limit)
    ).all()]


def _merge(primary, extra, limit):
    seen, out = set(), []
    for doc, chunk in [*primary, *extra]:
        if chunk.id not in seen:
            seen.add(chunk.id)
            out.append((doc, chunk))
    return out[:limit]
