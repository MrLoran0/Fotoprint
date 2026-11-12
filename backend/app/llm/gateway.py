"""LLM-шлюз: одна точка подключения любой модели.

Провайдеры (LLM_PROVIDER):
- gemini — внешний API; в демо по умолчанию, потому что на ноутбуках команды умная локальная модель не помещается;
- openai — внешний API OpenAI (GPT), OpenAI-совместимый протокол;
- anthropic — внешний API Anthropic (Claude), нативный Messages API; JSON по схеме — через принудительный вызов инструмента;
- local  — любой OpenAI-совместимый сервер ВНУТРИ контура заказчика (vLLM / TGI / LM Studio на их GPU) —
           это путь для пилота: меняется только адрес в .env, код не меняется;
- ollama — локальная модель через Ollama (проверка на ноутбуке с GPU);
- fake   — детерминированная dev-заглушка без ИИ, только для разработки.

Правила:
- маршрут выбирается в .env и возвращается в каждом ответе (`route`: local | external) — скрытого переключения нет;
- структурированный вывод — JSON Schema; результат валидирует вызывающий пайплайн (Pydantic);
- эмбеддинги — через Ollama на CPU (num_gpu=0); если недоступны, поиск работает полнотекстово.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

import httpx

from ..config import Settings, get_settings

log = logging.getLogger("fokus.llm")

Route = Literal["local", "external"]
EXTERNAL = {"gemini", "openai", "anthropic"}  # данные уходят внешнему провайдеру — это видно в каждом ответе


class LLMError(RuntimeError):
    """Модель недоступна или вернула непригодный ответ. Сообщение — для пользователя."""


@dataclass
class LLMResult:
    text: str
    data: Any | None
    provider: str
    model: str
    route: Route
    latency_ms: int
    usage: dict = field(default_factory=dict)


class LLMGateway:
    def __init__(self, settings: Settings):
        self.s = settings

    # ---------------------------------------------------------------- публичное API

    @property
    def provider(self) -> str:
        return self.s.llm_provider

    @property
    def model(self) -> str:
        return {
            "fake": "fake-dev (без ИИ)",
            "ollama": self.s.llm_model,
            "local": self.s.local_model or "local",
            "gemini": self.s.gemini_model,
            "openai": self.s.openai_model,
            "anthropic": self.s.anthropic_model,
        }[self.provider]

    @property
    def route(self) -> Route:
        return "external" if self.provider in EXTERNAL else "local"

    async def chat(
        self,
        messages: list[dict],
        *,
        schema: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> LLMResult:
        started = time.perf_counter()
        try:
            if self.provider == "fake":
                from .fake import fake_chat

                text, usage = fake_chat(messages), {}
            elif self.provider == "ollama":
                text, usage = await self._ollama_chat(messages, schema, temperature, max_tokens)
            elif self.provider == "local":
                text, usage = await self._openai_chat(
                    self.s.local_base_url, self.s.local_api_key, self.s.local_model, messages, schema, temperature, max_tokens
                )
            elif self.provider == "openai":
                if not self.s.openai_api_key:
                    raise LLMError("Модель не настроена: задайте OPENAI_API_KEY в .env")
                # новые модели OpenAI принимают max_completion_tokens и только температуру по умолчанию
                text, usage = await self._openai_chat(
                    self.s.openai_base_url, self.s.openai_api_key, self.s.openai_model, messages, schema, None, max_tokens,
                    tokens_field="max_completion_tokens",
                )
            elif self.provider == "anthropic":
                if not self.s.anthropic_api_key:
                    raise LLMError("Модель не настроена: задайте ANTHROPIC_API_KEY в .env")
                text, usage = await self._anthropic_chat(messages, schema, temperature, max_tokens)
            else:
                if not self.s.gemini_api_key:
                    raise LLMError("Модель не настроена: задайте GEMINI_API_KEY в .env")
                text, usage = await self._openai_chat(
                    self.s.gemini_base_url, self.s.gemini_api_key, self.s.gemini_model, messages, schema, temperature, max_tokens
                )
        except httpx.TimeoutException as e:
            raise LLMError("Модель не ответила вовремя. Попробуйте ещё раз — введённые данные сохранены.") from e
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            log.warning("LLM %s HTTP %s: %s", self.provider, code, e.response.text[:300])
            if code in (401, 403):
                raise LLMError("Ключ модели не принят провайдером — проверьте ключ в .env.") from e
            if code == 404:
                raise LLMError(f"Модель {self.model} недоступна этому ключу — проверьте имя модели в .env.") from e
            if code == 429:
                raise LLMError("Превышен лимит запросов к модели. Повторите через минуту — введённые данные сохранены.") from e
            raise LLMError("Модель сейчас недоступна. Введённые данные сохранены, повторите позже.") from e
        except httpx.HTTPError as e:
            log.warning("LLM request failed: %s", e)
            raise LLMError("Модель сейчас недоступна. Введённые данные сохранены, повторите позже.") from e
        except (KeyError, IndexError, TypeError) as e:
            log.warning("LLM %s: unexpected response shape: %r", self.provider, e)
            raise LLMError("Модель вернула ответ в неожиданном формате. Повторите запрос.") from e

        data = None
        if schema is not None:
            try:
                data = json.loads(_strip_fences(text))
            except json.JSONDecodeError as e:
                raise LLMError("Модель вернула ответ в неверном формате. Повторите запрос.") from e
        return LLMResult(
            text=text,
            data=data,
            provider=self.provider,
            model=self.model,
            route=self.route,
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=usage,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Эмбеддинги bge-m3 через Ollama на CPU. Ошибка → вызывающий код откатывается на полнотекстовый поиск."""
        async with self._client() as client:
            r = await client.post(
                f"{self.s.ollama_base_url}/api/embed",
                json={"model": self.s.embed_model, "input": texts, "options": {"num_gpu": 0}, "keep_alive": "24h"},
            )
            r.raise_for_status()
            return r.json()["embeddings"]

    async def status(self) -> dict:
        if self.provider == "fake":
            return {"reachable": True, "detail": "dev-заглушка без ИИ — только для разработки, не для стенда"}
        try:
            async with self._client(timeout=5) as client:
                if self.provider == "openai":
                    if not self.s.openai_api_key:
                        return {"reachable": False, "detail": "не задан OPENAI_API_KEY"}
                    r = await client.get(f"{self.s.openai_base_url}/models/{self.s.openai_model}", headers=_auth(self.s.openai_api_key))
                    r.raise_for_status()
                    return {"reachable": True, "detail": "ключ принят, модель доступна"}
                if self.provider == "anthropic":
                    if not self.s.anthropic_api_key:
                        return {"reachable": False, "detail": "не задан ANTHROPIC_API_KEY"}
                    r = await client.get(f"{self.s.anthropic_base_url}/v1/models/{self.s.anthropic_model}",
                                         headers=self._anthropic_headers())
                    r.raise_for_status()
                    return {"reachable": True, "detail": "ключ принят, модель доступна"}
                if self.provider == "ollama":
                    r = await client.get(f"{self.s.ollama_base_url}/api/tags")
                    r.raise_for_status()
                    names = {m.get("name") for m in r.json().get("models", [])}
                    ok = self.s.llm_model in names or f"{self.s.llm_model}:latest" in names
                    detail = "модель загружена" if ok else f"модель {self.s.llm_model} не скачана (ollama pull)"
                    return {"reachable": ok, "detail": detail}
                if self.provider == "local":
                    r = await client.get(f"{self.s.local_base_url}/models", headers=_auth(self.s.local_api_key))
                    r.raise_for_status()
                    ids = {m.get("id") for m in r.json().get("data", [])}
                    ok = not self.s.local_model or self.s.local_model in ids
                    return {"reachable": ok, "detail": "сервер в контуре отвечает" if ok else f"на сервере нет модели {self.s.local_model}"}
                if not self.s.gemini_api_key:
                    return {"reachable": False, "detail": "не задан GEMINI_API_KEY"}
                r = await client.get(f"{self.s.gemini_base_url}/models", headers=_auth(self.s.gemini_api_key))
                r.raise_for_status()
                return {"reachable": True, "detail": "ключ принят"}
        except httpx.HTTPError as e:
            return {"reachable": False, "detail": f"нет связи: {type(e).__name__}"}

    # ---------------------------------------------------------------- Ollama

    async def _ollama_chat(self, messages, schema, temperature, max_tokens) -> tuple[str, dict]:
        body: dict[str, Any] = {
            "model": self.s.llm_model,
            "messages": messages,
            "stream": False,
            "think": False,  # Qwen3: режим без рассуждений — быстрее и предсказуемее для извлечения
            "keep_alive": "24h",
            "options": {"temperature": temperature, "num_ctx": self.s.ollama_num_ctx, "num_predict": max_tokens},
        }
        if schema is not None:
            body["format"] = schema
        async with self._client() as client:
            r = await client.post(f"{self.s.ollama_base_url}/api/chat", json=body)
            r.raise_for_status()
            data = r.json()
        usage = {"prompt_tokens": data.get("prompt_eval_count"), "completion_tokens": data.get("eval_count")}
        return data["message"]["content"], usage

    # ---------------------------------------------------------------- OpenAI-совместимые (Gemini, vLLM и т.п.)

    async def _openai_chat(self, base_url, api_key, model, messages, schema, temperature, max_tokens,
                           *, tokens_field: str = "max_tokens") -> tuple[str, dict]:
        body: dict[str, Any] = {"model": model, "messages": messages, tokens_field: max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}
        async with self._client() as client:
            r = await client.post(f"{base_url}/chat/completions", json=body, headers=_auth(api_key))
            r.raise_for_status()
            data = r.json()
        return data["choices"][0]["message"]["content"] or "", data.get("usage", {})

    # ---------------------------------------------------------------- Anthropic (Claude), нативный Messages API

    def _anthropic_headers(self) -> dict:
        return {"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01"}

    async def _anthropic_chat(self, messages, schema, temperature, max_tokens) -> tuple[str, dict]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        body: dict[str, Any] = {
            "model": self.s.anthropic_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"],
        }
        if system:
            body["system"] = system
        if schema is not None:
            # строгий JSON: модель обязана «вызвать» инструмент, входные данные которого — наша схема
            body["tools"] = [{"name": "result", "description": "Верни результат строго по схеме.", "input_schema": schema}]
            body["tool_choice"] = {"type": "tool", "name": "result"}
        async with self._client() as client:
            r = await client.post(f"{self.s.anthropic_base_url}/v1/messages", json=body, headers=self._anthropic_headers())
            r.raise_for_status()
            data = r.json()
        blocks = data.get("content", [])
        if schema is not None:
            tool = next((b for b in blocks if b.get("type") == "tool_use"), None)
            if tool is None:
                raise LLMError("Модель вернула ответ в неверном формате. Повторите запрос.")
            text = json.dumps(tool.get("input", {}), ensure_ascii=False)
        else:
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage", {})
        return text, {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens")}

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        """Единая точка создания HTTP-клиента (в тестах подменяется транспортом без сети)."""
        return httpx.AsyncClient(timeout=timeout or self.s.llm_timeout_s)


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


@lru_cache
def get_gateway() -> LLMGateway:
    return LLMGateway(get_settings())
