"""Аутентификация и права. Все проверки — на сервере, при каждом запросе."""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import (
    ACCESS_CONFIDENTIAL,
    ACCESS_DEPARTMENT,
    ACCESS_PARTICIPANTS,
    ACCESS_PUBLIC,
    ROLE_DIRECTOR,
    Document,
    User,
)

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_token(user: User) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": user.id, "role": user.role, "iat": now, "exp": now + timedelta(minutes=s.jwt_ttl_minutes)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется вход")
    try:
        payload = jwt.decode(creds.credentials, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Сессия истекла, войдите снова") from None
    user = db.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Пользователь не найден")
    request.state.user = user
    return user


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
        return user

    return dep


# ---------------------------------------------------------------- ACL документов


def can_read_document(user: User, doc: Document) -> bool:
    """Может ли пользователь открыть документ во вьюере."""
    # Переписку видят только её участники — даже директор не читает чужие чаты и письма.
    if doc.access_level == ACCESS_PARTICIPANTS:
        return user.employee_id is not None and user.employee_id in (doc.allowed_employee_ids or [])
    if user.role == ROLE_DIRECTOR:
        return True
    if doc.access_level == ACCESS_PUBLIC:
        return True
    if doc.access_level == ACCESS_DEPARTMENT:
        return doc.department_id is not None and doc.department_id == user.department_id
    if doc.access_level == ACCESS_CONFIDENTIAL:
        return False
    return False


def can_use_in_ai(user: User, doc: Document) -> bool:
    """Может ли документ попасть в контекст AI для этого пользователя."""
    return doc.ai_allowed and can_read_document(user, doc)


def readable_document_filter(user: User, *, for_ai: bool):
    """SQL-условие для поиска: права применяются ДО ранжирования. Должно совпадать с can_read_document."""
    from sqlalchemy import and_, false, or_

    conds = []
    if for_ai:
        conds.append(Document.ai_allowed.is_(True))
    participant = (
        and_(Document.access_level == ACCESS_PARTICIPANTS, Document.allowed_employee_ids.contains([user.employee_id]))
        if user.employee_id
        else false()
    )
    if user.role == ROLE_DIRECTOR:
        acl = or_(Document.access_level != ACCESS_PARTICIPANTS, participant)
    else:
        acl = or_(
            Document.access_level == ACCESS_PUBLIC,
            and_(
                Document.access_level == ACCESS_DEPARTMENT,
                Document.department_id == user.department_id if user.department_id else false(),
            ),
            participant,
        )
    conds.append(acl)
    return and_(*conds)
