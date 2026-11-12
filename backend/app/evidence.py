"""Проверка цитат, детектор подозрительных инструкций и разрешение явных полей."""

import re
import unicodedata
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import Employee


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").split())


def quote_matches(quote: str, text: str) -> bool:
    return bool(normalize(quote)) and normalize(quote) in normalize(text)


_INJECTION = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior)|system\s*(?:prompt|message)|"
    r"игнорир\w*\s+(?:все\s+)?(?:предыдущ|инструкц|правил)|"
    r"(?:раскрой|покажи|выведи)\s+(?:все\s+)?(?:парол|секрет|оклад)|"
    r"(?:ты\s+теперь|новая\s+роль|системн\w*\s+инструкц)|<\|(?:system|im_start)\|>",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(r"оклад|зарплат|заработн\w*\s+плат|паспорт|снилс|персональн\w*\s+данн", re.IGNORECASE)


def suspicious(text: str) -> bool:
    return bool(_INJECTION.search(text))


def sensitive_request(text: str) -> bool:
    return bool(_SENSITIVE.search(text))


def resolve_owner(raw: str | None, employees: list[Employee]) -> str | None:
    if not raw:
        return None
    name = normalize(raw)
    matches = [e.id for e in employees if name in {normalize(e.full_name), *(normalize(a) for a in e.aliases)}]
    return matches[0] if len(matches) == 1 else None


_WEEKDAYS = {"понедельника": 0, "понедельник": 0, "вторника": 1, "вторник": 1,
             "среды": 2, "среду": 2, "четверга": 3, "четверг": 3, "пятницы": 4,
             "пятницу": 4, "субботы": 5, "субботу": 5, "воскресенья": 6, "воскресенье": 6}


def resolve_due(raw: str | None, starts_at: datetime | None, timezone: str) -> datetime | None:
    """Без даты встречи относительные сроки не вычисляем. Двусмысленность оставляем человеку."""
    if not raw or starts_at is None:
        return None
    tz = ZoneInfo(timezone)
    base = starts_at.astimezone(tz).date()
    text = normalize(raw)
    text = re.sub(r"^(?:до|к|в)\s+", "", text)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = datetime.strptime(text, "%Y-%m-%d").date()
        elif re.fullmatch(r"\d{1,2}\.\d{1,2}(?:\.\d{4})?", text):
            day = datetime.strptime(text if text.count(".") == 2 else f"{text}.{base.year}", "%d.%m.%Y").date()
        elif text in ("сегодня", "завтра", "послезавтра"):
            day = base + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[text])
        elif text in _WEEKDAYS:
            day = base + timedelta(days=(_WEEKDAYS[text] - base.weekday()) % 7)
        else:
            return None
    except ValueError:
        return None
    return datetime.combine(day, time(18), tzinfo=tz)
