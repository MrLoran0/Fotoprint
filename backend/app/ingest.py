"""Разбор входных материалов: документы Markdown с шапкой, расшифровки, разбиение на фрагменты."""

import re
from dataclasses import dataclass
from datetime import date

_LIST = re.compile(r"^\[(.*)\]$")


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Простая шапка `key: value` между строками `---` (без зависимости от YAML)."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n")
    meta: dict = {}
    end = None
    for i, line in enumerate(parts[1:], start=1):
        if line.strip() == "---":
            end = i
            break
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = _parse_value(value.strip())
    if end is None:
        return {}, text
    return meta, "\n".join(parts[end + 1 :]).strip()


def _parse_value(v: str):
    if v.lower() in ("true", "да"):
        return True
    if v.lower() in ("false", "нет"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return date.fromisoformat(v)
    m = _LIST.match(v)
    if m:
        return [x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip()]
    return v.strip("\"'")


@dataclass
class Chunk:
    ordinal: int
    text: str
    start: int
    end: int


def chunk_text(text: str, target: int = 900, overlap_paragraphs: int = 0) -> list[Chunk]:
    """Режем по абзацам, собирая фрагменты ~target символов. Смещения — в исходном тексте."""
    paragraphs = [(m.start(), m.end()) for m in re.finditer(r"[^\n](?:.|\n(?!\s*\n))*", text)]
    chunks: list[Chunk] = []
    cur_start = cur_end = None
    for start, end in paragraphs:
        if cur_start is None:
            cur_start, cur_end = start, end
            continue
        if end - cur_start > target:
            chunks.append(Chunk(len(chunks), text[cur_start:cur_end].strip(), cur_start, cur_end))
            cur_start, cur_end = start, end
        else:
            cur_end = end
    if cur_start is not None:
        chunks.append(Chunk(len(chunks), text[cur_start:cur_end].strip(), cur_start, cur_end))
    return [c for c in chunks if c.text]


_SEGMENT = re.compile(r"^\s*(?:\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?(?P<speaker>[^:\[\]\n]{2,60}?):\s*(?P<text>.+)$")


def parse_transcript(raw: str) -> list[dict]:
    """Строки вида `[00:14] Илья Морозов: текст` или `Илья Морозов: текст`.
    Строка без говорящего приклеивается к предыдущей реплике. Таймкоды не придумываем."""
    segments: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        m = _SEGMENT.match(line)
        if m:
            segments.append(
                {"n": len(segments) + 1, "speaker": m.group("speaker").strip(), "text": m.group("text").strip(), "ts": m.group("ts")}
            )
        elif segments:
            segments[-1]["text"] += " " + line.strip()
        else:
            segments.append({"n": 1, "speaker": "Неизвестно", "text": line.strip(), "ts": None})
    return segments
