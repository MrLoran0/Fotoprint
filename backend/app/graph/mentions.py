"""Словарные упоминания сотрудников и сущностей в тексте (основа рёбер графа).

Русские словоформы ловим основой слова + коротким окончанием: «Петрова» → «Петровой», «поставщик» → «поставщиком».
Имена людей — с заглавной буквы (чтобы «Лена» не совпала с обычными словами), сущности — без учёта регистра.
"""

import re
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import DocumentChunk, Employee, Entity, Mention


def _stem(word: str, person: bool = False) -> str:
    n = len(word)
    if person and re.search(r"(ов|ев|ёв|ин)а?$", word):
        # фамилии склоняются после «-ов/-ев/-ин» («Белов» → «Беловым», «Петрова» → «Петровой»): основа — мужская форма,
        # иначе короткая основа «Бел…» ловит «БелЛист»
        keep = n - 1 if word.endswith("а") else n
    else:
        keep = max(3, n - 1) if n <= 4 else n - 2
    return re.escape(word[:keep])


def _pattern(name: str, person: bool = False) -> str:
    # короткие слова и слова с цифрами («Б», «№1», «2611») — только точно, иначе «поставщик был» ≈ «Поставщик Б»;
    # окончание — только строчные буквы: «Белов» не совпадает с «БелЛист»
    return r"\s+".join(
        re.escape(w) if len(w) <= 2 or any(ch.isdigit() for ch in w) else _stem(w, person) + r"[а-яёa-z]{0,4}"
        for w in name.split()
    )


@dataclass
class Matcher:
    target_type: str  # employee | entity
    target_id: str
    regex: re.Pattern


def build_matchers(db: Session) -> list[Matcher]:
    out: list[Matcher] = []
    for e in db.scalars(select(Employee)).all():
        names = {e.full_name, *e.aliases, *e.full_name.split()}
        alts = "|".join(sorted((_pattern(n, person=True) for n in names if len(n) >= 3), key=len, reverse=True))
        out.append(Matcher("employee", e.id, re.compile(rf"(?<!\w)(?:{alts})(?!\w)")))
    for ent in db.scalars(select(Entity)).all():
        names = {ent.name, *ent.aliases}
        alts = "|".join(sorted((_pattern(n) for n in names if len(n) >= 3), key=len, reverse=True))
        out.append(Matcher("entity", ent.id, re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)))
    return out


def find_mentions(text: str, matchers: list[Matcher]) -> list[tuple[str, str, str]]:
    found: dict[tuple[str, str], str] = {}
    for m in matchers:
        hit = m.regex.search(text)
        if hit:
            found.setdefault((m.target_type, m.target_id), hit.group(0))
    return [(t, i, s) for (t, i), s in found.items()]


def index_chunks(db: Session, chunks: list[DocumentChunk], matchers: list[Matcher] | None = None) -> int:
    """Пересчитать упоминания для фрагментов. Возвращает число упоминаний."""
    matchers = matchers if matchers is not None else build_matchers(db)
    ids = [c.id for c in chunks]
    if ids:
        db.execute(delete(Mention).where(Mention.chunk_id.in_(ids)))
    n = 0
    for c in chunks:
        for t, i, s in find_mentions(c.text, matchers):
            db.add(Mention(chunk_id=c.id, document_id=c.document_id, target_type=t, target_id=i, surface=s[:200]))
            n += 1
    db.flush()
    return n


def entities_in(text: str, db: Session) -> list[tuple[str, str]]:
    """Какие сотрудники и сущности упомянуты в вопросе пользователя."""
    return [(t, i) for t, i, _ in find_mentions(text, build_matchers(db))]
