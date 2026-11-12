"""Формирование писем и .ics-приглашений, отправка через SMTP (Mailpit)."""

import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

from .config import get_settings
from .schemas import CreateEventPayload


def _escape_ics(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def build_ics(
    *,
    uid: str,
    title: str,
    description: str,
    starts_at: datetime,
    ends_at: datetime,
    organizer_email: str,
    attendee_emails: list[str],
) -> str:
    dtstart = starts_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dtend = ends_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Fokus//Fotoprint//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}@fotoprint.test",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_escape_ics(title)}",
        f"DESCRIPTION:{_escape_ics(description)}",
        f"ORGANIZER:mailto:{organizer_email}",
    ]
    for email in attendee_emails:
        lines.append(f"ATTENDEE;RSVP=TRUE:mailto:{email}")
    lines.extend(["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines)


def build_invite_email(
    *,
    event_id: str,
    payload: CreateEventPayload,
    organizer_email: str,
    recipient_emails: list[str],
) -> EmailMessage:
    s = get_settings()
    tz = ZoneInfo(payload.timezone)
    local_start = payload.starts_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    local_end = payload.ends_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")

    parts = [f"Приглашение на встречу «{payload.title}»"]
    if payload.topic:
        parts.append(f"Тема: {payload.topic}")
    parts.append(f"Время: {local_start} — {local_end} ({payload.timezone})")
    if payload.message.strip():
        parts.append(f"\n{payload.message.strip()}")
    if payload.agenda:
        agenda_lines = []
        for idx, point in enumerate(payload.agenda, start=1):
            suffix = f" ({point.minutes} мин)" if point.minutes else ""
            agenda_lines.append(f"{idx}. {point.title}{suffix}")
        parts.append("\nПовестка:\n" + "\n".join(agenda_lines))
    body_text = "\n".join(parts)

    ics = build_ics(
        uid=event_id,
        title=payload.title,
        description=body_text,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        organizer_email=organizer_email,
        attendee_emails=recipient_emails,
    )

    msg = EmailMessage()
    msg["From"] = s.mail_from
    msg["To"] = ", ".join(recipient_emails)
    msg["Subject"] = f"Приглашение: {payload.title}"[:300]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="fotoprint.test")
    msg.set_content(body_text)
    msg.add_attachment(
        ics,
        subtype="calendar",
        filename="invite.ics",
        params={"method": "REQUEST"},
    )
    return msg


def build_summary_email(*, recipient_email: str, subject: str, body: str) -> EmailMessage:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.mail_from
    msg["To"] = recipient_email
    msg["Subject"] = subject[:300]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="fotoprint.test")
    msg.set_content(body)
    return msg


def send_message(msg: EmailMessage) -> str:
    s = get_settings()
    if not msg.get("Message-ID"):
        msg["Message-ID"] = make_msgid(domain="fotoprint.test")
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
        smtp.send_message(msg)
    return str(msg["Message-ID"])
