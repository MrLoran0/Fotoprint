from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..models import User
from ..schemas import DemoAccountOut, LoginIn, TokenOut, UserOut
from ..security import create_token, current_user, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

ROLE_LABELS = {"director": "Директор", "production_head": "Руководитель производства"}


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    user = db.scalar(select(User).where(User.login == body.login))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        audit.write(db, None, "auth.login", object_type="user", object_id=body.login, result="denied")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")
    audit.write(db, user, "auth.login", object_type="user", object_id=user.id)
    return TokenOut(access_token=create_token(user), user=UserOut.model_validate(user))


@router.get("/demo-accounts", response_model=list[DemoAccountOut])
def demo_accounts(db: Session = Depends(get_db)) -> list[DemoAccountOut]:
    """Тестовые учётки для быстрого входа на стенде (без паролей; пароль — в README/.env)."""
    if not get_settings().demo_accounts_public:
        return []
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.role)).all()
    return [DemoAccountOut(login=u.login, full_name=u.full_name, role=u.role, role_label=ROLE_LABELS.get(u.role, u.role)) for u in users]


me_router = APIRouter(tags=["auth"])


@me_router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)
