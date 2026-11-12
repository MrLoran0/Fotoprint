"""Рабочий чат: простой JSON-экспорт {"messages": [{"id", "author", "author_email"?, "sent_at", "text"}]}.

Так выгружаются чаты Битрикс24/корпоративных мессенджеров после небольшого преобразования.
"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .base import SourceMessage


def parse_chat_json(path: Path, tz: ZoneInfo) -> list[SourceMessage]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[SourceMessage] = []
    for m in data.get("messages", []):
        text = str(m.get("text", "")).strip()
        if not text:
            continue
        sent = datetime.fromisoformat(m["sent_at"])
        sent = sent.replace(tzinfo=tz) if sent.tzinfo is None else sent.astimezone(tz)
        out.append(
            SourceMessage(
                external_id=str(m["id"]),
                sent_at=sent,
                author_raw=str(m.get("author") or "Неизвестно"),
                author_email=(m.get("author_email") or None),
                text=text,
            )
        )
    return out
