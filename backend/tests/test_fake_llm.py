"""Dev-заглушка LLM: проходит через те же серверные проверки, что и настоящая модель."""

import pytest

from conftest import login

from app.config import get_settings
from app.llm.gateway import LLMGateway

TEXT = (
    "[00:31] Мария Петрова: Предлагаю сразу вернуться к старому поставщику.\n"
    "[00:40] Илья Морозов: Пока не решаем. Сначала испытания. Дмитрий, проведи испытания обеих бумаг до пятницы.\n"
    "[01:02] Илья Морозов: Решили: на время испытаний возвращаем входной контроль каждой партии бумаги."
)


@pytest.fixture
def fake(monkeypatch):
    settings = get_settings().model_copy(update={"llm_provider": "fake"})
    gateway = LLMGateway(settings)
    monkeypatch.setattr("app.routers.workflow.get_gateway", lambda: gateway)
    monkeypatch.setattr("app.routers.assistant.get_gateway", lambda: gateway)
    return gateway


def test_fake_protocol_goes_through_server_checks(client, fake):
    h = login(client, "morozov")
    r = client.post("/meetings/m-quality-1104/transcript", json={"text": TEXT}, headers=h)
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["model"] == "fake-dev (без ИИ)"
    kinds = {i["segment_id"]: i for i in draft["items"]}
    assert kinds[1]["kind"] == "proposal" and kinds[1]["decision_status"] == "proposed"
    assert kinds[2]["kind"] == "task" and kinds[2]["owner_employee_id"] == "e-volkov" and kinds[2]["due_at"]
    assert kinds[3]["kind"] == "decision"
    assert all(i["quote_verified"] for i in draft["items"])


def test_fake_answer_cites_allowed_fragment(client, fake):
    h = login(client, "morozov")
    r = client.post("/assistant/ask", json={"question": "Какая доля брака фотокниг в октябре?"}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "answered" and out["claims"][0]["sources"][0]["source_id"]
