"""Источники (почта, Telegram, чат), приватность переписки, граф и честные ответы «недостаточно данных»."""

import copy
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from conftest import login

from app.config import get_settings
from app.db import SessionLocal
from app.llm.gateway import LLMGateway, LLMResult
from app.models import DocumentChunk, Mention, SourceChannel

TG_DAY = "tg-production:2026-11-05"


def test_sources_are_synced_and_visible_by_membership(client):
    morozov = {s["id"]: s for s in client.get("/sources", headers=login(client, "morozov")).json()}
    assert {"tg-production", "mail-morozov", "chat-quality"} <= morozov.keys()
    assert all(s["status"] == "ok" and s["items_count"] > 0 for s in morozov.values())
    assert morozov["tg-production"]["last_error"] is None
    director = {s["id"] for s in client.get("/sources", headers=login(client, "director")).json()}
    # директор не участник Telegram-группы и не владелец ящика Морозова — эти каналы ему не видны
    assert "chat-quality" in director and "tg-production" not in director and "mail-morozov" not in director


def test_private_correspondence_is_visible_only_to_participants(client):
    m, d = login(client, "morozov"), login(client, "director")
    assert client.get(f"/documents/{TG_DAY}", headers=m).status_code == 200
    assert client.get(f"/documents/{TG_DAY}", headers=d).status_code == 403
    kinds_m = {doc["id"].split(":")[0] for doc in client.get("/documents", headers=m).json()}
    kinds_d = {doc["id"].split(":")[0] for doc in client.get("/documents", headers=d).json()}
    assert {"tg-production", "mail-morozov", "chat-quality"} <= kinds_m
    assert "tg-production" not in kinds_d and "mail-morozov" not in kinds_d and "chat-quality" in kinds_d


def test_messages_keep_author_time_mentions_and_injection_flag(client):
    with SessionLocal() as db:
        chunks = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == TG_DAY)).all()
        petrova = next(c for c in chunks if c.author_raw == "Мария Петрова")
        local = petrova.sent_at.astimezone(ZoneInfo("Europe/Minsk"))
        assert petrova.author_employee_id == "e-petrova" and local.isoformat().startswith("2026-11-05T10:12")
        targets = {m.target_id for m in db.scalars(select(Mention).where(Mention.chunk_id == petrova.id)).all()}
        assert {"sup-b", "batch-b1012", "batch-b1102", "topic-certificates"} <= targets
        flagged = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id.like("mail-morozov:%"),
                                                         DocumentChunk.injection_flag.is_(True))).all()
        assert flagged, "письмо с инъекцией должно быть помечено"


def test_mentions_decline_surnames_but_do_not_confuse_names_with_suppliers(client):
    from app.graph.mentions import build_matchers, find_mentions

    with SessionLocal() as db:
        matchers = build_matchers(db)
        hits = {i for _, i, _ in find_mentions("Сертификаты у «БелЛиста» запросила Петровой поручено, с Беловым и Дмитрием согласовано", matchers)}
    # «БелЛист» — поставщик, а не Олег Белов; фамилии и имена ловятся в падежах
    assert {"sup-b", "e-petrova", "e-belov", "e-volkov"} <= hits
    with SessionLocal() as db:
        only_supplier = {i for _, i, _ in find_mentions("Поставщик Б «БелЛист»", build_matchers(db))}
    assert "e-belov" not in only_supplier and "sup-b" in only_supplier


def test_broken_source_reports_error_and_others_keep_working(client):
    with SessionLocal() as db:
        db.merge(SourceChannel(id="chat-broken", kind="chat", name="Сломанный экспорт", connector="chat_json",
                               path="chat/missing.json", access_level="department", department_id="production", member_ids=[]))
        db.commit()
    r = client.post("/sources/chat-broken/sync", headers=login(client, "director"))
    assert r.status_code == 200 and r.json()["status"] == "error" and r.json()["last_error"]
    assert client.post("/sources/chat-broken/sync", headers=login(client, "morozov")).status_code == 403
    ok = {s["id"]: s["status"] for s in client.get("/sources", headers=login(client, "morozov")).json()}
    assert ok["tg-production"] == "ok"


class FakeGateway:
    route, model, provider = "local", "test-model", "ollama"

    def __init__(self, data):
        self.data, self.calls = data, []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        data = copy.deepcopy(self.data(messages) if callable(self.data) else self.data)
        return LLMResult(text="", data=data, provider=self.provider, model=self.model, route=self.route, latency_ms=1)


