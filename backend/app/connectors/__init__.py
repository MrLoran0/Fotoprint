"""Коннекторы источников: почта, Telegram, рабочие чаты.

Каждый коннектор читает стандартный формат и отдаёт список SourceMessage; sync.py превращает их
в документы с правами участников, фрагментами по сообщениям и упоминаниями для графа.

| Коннектор         | Хакатон (синтетика)                     | Пилот (тот же код разбора + транспорт)      |
|-------------------|-----------------------------------------|---------------------------------------------|
| email_eml         | папка .eml (RFC 5322)                   | IMAP / Exchange / Microsoft Graph           |
| telegram_export   | Telegram Desktop export result.json     | Telegram Bot API (группа с ботом)           |
| chat_json         | JSON-экспорт чата                       | Битрикс24 / корпоративный мессенджер (API)  |
"""

from .base import SourceMessage
from .chat_json import parse_chat_json
from .email_eml import parse_eml_folder
from .telegram_export import parse_telegram_export

PARSERS = {
    "email_eml": parse_eml_folder,
    "telegram_export": parse_telegram_export,
    "chat_json": parse_chat_json,
}

__all__ = ["PARSERS", "SourceMessage"]
