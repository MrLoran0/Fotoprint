"""Явный выбор маршрута распознавания и запуск с ограничением времени."""

import asyncio
import os
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from typing import BinaryIO, Literal

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..schemas import AudioSegment, AudioTranscriptOut, Route

AUDIO_FORMATS = {".mp3": "mp3", ".m4a": "mov", ".wav": "wav", ".webm": "matroska", ".ogg": "ogg"}
WHISPER_SLOT = asyncio.Lock()


class STTError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class STTPlan:
    provider: Literal["whisper", "openai", "gemini"]
    route: Route
    model: str


class Recognition(BaseModel):
    segments: list[AudioSegment]
    duration_s: float
    warnings: list[str]


def plan_for(settings: Settings) -> STTPlan:
    match settings.stt_provider.strip().lower():
        case "whisper":
            return STTPlan("whisper", "local", settings.stt_whisper_model_dir.name)
        case "openai":
            return STTPlan("openai", "external", settings.stt_openai_model)
        case "gemini":
            return STTPlan("gemini", "external", settings.stt_gemini_model)
        case "" | "off":
            raise STTError(503, "Распознавание отключено. Настройте STT_PROVIDER или вставьте расшифровку вручную.")
        case _:
            raise STTError(503, "Неизвестный STT_PROVIDER. Доступны whisper, openai, gemini и off.")


def check_available(settings: Settings, plan: STTPlan) -> None:
    if plan.provider == "whisper":
        model_files = ("model.bin", "config.json", "tokenizer.json")
        if find_spec("faster_whisper") is None or not all(
            (settings.stt_whisper_model_dir / name).is_file() for name in model_files
        ):
            raise STTError(503, "Whisper недоступен. Установите faster-whisper и укажите каталог с весами.")
    elif plan.provider == "openai" and not settings.openai_api_key.strip():
        raise STTError(503, "Распознавание OpenAI недоступно. Настройте OPENAI_API_KEY.")
    elif plan.provider == "gemini" and not settings.gemini_api_key.strip():
        raise STTError(503, "Распознавание Gemini недоступно. Настройте GEMINI_API_KEY.")


def format_transcript(segments: list[AudioSegment]) -> str:
    lines = []
    for segment in segments:
        minutes, seconds = divmod(int(segment.start), 60)
        hours, minutes = divmod(minutes, 60)
        timestamp = f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"
        lines.append(f"[{timestamp}] {segment.speaker}: {' '.join(segment.text.split())}")
    return "\n".join(lines)


async def recognize(audio: BinaryIO, extension: str, plan: STTPlan, settings: Settings) -> AudioTranscriptOut:
    try:
        async with asyncio.timeout(settings.stt_timeout_s):
            if plan.provider == "whisper":
                # Один процесс с весами на API-воркер, чтобы не исчерпать VRAM при загрузках.
                async with WHISPER_SLOT:
                    result = await _run_worker(audio, extension, plan, settings)
            else:
                result = await _run_worker(audio, extension, plan, settings)
    except TimeoutError as exc:
        raise STTError(503, "Истекло время распознавания. Загрузите более короткую запись или повторите запрос.") from exc
    return AudioTranscriptOut(text=format_transcript(result.segments), segments=result.segments, route=plan.route,
                              model=plan.model, duration_s=result.duration_s, warnings=result.warnings)


async def _run_worker(audio: BinaryIO, extension: str, plan: STTPlan, settings: Settings) -> Recognition:
    # Наследуем окружение для CUDA, но закрепляем выбранный маршрут и его настройки.
    env = {**os.environ, "STT_PROVIDER": plan.provider, "STT_TIMEOUT_S": str(settings.stt_timeout_s)}
    if plan.provider == "whisper":
        env.update(STT_WHISPER_MODEL_DIR=str(settings.stt_whisper_model_dir.resolve()),
                   STT_WHISPER_DEVICE=settings.stt_whisper_device,
                   STT_WHISPER_COMPUTE_TYPE=settings.stt_whisper_compute_type, HF_HUB_OFFLINE="1")
    elif plan.provider == "openai":
        env.update(STT_OPENAI_MODEL=plan.model, OPENAI_API_KEY=settings.openai_api_key)
    else:
        env.update(STT_GEMINI_MODEL=plan.model, GEMINI_API_KEY=settings.gemini_api_key)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.stt.worker", extension, env=env, stdin=audio,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, NotImplementedError) as exc:
        raise STTError(503, "Не удалось запустить процесс распознавания.") from exc
    try:
        stdout, _ = await proc.communicate()
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
    if proc.returncode == 2:
        raise STTError(415, "Не удалось прочитать аудио. Используйте MP3, M4A, WAV, WebM или OGG.")
    if proc.returncode == 3:
        raise STTError(422, "Речь в записи не обнаружена.")
    if proc.returncode == 5:
        raise STTError(503, "Сервис распознавания отклонил ключ API. Проверьте настройки провайдера.")
    if proc.returncode == 0:
        try:
            return Recognition.model_validate_json(stdout)
        except ValidationError:
            pass
    raise STTError(503, "Сервис распознавания недоступен. Проверьте настройки или повторите позже.")
