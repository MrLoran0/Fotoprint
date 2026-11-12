"""Провайдеры внешних моделей (GPT, Claude): формат запроса, разбор ответа, честный маршрут, понятные ошибки. Без сети."""

import asyncio
import json

import httpx
import pytest

from app.config import get_settings
from app.llm.gateway import LLMError, LLMGateway

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
MESSAGES = [{"role": "system", "content": "Отвечай JSON."}, {"role": "user", "content": "Сколько брака?"}]


def _gateway(monkeypatch, provider: str, handler, **overrides) -> tuple[LLMGateway, list[httpx.Request]]:
    s = get_settings().model_copy(update={"llm_provider": provider, **overrides})
    gw = LLMGateway(s)
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(gw, "_client", lambda timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    return gw, seen


def test_openai_request_and_parse(monkeypatch):
    gw, seen = _gateway(monkeypatch, "openai", lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": '{"answer": "4,1%"}'}}], "usage": {"prompt_tokens": 10}}),
        openai_api_key="sk-test", openai_model="gpt-test")
    out = asyncio.run(gw.chat(MESSAGES, schema=SCHEMA, max_tokens=300))
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "gpt-test" and body["max_completion_tokens"] == 300
    assert "max_tokens" not in body and "temperature" not in body  # новые модели OpenAI их не принимают
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert out.data == {"answer": "4,1%"} and out.route == "external" and out.model == "gpt-test"


def test_anthropic_request_and_tool_parse(monkeypatch):
    gw, seen = _gateway(monkeypatch, "anthropic", lambda r: httpx.Response(200, json={
        "content": [{"type": "tool_use", "name": "result", "input": {"answer": "4,1%"}}],
        "usage": {"input_tokens": 12, "output_tokens": 5}}),
        anthropic_api_key="ak-test", anthropic_model="claude-test")
    out = asyncio.run(gw.chat(MESSAGES, schema=SCHEMA, max_tokens=300))
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.anthropic.com/v1/messages"
    assert seen[0].headers["x-api-key"] == "ak-test" and seen[0].headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "Отвечай JSON." and [m["role"] for m in body["messages"]] == ["user"]
    assert body["tool_choice"] == {"type": "tool", "name": "result"} and body["tools"][0]["input_schema"] == SCHEMA
    assert out.data == {"answer": "4,1%"} and out.route == "external" and out.usage["completion_tokens"] == 5


def test_anthropic_text_without_schema(monkeypatch):
    gw, _ = _gateway(monkeypatch, "anthropic", lambda r: httpx.Response(200, json={
        "content": [{"type": "text", "text": "Брак 4,1%."}]}), anthropic_api_key="ak-test")
    assert asyncio.run(gw.chat(MESSAGES)).text == "Брак 4,1%."


@pytest.mark.parametrize("provider,key", [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")])
def test_missing_key_is_clear_error_without_request(monkeypatch, provider, key):
    gw, seen = _gateway(monkeypatch, provider, lambda r: httpx.Response(500),
                        openai_api_key="", anthropic_api_key="")
    with pytest.raises(LLMError, match=key):
        asyncio.run(gw.chat(MESSAGES, schema=SCHEMA))
    assert seen == []  # без ключа — никуда не ходим и не подменяем модель другой


@pytest.mark.parametrize("code,text", [(401, "Ключ модели"), (404, "недоступна этому ключу"), (429, "лимит")])
def test_http_errors_are_explained(monkeypatch, code, text):
    gw, _ = _gateway(monkeypatch, "anthropic", lambda r: httpx.Response(code, json={"error": {}}), anthropic_api_key="ak")
    with pytest.raises(LLMError, match=text):
        asyncio.run(gw.chat(MESSAGES, schema=SCHEMA))


def test_status_checks_the_configured_model(monkeypatch):
    gw, seen = _gateway(monkeypatch, "openai", lambda r: httpx.Response(200, json={"id": "gpt-test"}),
                        openai_api_key="sk-test", openai_model="gpt-test")
    assert asyncio.run(gw.status())["reachable"] is True
    assert str(seen[0].url).endswith("/models/gpt-test")