def _first_fragment(messages, source_prefix):
    import json

    frags = json.loads(messages[-1]["content"])["source_fragments"]
    return next(f for f in frags if f["chunk_id"].startswith(source_prefix))


def test_answer_cites_message_with_author_and_reports_gaps(client, monkeypatch):
    def respond(messages):
        f = _first_fragment(messages, "tg-production")
        quote = f["text"].split(": ", 1)[1]
        return {"status": "answered", "claims": [{"text": "Петрова взялась запросить сертификаты",
                                                  "citations": [{"chunk_id": f["chunk_id"], "quote": quote}]}],
                "unanswered": ["нет данных о дате поступления сертификата Б-1102 на склад"]}

    gw = FakeGateway(respond)
    monkeypatch.setattr("app.routers.assistant.get_gateway", lambda: gw)
    r = client.post("/assistant/ask", json={"question": "Кто запрашивает сертификаты Б-1102 у БелЛиста?"},
                    headers=login(client, "morozov"))
    out = r.json()
    assert r.status_code == 200 and out["status"] == "partial", out
    src = out["claims"][0]["sources"][0]
    assert src["source_type"] == "telegram" and src["author"] == "Мария Петрова" and src["sent_at"]
    assert out["gaps"] and "Б-1102" in out["gaps"][0]
    assert any(e["employee_id"] == "e-petrova" for e in out["experts"])


def test_director_never_gets_private_chat_into_prompt(client, monkeypatch):
    gw = FakeGateway({"status": "not_found", "claims": []})
    monkeypatch.setattr("app.routers.assistant.get_gateway", lambda: gw)
    client.post("/assistant/ask", json={"question": "Что с сертификатами Б-1102 у БелЛиста?"}, headers=login(client, "director"))
    prompt = " ".join(str(m) for call in gw.calls for m in call)
    assert "tg-production" not in prompt and "mail-morozov" not in prompt


def test_no_sources_means_not_found_without_calling_model(client, monkeypatch):
    gw = FakeGateway({"status": "answered", "claims": []})
    monkeypatch.setattr("app.routers.assistant.get_gateway", lambda: gw)
    out = client.post("/assistant/ask", json={"question": "Квазары Андромеды?"},
                      headers=login(client, "morozov")).json()
    assert out["status"] == "not_found" and out["answer"].startswith("Недостаточно данных") and out["gaps"]
    assert gw.calls == []


def test_graph_is_built_from_evidence_and_filtered_by_rights(client):
    m = client.get("/graph", params={"focus": "entity:sup-b"}, headers=login(client, "morozov")).json()
    types = {n["type"] for n in m["nodes"]}
    assert {"entity", "telegram", "email", "employee"} <= types
    authored = [e for e in m["edges"] if e["type"] == "authored" and e["source"] == "employee:e-petrova"]
    assert authored and authored[0]["evidence"]["quote"]
    assert all(e["evidence"] for e in m["edges"] if e["type"] in ("mentions", "authored"))
    d = client.get("/graph", params={"focus": "entity:sup-b"}, headers=login(client, "director")).json()
    assert not {n["type"] for n in d["nodes"]} & {"telegram", "email"}


def test_task_graph_links_registry_to_conversations(client):
    g = client.get("/graph", params={"focus": "task:t-certificates-1104"}, headers=login(client, "morozov")).json()
    ids = {n["id"] for n in g["nodes"]}
    assert "task:t-certificates-1104" in ids and "entity:topic-certificates" in ids
    assert any(n["type"] in ("telegram", "email") for n in g["nodes"])


def test_graph_rejects_unknown_focus_and_hidden_task(client):
    h = login(client, "morozov")
    assert client.get("/graph", params={"focus": "nope:1"}, headers=h).status_code == 422
    assert client.get("/graph", params={"focus": "task:does-not-exist"}, headers=h).status_code == 403


@pytest.mark.parametrize("provider,route", [("gemini", "external"), ("local", "local"), ("ollama", "local"), ("fake", "local")])
def test_route_is_honest_for_every_provider(provider, route):
    gw = LLMGateway(get_settings().model_copy(update={"llm_provider": provider, "local_model": "qwen-72b"}))
    assert gw.route == route and gw.model
