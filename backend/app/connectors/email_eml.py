"""Почта: папка с .eml (стандарт RFC 5322 — так сохраняет любой почтовый клиент; IMAP отдаёт то же самое)."""

import email
import email.policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .base import SourceMessage


def parse_eml_folder(path: Path, tz: ZoneInfo) -> list[SourceMessage]:
    out: list[SourceMessage] = []
    for file in sorted(path.glob("*.eml")):
        msg = email.message_from_bytes(file.read_bytes(), policy=email.policy.default)
        name, addr = parseaddr(str(msg.get("From", "")))
        recipients = [a.lower() for _, a in getaddresses([str(v) for v in msg.get_all("To", []) + msg.get_all("Cc", [])]) if a]
        sent = parsedate_to_datetime(str(msg["Date"])) if msg.get("Date") else None
        if sent is None:
            raise ValueError(f"{file.name}: нет заголовка Date")
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=tz)
        body_part = msg.get_body(preferencelist=("plain",))
        body = body_part.get_content().strip() if body_part is not None else ""
        out.append(
            SourceMessage(
                external_id=str(msg.get("Message-ID") or file.name).strip("<> "),
                sent_at=sent.astimezone(tz),
                author_raw=name or addr,
                author_email=addr.lower() or None,
                subject=str(msg.get("Subject", "")).strip() or "(без темы)",
                recipients=recipients,
                text=body,
            )
        )
    return out
