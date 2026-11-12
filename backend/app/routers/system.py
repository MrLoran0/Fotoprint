import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import __version__
from ..config import get_settings
from ..db import get_db
from ..llm import get_gateway
from ..schemas import ContractBundle, HealthOut, LLMStatus

router = APIRouter(tags=["system"])


@router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/contract", response_model=ContractBundle, tags=["contract"])
def contract() -> ContractBundle:
    """Служебный: публикует все типы контракта в OpenAPI для генерации типов фронтенда. Убрать к сдаче."""
    raise HTTPException(501, "Служебный эндпоинт: только схема")


@router.get("/health", response_model=HealthOut)
async def health(db: Session = Depends(get_db)) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    gw = get_gateway()
    st = await gw.status()
    smtp_ok = await _smtp_ok()
    llm = LLMStatus(provider=gw.provider, model=gw.model, route=gw.route, **st)
    return HealthOut(
        status="ok" if db_ok and llm.reachable and smtp_ok else "degraded",
        version=__version__,
        db=db_ok,
        llm=llm,
        scenario_now=get_settings().now(),
        smtp=smtp_ok,
    )


async def _smtp_ok() -> bool:
    """Почтовый шлюз принимает соединения (без отправки писем)."""
    s = get_settings()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(s.smtp_host, s.smtp_port), timeout=2)
        writer.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False
