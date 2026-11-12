from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from python_multipart.exceptions import MultipartParseError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from .. import audit, stt
from ..access import get_meeting
from ..config import get_settings
from ..db import get_db
from ..models import User
from ..schemas import AudioTranscriptOut, STTStatusOut
from ..security import current_user

router = APIRouter(tags=["audio"])

_MULTIPART = {"requestBody": {"required": True, "content": {"multipart/form-data": {
    "schema": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}},
}}}}
_ERRORS = {
    413: {"description": "Превышен лимит STT_MAX_MB"},
    415: {"description": "Неподдерживаемый формат аудио"},
    422: {"description": "Нет файла или речи в записи"},
    503: {"description": "Распознавание отключено, недоступно или истёк таймаут"},
}


class UploadTooLarge(MultiPartException):
    pass


@router.get("/stt/status", response_model=STTStatusOut)
def status(_: User = Depends(current_user)) -> STTStatusOut:
    settings = get_settings()
    plan = None
    message = None
    try:
        plan = stt.plan_for(settings)
        stt.check_available(settings, plan)
    except stt.STTError as exc:
        message = str(exc)
    return STTStatusOut(configured=settings.stt_provider.strip().lower() not in {"", "off"},
                        available=message is None, route=plan.route if plan else None,
                        max_mb=settings.stt_max_mb, message=message)


async def _parse_upload(request: Request, max_mb: int) -> FormData:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(415, "Отправьте аудиофайл как multipart/form-data в поле file.")
    file_limit = max_mb * 1024 * 1024
    # Multipart-заголовки и границы не входят в лимит самого файла.
    body_limit = file_limit + 64 * 1024
    error = HTTPException(413, f"Запись больше допустимого размера ({max_mb} МБ).")
    declared_size = request.headers.get("content-length", "")
    if declared_size.isdecimal() and int(declared_size) > body_limit:
        raise error

    async def bounded_stream():
        received = 0
        try:
            async for chunk in request.stream():
                received += len(chunk)
                if received > body_limit:
                    # MultiPartParser закрывает частично принятый файл при MultiPartException.
                    raise UploadTooLarge("Request body too large")
                yield chunk
        except ClientDisconnect as exc:
            raise MultiPartException("Upload interrupted") from exc

    try:
        form = await MultiPartParser(request.headers, bounded_stream(), max_files=1, max_fields=0).parse()
    except UploadTooLarge:
        raise error from None
    except (MultiPartException, MultipartParseError):
        raise HTTPException(422, "Загрузите один аудиофайл в поле file.") from None
    upload = form.get("file")
    if isinstance(upload, UploadFile) and upload.size is not None and upload.size > file_limit:
        await form.close()
        raise error
    return form


async def _transcribe(request: Request, db: Session, user: User, meeting_id: str | None = None) -> AudioTranscriptOut:
    settings = get_settings()
    try:
        plan = stt.plan_for(settings)
        stt.check_available(settings, plan)
    except stt.STTError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    form = await _parse_upload(request, settings.stt_max_mb)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile) or not upload.size:
            raise HTTPException(422, "Загрузите непустой аудиофайл в поле file.")
        extension = Path(upload.filename or "").suffix.lower()
        if extension not in stt.AUDIO_FORMATS:
            raise HTTPException(415, "Формат записи не поддерживается. Используйте MP3, M4A, WAV, WebM или OGG.")
        try:
            out = await stt.recognize(upload.file, extension, plan, settings)
        except stt.STTError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
    finally:
        await form.close()

    if meeting_id is not None:
        db.refresh(user)
        get_meeting(db, user, meeting_id)
    audit.write(db, user, "audio.transcribe", object_type="meeting" if meeting_id is not None else "voice",
                object_id=meeting_id if meeting_id is not None else "dictation",
                details={"duration_s": out.duration_s, "provider": plan.provider, "route": plan.route, "model": plan.model})
    return out


@router.post("/meetings/{meeting_id}/audio", response_model=AudioTranscriptOut, openapi_extra=_MULTIPART, responses=_ERRORS)
async def meeting_audio(meeting_id: str, request: Request,
                        user: User = Depends(current_user), db: Session = Depends(get_db)) -> AudioTranscriptOut:
    get_meeting(db, user, meeting_id)
    db.commit()
    return await _transcribe(request, db, user, meeting_id)


@router.post("/voice/audio", response_model=AudioTranscriptOut, openapi_extra=_MULTIPART, responses=_ERRORS)
async def voice_audio(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> AudioTranscriptOut:
    db.commit()
    return await _transcribe(request, db, user)
