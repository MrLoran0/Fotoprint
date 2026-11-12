from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SourceMessage:
    """Одно сообщение из любого канала в едином виде."""

    external_id: str
    sent_at: datetime  # всегда с часовым поясом
    author_raw: str
    text: str
    author_email: str | None = None
    subject: str | None = None
    recipients: list[str] = field(default_factory=list)  # e-mail получателей (для почты)
