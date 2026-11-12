"""Журнал аудита: кто, когда, что, над каким объектом, с каким результатом.

Чувствительные значения маскируются ДО записи.
"""

import re
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog, User

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?375|8\s?0)[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_MONEY_NEAR_SALARY = re.compile(
    r"((?:оклад|зарплат|заработн|премия|ставк)\w*[^.\n]{0,40}?)(\d[\d\s.,]*)",
    re.IGNORECASE,
)


def mask(text: str) -> str:
    text = _EMAIL.sub(lambda m: m.group(0)[0] + "***@" + m.group(0).split("@")[-1], text)
    text = _PHONE.sub("+375 ** *** ** **", text)
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
