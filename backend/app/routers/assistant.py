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
    tools: list[str] = []
    candidates: list = []
    if sensitive_request(body.question):
        out = AskOut(status="refused", answer="Оклады и персональные данные запрещены для обработки AI",
                     route=gateway.route, model=gateway.model)
    else:
        candidates = search(db, user, body.question)
        tools.append("retrieval.search: полнотекстовый поиск + расширение по графу (только доступное пользователю)")
        if not candidates:
            # Модель не вызываем: без источников ей нечего подтверждать, а значит — нечего и выдумывать.
            out = AskOut(status="not_found", answer=NOT_FOUND,
                         gaps=[f"В доступных вам документах и сообщениях нет упоминаний по вопросу: «{body.question}»"],
                         route=gateway.route, model=gateway.model)
        else:
            try:
                out = await answer(db, user, gateway, body.question, candidates)
            except LLMError:
                audit.write(db, user, "assistant.ask", result="error",
                            details={**_trace(body.question, tools, candidates, []), "route": gateway.route, "model": gateway.model})
                raise
        if out.status in ("partial", "not_found"):
            out.experts = suggest_experts(db, user, body.question)
            tools.append("graph.experts: подбор «кто может знать» по доступным источникам")
    # Текст ответа и цитат в аудит не пишем (отзыв прав не должен раскрыть старый ответ). Пишем вопрос (маскированный),
    # какие фрагменты нашёл поиск и какие реально процитированы, какие инструменты работали — этого хватает для разбора.
    cited = [s.chunk_id for c in out.claims for s in c.sources if s.chunk_id]
    entry = audit.write(db, user, "assistant.ask", result=out.status,
                        details={**_trace(body.question, tools, candidates, cited),
                                 "route": out.route, "model": out.model, "claims": len(out.claims),
                                 "gaps": len(out.gaps), "experts": [e.employee_id for e in out.experts]})
    out.audit_id = entry.id
    return out


def _trace(question: str, tools: list[str], candidates: list, cited: list[str]) -> dict:
    return {"question": question[:500], "tools": tools,
            "candidate_chunks": [chunk.id for _, chunk in candidates], "cited_chunks": list(dict.fromkeys(cited))}
