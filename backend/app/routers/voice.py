from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import User
from ..pipelines.voice import structure_voice_tasks
from ..schemas import VoiceStructureOut, VoiceTaskIn
from ..security import current_user

router = APIRouter(tags=["voice"])


@router.post("/voice/structure", response_model=VoiceStructureOut)
def voice_structure(
    body: VoiceTaskIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> VoiceStructureOut:
    """Структурирует голосовую речь пользователя в карточки ActionProposal (delegate_task).
    
    Модель и правила только предлагают структуру; задачи попадают в реестр
    только после подтверждения пользователем карточки действия.
    """
    out = structure_voice_tasks(db, user, body.text, body.audio_origin)
    audit.write(
        db,
        user,
        "voice.structure",
        object_type="voice",
        object_id="dictation",
        details={
            "length": len(body.text),
            "tasks_count": len(out.tasks),
            "actions_count": len(out.action_proposals),
            "audio_origin": body.audio_origin,
        },
    )
    return out
