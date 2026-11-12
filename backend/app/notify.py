"""Письма по карточкам: пакет напоминаний о просрочках и утренний брифинг.

Как и итоги встречи: письмо уходит только после подтверждения карточки; при сбое SMTP при повторе
досылаются только недоставленные письма. Модуль не импортирует actions (без циклов): actions вызывает его.
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, mailer
from .access import get_task
from .models import ActionProposal, DeliveryJob, Employee, User, new_id
from .schemas import SendBriefPayload, SendRemindersPayload

OPEN = ("assigned", "accepted", "in_progress")


def validate_reminders(db: Session, user: User, payload: dict, *, lock: bool = False):
    try:
        parsed = SendRemindersPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "Некорректное содержимое карточки") from exc
    seen = set()
    for letter in parsed.letters:
        emp = db.get(Employee, letter.recipient_id)
        if emp is None or not emp.email or letter.recipient_id in seen:
            raise HTTPException(422, "Получатели напоминаний должны быть разными сотрудниками из справочника")
        seen.add(letter.recipient_id)
        for task_id in letter.task_ids:
            task = get_task(db, user, task_id, lock=lock)  # права на каждое поручение — заново
            if task.owner_employee_id != letter.recipient_id or task.status not in OPEN:
                raise HTTPException(409, "Поручение изменилось: обновите пакет напоминаний")
    return None, parsed.model_dump(mode="json")


def validate_brief(db: Session, user: User, payload: dict, *, lock: bool = False):
    try:
        parsed = SendBriefPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "Некорректное содержимое карточки") from exc
    if parsed.recipient_id != user.employee_id or db.get(Employee, parsed.recipient_id) is None:
        raise HTTPException(422, "Брифинг отправляется только себе")
    return None, parsed.model_dump(mode="json")


def execute_letters(db: Session, user: User, action: ActionProposal, letters: list[tuple[str, str, str]]):
    """letters: (recipient_id, subject, body). Досылает только тем, кому ещё не ушло."""
    action.status = "executing"
    if action.confirmed_by is None:
        action.confirmed_by = user.id
        action.confirmed_at = datetime.now(timezone.utc)
    db.flush()
    job = db.scalar(select(DeliveryJob).where(DeliveryJob.action_id == action.id).with_for_update())
    if job is None:
        job = DeliveryJob(id=new_id("dj"), action_id=action.id, channel="email",
                          recipients=[db.get(Employee, r).email for r, _, _ in letters],
                          subject=letters[0][1] if len(letters) == 1 else f"{letters[0][1]} (+{len(letters) - 1})",
                          status="queued", attempts=0)
        db.add(job)
        db.flush()
    sent = list((action.result or {}).get("sent_recipient_ids", []))
    ids = list((action.result or {}).get("message_ids", []))
    job.attempts += 1
    job.updated_at = datetime.now(timezone.utc)
    try:
        for recipient_id, subject, body in letters:
            if recipient_id in sent:
                continue
            emp = db.get(Employee, recipient_id)
            ids.append(mailer.send_message(mailer.build_summary_email(recipient_email=emp.email, subject=subject, body=body)))
            sent.append(recipient_id)
        job.status, job.last_error, job.message_id = "sent", None, (ids[-1] if ids else None)
        action.status, action.error = "executed", None
        result = "confirmed"
    except Exception as exc:  # noqa: BLE001 — сбой SMTP виден в статусе карточки и доставки
        job.status, job.last_error = "failed", str(exc)
        action.status = "partially_failed" if sent else "failed"
        action.error = f"Не удалось отправить письма: {exc}"
        result = "error"
    action.result = {"delivery_job_id": job.id, "delivery_status": job.status, "sent_count": len(sent),
                     "sent_recipient_ids": sent, "message_ids": ids}
    audit.write(db, user, "action.confirm", object_type="action", object_id=action.id, result=result,
                details={"type": action.type, "revision": action.revision, "sent_count": len(sent)}, commit=False)
    db.commit()
