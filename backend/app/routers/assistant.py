from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..evidence import sensitive_request
from ..graph.experts import suggest_experts
from ..llm import LLMError, get_gateway
from ..models import User
from ..pipelines.answer import NOT_FOUND, answer
from ..retrieval import search
from ..schemas import AskIn, AskOut
from ..security import current_user

router = APIRouter(tags=["assistant"])


@router.post("/assistant/ask", response_model=AskOut)
async def ask(body: AskIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    gateway = get_gateway()
    if sensitive_request(body.question):
        out = AskOut(status="refused", answer="Оклады и персональные данные запрещены для обработки AI",
                     route=gateway.route, model=gateway.model)
    else:
        candidates = search(db, user, body.question)
        if not candidates:
            # Модель не вызываем: без источников ей нечего подтверждать, а значит — нечего и выдумывать.
            out = AskOut(status="not_found", answer=NOT_FOUND,
                         gaps=[f"В доступных вам документах и сообщениях нет упоминаний по вопросу: «{body.question}»"],
                         route=gateway.route, model=gateway.model)
        else:
            try:
                out = await answer(db, user, gateway, body.question, candidates)
            except LLMError:
                audit.write(db, user, "assistant.ask", result="error", details={"route": gateway.route})
                raise
        if out.status in ("partial", "not_found"):
            out.experts = suggest_experts(db, user, body.question)
    # Не храним текст ответа/цитат в аудите: отзыв ACL не должен раскрыть старый ответ.
    entry = audit.write(db, user, "assistant.ask", result=out.status,
                        details={"route": out.route, "model": out.model, "claims": len(out.claims),
                                 "gaps": len(out.gaps), "experts": [e.employee_id for e in out.experts]})
    out.audit_id = entry.id
    return out
