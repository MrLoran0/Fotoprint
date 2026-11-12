"""Telegram: экспорт чата из Telegram Desktop (Settings → Export chat history → JSON → result.json).

Формат: {"name", "type", "id", "messages": [{"id", "type": "message", "date", "date_unixtime", "from", "from_id",
"text": str | [str | {"type", "text"}]}]}. Служебные сообщения (type=service) пропускаем.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .base import SourceMessage


def _text(value) -> str:
    if isinstance(value, str):
        return value
    return "".join(part if isinstance(part, str) else str(part.get("text", "")) for part in value or [])


def parse_telegram_export(path: Path, tz: ZoneInfo) -> list[SourceMessage]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[SourceMessage] = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        text = _text(m.get("text")).strip()
        if not text:
            continue
        if m.get("date_unixtime"):
            sent = datetime.fromtimestamp(int(m["date_unixtime"]), tz=timezone.utc).astimezone(tz)
        else:
            sent = datetime.fromisoformat(m["date"])
            sent = sent.replace(tzinfo=tz) if sent.tzinfo is None else sent.astimezone(tz)
        out.append(
            SourceMessage(
                external_id=f"{data.get('id', path.parent.name)}/{m['id']}",
                sent_at=sent,
                author_raw=str(m.get("from") or "Неизвестно"),
                text=text,
            )
        )
    return out
