"""Журнал аудита: кто, когда, что, над каким объектом, с каким результатом.

Чувствительные значения маскируются ДО записи.
"""

import re
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog, User

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?375|8\s?0)[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d[\d\s\-()]{8,16}\d(?!\d)")  # +7 …, +48 … и любые другие международные
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")  # номера банковских карт (13–19 цифр)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b")
_BY_PERSONAL_ID = re.compile(r"\b\d{7}[A-Z]\d{3}[A-Z]{2}\d\b", re.IGNORECASE)  # личный номер паспорта РБ
_BY_PASSPORT = re.compile(r"\b(?:AB|BM|HB|KH|MP|MC|KB|PP|SP|DP)\s?\d{7}\b", re.IGNORECASE)
_RU_PASSPORT = re.compile(r"(?<!\d)\d{2}\s?\d{2}\s?\d{6}(?!\d)")
_SNILS = re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s-]\d{2}(?!\d)")
_SECRET = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{20,}|Bearer\s+[A-Za-z0-9._\-]{16,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+)")
_MONEY_NEAR_SALARY = re.compile(
    r"((?:оклад|зарплат|заработн|премия|ставк)\w*[^.\n]{0,40}?)(\d[\d\s.,]*)",
    re.IGNORECASE,
)


def mask(text: str) -> str:
    text = _SECRET.sub("[секрет скрыт]", text)
    text = _EMAIL.sub(lambda m: m.group(0)[0] + "***@" + m.group(0).split("@")[-1], text)
    text = _IBAN.sub("[счёт скрыт]", text)
    text = _BY_PERSONAL_ID.sub("[личный номер скрыт]", text)
    text = _BY_PASSPORT.sub("[паспорт скрыт]", text)
    text = _SNILS.sub("[СНИЛС скрыт]", text)
    text = _PHONE.sub("+375 ** *** ** **", text)
    text = _PHONE_INTL.sub("[телефон скрыт]", text)
    text = _CARD.sub("[карта скрыта]", text)
    text = _RU_PASSPORT.sub("[паспорт скрыт]", text)
    text = _MONEY_NEAR_SALARY.sub(lambda m: m.group(1) + "***", text)
    return text


def _mask_any(value: Any) -> Any:
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: _mask_any(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_any(v) for v in value]
    return value


def write(
    db: Session,
    user: User | None,
    action: str,
    *,
    object_type: str = "",
    object_id: str = "",
    result: str = "ok",
    details: dict | None = None,
    commit: bool = True,
) -> AuditLog:
    entry = AuditLog(
        user_id=user.id if user else None,
        role=user.role if user else None,
        action=action,
        object_type=object_type,
        object_id=str(object_id),
        result=result,
        details=_mask_any(details or {}),
    )
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()
    return entry
