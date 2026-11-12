"""Контур решений из трека 1: разбор отклонения → решение (подтверждение) → повторная проверка «до/после»."""

import copy
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from conftest import login

from app.config import get_settings
from app.db import SessionLocal
from app.llm.gateway import LLMResult
from app.models import Decision, Task

NOW = datetime.fromisoformat("2026-11-11T12:00:00+03:00")


class FakeGateway:
    route, model, provider = "local", "test-model", "ollama"

    def __init__(self, data):
        self.data, self.calls = data, []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        data = self.data(messages) if callable(self.data) else self.data
        return LLMResult(text="", data=copy.deepcopy(data), provider=self.provider, model=self.model, route=self.route, latency_ms=1)


def _respond(messages):
    frags = json.loads(messages[-1]["content"])["source_fragments"]
    cites = [{"chunk_id": "nope#1", "quote": "выдуманная цитата"}]
    if frags:
        f = frags[0]
        cites.insert(0, {"chunk_id": f["chunk_id"], "quote": f["text"].split(": ", 1)[-1].split(".")[0]})
    return {"cause_hypothesis": "Вероятно, связано с бумагой нового поставщика; причинность не доказана.",
            "suggested_action": "Провести сравнительные испытания бумаги на одной настройке линии", "citations": cites}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(get_settings().__class__, "now", lambda self: NOW)
    gw = FakeGateway(_respond)
    monkeypatch.setattr("app.pipelines.deviation.get_gateway", lambda: gw)
    return gw


def conf(card):
    return {"revision": card["revision"], "payload_hash": card["payload_hash"]}


def test_analysis_finds_contributor_counts_by_code_and_cites_only_verified(client):
    a = client.get("/metrics/defect_rate/analysis", headers=login(client, "morozov")).json()
    assert a["metric"]["value"] == round(13 / 380 * 100, 1) and a["metric"]["status"] == "critical"
    assert a["trend"] and a["trend"][-1]["denominator"] == 380
    dims = {(b["dimension"], b["period"]) for b in a["breakdowns"]}
    assert ("paper_supplier", "10.2026") in dims
    assert a["top_contributor"] and "Поставщик Б" in a["top_contributor"]
    assert a["hypothesis"]["kind"] == "assumption"
    ids = [s["chunk_id"] for s in a["hypothesis"]["sources"]]
    assert ids and "nope#1" not in ids
    assert a["evidence"] and a["owner_employee_id"] == "e-kozlova"


def test_sla_is_director_only(client):
    assert client.get("/metrics/support_sla/analysis", headers=login(client, "morozov")).status_code == 404
    assert client.get("/metrics/support_sla/analysis", headers=login(client, "director")).status_code == 200


def test_decision_card_is_drafted_by_assistant_and_created_only_after_confirmation(client):
    h = login(client, "morozov")
    card = client.post("/metrics/defect_rate/decision-proposal", headers=h).json()
    assert card["type"] == "create_decision" and card["origin"] == "ai" and card["status"] == "pending"
    p = card["payload"]
    assert p["title"].startswith("Брак фотокниг") and p["owner_employee_id"] == "e-kozlova" and p["priority"] == "high"
    assert p["impact"] and p["action_text"]
    with SessionLocal() as db:
        before = len(db.scalars(select(Decision)).all())
    done = client.post(f"/actions/{card['id']}/confirm", json=conf(card), headers=h).json()
    assert done["status"] == "executed"
    with SessionLocal() as db:
        d = db.get(Decision, done["result"]["decision_id"])
        assert len(db.scalars(select(Decision)).all()) == before + 1
        assert d.baseline["value"] == round(13 / 380 * 100, 1) and d.baseline["denominator"] == 380
        t = db.get(Task, d.task_ids[0])
        assert t.decision_id == d.id and t.owner_employee_id == "e-kozlova"
    listed = {x["id"] for x in client.get("/decisions", headers=h).json()}
    assert d.id in listed


def test_recheck_compares_before_and_after_and_feeds_my_day(client):
    h = login(client, "morozov")
    day = client.get("/my-day", headers=h).json()
    assert any(n["kind"] == "decision_check" and n["object_id"] == "dec-incoming-control-1104" for n in day["needs_decision"])
    r = client.post("/decisions/dec-incoming-control-1104/recheck", headers=h).json()
    assert r["recheck"]["value"] == round(13 / 380 * 100, 1) and r["baseline"]["value"] == 4.1
    assert r["status"] == "partial" and r["recheck"]["delta"] < 0 and r["history"][-1]["event"] == "повторная проверка"
    day = client.get("/my-day", headers=h).json()
    assert any("следующее решение" in n["title"] for n in day["needs_decision"])


def test_decision_rights_and_validation(client):
    d, m = login(client, "director"), login(client, "morozov")
    bad = {"title": "x", "action_text": "y", "owner_employee_id": "e-belov", "due_at": "2026-11-20T18:00:00+03:00"}
    assert client.post("/actions", headers=m, json={"type": "create_decision", "payload": bad}).status_code == 422
    past = {**bad, "owner_employee_id": "e-volkov", "due_at": "2026-11-01T18:00:00+03:00"}
    assert client.post("/actions", headers=m, json={"type": "create_decision", "payload": past}).status_code == 422
    card = client.post("/actions", headers=d, json={"type": "create_decision", "payload": {**bad, "title": "Решение дирекции"}}).json()
    res = client.post(f"/actions/{card['id']}/confirm", json=conf(card), headers=d).json()
    dec_id = res["result"]["decision_id"]
    assert client.get(f"/decisions/{dec_id}", headers=m).status_code == 403  # решение дирекции руководителю не видно
    assert client.get(f"/decisions/{dec_id}", headers=d).status_code == 200


def test_deviation_analysis_llm_unavailable_or_timeout_returns_503_and_preserves_state(client, monkeypatch):
    import httpx
    from app.llm import LLMError

    h = login(client, "morozov")
    decisions_before = client.get("/decisions", headers=h).json()

    class FailingGateway:
        route, model, provider = "local", "test-model", "ollama"

        def __init__(self, exc):
            self.exc = exc

        async def chat(self, messages, **kwargs):
            raise self.exc

    # 1) LLM недоступна при разборе отклонения
    monkeypatch.setattr("app.pipelines.deviation.get_gateway", lambda: FailingGateway(LLMError("Модель недоступна")))
    r_err = client.get("/metrics/defect_rate/analysis", headers=h)
    assert r_err.status_code == 503
    assert r_err.json()["code"] == "llm_unavailable"
    assert "недоступна" in r_err.json()["detail"]

    # 2) Таймаут LLM при разборе отклонения и формировании карточки решения
    monkeypatch.setattr("app.pipelines.deviation.get_gateway", lambda: FailingGateway(httpx.ReadTimeout("timeout")))
    r_to = client.get("/metrics/defect_rate/analysis", headers=h)
    assert r_to.status_code == 503
    assert r_to.json()["code"] == "llm_unavailable"
    assert "не ответила вовремя" in r_to.json()["detail"]

    r_prop = client.post("/metrics/defect_rate/decision-proposal", headers=h)
    assert r_prop.status_code == 503
    assert r_prop.json()["code"] == "llm_unavailable"
    assert "не ответила вовремя" in r_prop.json()["detail"]

    # Состояние реестра решений не повреждается
    assert client.get("/decisions", headers=h).json() == decisions_before
