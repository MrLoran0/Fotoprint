import logging
import secrets
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("fokus")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fokus:fokus@localhost:5432/fokus"

    jwt_secret: str = ""
    jwt_ttl_minutes: int = 12 * 60
    demo_password: str = "fokus-demo"
    # список тестовых учёток (без паролей) для быстрого входа на стенде
    demo_accounts_public: bool = True

    # gemini — внешний API (демо по умолчанию: на наших ноутбуках умная локальная модель не помещается);
    # local — любой OpenAI-совместимый сервер в контуре заказчика (vLLM, LM Studio, TGI…) — путь для пилота;
    # ollama — локальная модель через Ollama; fake — детерминированная dev-заглушка без ИИ (на стенде запрещена)
    llm_provider: Literal["gemini", "local", "ollama", "fake"] = "gemini"
    llm_model: str = "qwen3:8b"
    embed_model: str = "bge-m3"
    ollama_base_url: str = "http://ollama:11434"
    ollama_num_ctx: int = 8192
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    # OpenAI-совместимый сервер внутри контура (например vLLM на GPU-сервере заказчика)
    local_base_url: str = "http://localhost:8001/v1"
    local_api_key: str = ""
    local_model: str = ""
    llm_timeout_s: float = 180.0

    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    mail_from: str = "AI-ассистент <ai-assistant@fotoprint.test>"

    company_tz: str = "Europe/Minsk"
    # фиксированное «время сценария» для повторяемого демо, ISO 8601 со смещением
    scenario_now: str = ""

    data_dir: Path = Path("/data")
    seed_on_start: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.company_tz)

    def now(self) -> datetime:
        """Текущее время сценария (с часовым поясом)."""
        if self.scenario_now:
            return datetime.fromisoformat(self.scenario_now)
        return datetime.now(self.tz)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.jwt_secret:
        s.jwt_secret = secrets.token_urlsafe(48)
        log.warning("JWT_SECRET не задан — сгенерирован временный; токены сбросятся после перезапуска")
    return s
