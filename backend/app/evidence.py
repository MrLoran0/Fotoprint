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


# --- Цитата должна подтверждать СМЫСЛ утверждения, а не только существовать в источнике.
# Модель может взять дословную цитату-обещание («направим до 13.11») и выдать её за факт («сертификат поступил»).
# Проверяем модальность детерминированно: источник «обещание/план/предложение», утверждение — «свершившийся факт/решение».
_PROMISE = re.compile(
    r"\b(?:пришл(?:ю|ем|ет|ют)|направ(?:лю|им|ит|ят)|отправ(?:лю|им|ит|ят)|запрош(?:у|ем)|сдела(?:ю|ем)|подготов(?:лю|им)|"
    r"предостав(?:лю|им)|буд(?:ет|ут|у|ем)|планиру\w*|обеща\w*|собира\w*|постара\w*|ожида\w*|должн\w*|"
    r"предлага\w*|предложи\w*|предлож\w*|можно\s+бы|стоит\s+бы)\b",
    re.IGNORECASE,
)
_DONE = re.compile(
    r"\b(?:получен\w*|поступ(?:ил|ила|ило|или)\w*|прибыл\w*|доставлен\w*|отправлен\w*|направлен\w*|выполнен\w*|сделан\w*|"
    r"готов(?:а|о|ы)?|пришл(?:о|а|и)\b|пришел|прислал\w*|утвержд\w*|принят\w*|решили|решено|закрыт\w*|завершен\w*|"
    r"подписан\w*|оплачен\w*|уже\s+есть|имеется)",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(?:обеща\w*|планиру\w*|план\w*|собира\w*|предлага\w*|предлож\w*|намерен\w*|ожида\w*|должн\w*|буд(?:ет|ут)|"
    r"пришл(?:ет|ют)|направ(?:ит|ят)|отправ(?:ит|ят)|до\s+\d|к\s+\d|не\s+(?:получен|поступ|подтвержд))",
    re.IGNORECASE,
)
_STOP = {"который", "которая", "которые", "также", "этого", "этот", "была", "было", "были", "будет", "после", "перед",
         "через", "только", "очень", "между", "более", "менее", "чтобы", "когда", "почему"}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яa-z0-9]+", normalize(text)) if len(w) >= 4 and w not in _STOP}


def claim_supported(claim: str, quotes: list[str]) -> tuple[bool, str]:
    """Подтверждают ли цитаты утверждение по смыслу. Возвращает (да/нет, причина для пользователя)."""
    joined = " ".join(quotes)
    if not (_stems(claim) & _stems(joined)):
        return False, "цитата не относится к утверждению"
    src, cl = normalize(joined), normalize(claim)
    if _PROMISE.search(src) and not _DONE.search(src) and _DONE.search(cl) and not _HEDGE.search(cl):
        return False, "источник подтверждает только обещание, план или предложение, а не свершившийся факт"
    return True, ""


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
