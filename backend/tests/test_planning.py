"""С1/Д1 подготовка встречи, Д2 делегирование, С6 «Мой день»."""

import copy
import json
from collections import Counter
from datetime import datetime

import pytest
from sqlalchemy import select

from conftest import login

from app.config import get_settings
from app.db import SessionLocal
from app.llm.gateway import LLMResult
from app.models import ActionProposal, Task

NOW = datetime.fromisoformat("2026-11-11T12:00:00+03:00")


class FakeGateway:
    route, model, provider = "local", "test-model", "ollama"

    def __init__(self, data):
        self.data, self.calls = data, []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        data = self.data(messages) if callable(self.data) else self.data
        return LLMResult(text="", data=copy.deepcopy(data), provider=self.provider, model=self.model, route=self.route, latency_ms=1)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(get_settings().__class__, "now", lambda self: NOW)


def _fragments(messages):
    return json.loads(messages[-1]["content"])["source_fragments"]


def _new_task(task_id, title, **kw):
    with SessionLocal() as db:
        db.merge(Task(id=task_id, title=title, status="assigned", department_id="production", created_by="u-morozov",
                      owner_employee_id=None, owner_origin="unknown", due_origin="unknown", **kw))
        db.commit()


def conf(card):
    return {"revision": card["revision"], "payload_hash": card["payload_hash"]}


# ---------------------------------------------------------------- подготовка встречи


def test_prepare_returns_unclosed_tasks_and_only_cited_agenda(client, monkeypatch):
    def respond(messages):
        f = next(x for x in _fragments(messages) if not x["suspected_injection"])
        quote = f["text"].split(": ", 1)[-1].split(".")[0]
        return {"agenda": [{"title": "Сертификаты и карантин", "minutes": 10, "citations": [{"chunk_id": f["chunk_id"], "quote": quote}]},
                           {"title": "Выдуманный пункт", "minutes": 5, "citations": [{"chunk_id": "nope#0", "quote": "нет такого"}]}],
                "questions": [{"text": "Выдавать ли Б-1102?", "citations": [{"chunk_id": f["chunk_id"], "quote": quote}]}]}

    gw = FakeGateway(respond)
    monkeypatch.setattr("app.pipelines.prep.get_gateway", lambda: gw)
    r = client.post("/meetings/prepare", headers=login(client, "morozov"), json={
        "topic": "Рост брака фотокниг после смены бумаги", "participant_ids": ["e-morozov", "e-volkov", "e-petrova"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["meeting_id"] == "m-quality-1104"
    returned = {a["returned_task_id"] for a in out["agenda"] if a["returned_task_id"]}
    assert {"t-trials-1104", "t-certificates-1104"} <= returned
    titles = [a["title"] for a in out["agenda"]]
    assert "Сертификаты и карантин" in titles and "Выдуманный пункт" not in titles
    assert titles[-1] == "Решения, ответственные и сроки"
    cited = next(a for a in out["agenda"] if a["title"] == "Сертификаты и карантин")
    assert cited["sources"] and cited["sources"][0]["quote"]
    assert out["questions"] and out["questions"][0]["sources"]
    assert any(t["id"] == "t-trials-1104" and t["overdue"] for t in out["open_tasks"])


def test_prepare_without_sources_does_not_call_model(client, monkeypatch):
    gw = FakeGateway({"agenda": [], "questions": []})
    monkeypatch.setattr("app.pipelines.prep.get_gateway", lambda: gw)
    out = client.post("/meetings/prepare", headers=login(client, "morozov"),
                      json={"topic": "Квазары Андромеды", "participant_ids": ["e-morozov"]}).json()
    assert gw.calls == [] and [a["title"] for a in out["agenda"]] == ["Решения, ответственные и сроки"]


def test_prepare_validates_participants_and_hides_private_chats_from_director(client, monkeypatch):
    gw = FakeGateway({"agenda": [], "questions": []})
    monkeypatch.setattr("app.pipelines.prep.get_gateway", lambda: gw)
    assert client.post("/meetings/prepare", headers=login(client, "morozov"),
                       json={"topic": "x", "participant_ids": ["e-nobody"]}).status_code == 422
    r_dir = client.post("/meetings/prepare", headers=login(client, "director"),
                        json={"topic": "Сертификаты БелЛист Б-1102", "participant_ids": ["e-smirnova", "e-petrova"]})
    assert r_dir.status_code == 200
    dir_titles = " ".join(a["title"] for a in r_dir.json()["agenda"])
    assert "Обещание в переписке" not in dir_titles and "Противоречие с реестром" not in dir_titles
    prompt = " ".join(str(m) for call in gw.calls for m in call)
    assert gw.calls and "tg-production" not in prompt and "mail-morozov" not in prompt


def test_prepare_includes_due_decisions_before_after_and_watcher_items(client, monkeypatch):
    from app.models import Decision

    gw = FakeGateway({"agenda": [], "questions": []})
    monkeypatch.setattr("app.pipelines.prep.get_gateway", lambda: gw)
    h = login(client, "morozov")

    with SessionLocal() as db:
        d = db.get(Decision, "dec-incoming-control-1104")
        orig_status, orig_recheck, orig_history = d.status, copy.deepcopy(d.recheck), copy.deepcopy(d.history)
        d.status = "in_progress"
        d.recheck = None
        db.commit()

    try:
        # 1) До повторной проверки: решение участника e-kozlova с наступившим сроком (check_due) + обещания и противоречия сторожа
        r1 = client.post(
            "/meetings/prepare",
            headers=h,
            json={
                "topic": "Разбор качества и дисциплины поставок",
                "participant_ids": ["e-morozov", "e-kozlova", "e-petrova", "e-volkov"],
            },
        )
        assert r1.status_code == 200, r1.text
        agenda1 = r1.json()["agenda"]

        dec_point1 = next((a for a in agenda1 if "Проверка решения: Брак фотокниг" in a["title"]), None)
        assert dec_point1 is not None
        assert "пора проверить результат" in dec_point1["title"]
        assert "4,1%" in dec_point1["title"]
        assert dec_point1["sources"]

        commit_point = next((a for a in agenda1 if "Обещание в переписке (Мария Петрова" in a["title"]), None)
        assert commit_point is not None
        assert commit_point["sources"] and "пришлю до пятницы" in commit_point["sources"][0]["quote"]

        contra_point = next(
            (a for a in agenda1 if "Противоречие с реестром:" in a["title"] and "сертификат" in a["title"].lower()),
            None,
        )
        assert contra_point is not None
        assert contra_point["sources"] and any("направим до 13.11" in s["quote"] for s in contra_point["sources"])

        # 2) После повторной проверки решения — в повестке выводятся цифры «до/после»
        rechecked = client.post("/decisions/dec-incoming-control-1104/recheck", headers=h)
        assert rechecked.status_code == 200
        r2 = client.post(
            "/meetings/prepare",
            headers=h,
            json={
                "topic": "Разбор качества и дисциплины поставок",
                "participant_ids": ["e-morozov", "e-kozlova"],
            },
        )
        assert r2.status_code == 200, r2.text
        dec_point2 = next((a for a in r2.json()["agenda"] if "Проверка решения: Брак фотокниг" in a["title"]), None)
        assert dec_point2 is not None
        assert "до: 4,1% → после: 3,4%" in dec_point2["title"]
    finally:
        with SessionLocal() as db:
            d = db.get(Decision, "dec-incoming-control-1104")
            d.status, d.recheck, d.history = orig_status, orig_recheck, orig_history
            db.commit()


def test_prepare_llm_unavailable_or_timeout_returns_503_and_preserves_input(client, monkeypatch):
    import httpx
    from app.llm import LLMError

    h = login(client, "morozov")
    payload = {"topic": "Сертификаты БелЛист Б-1102", "participant_ids": ["e-morozov", "e-petrova"]}

    class FailingGateway:
        route, model, provider = "local", "test-model", "ollama"

        def __init__(self, exc):
            self.exc = exc

        async def chat(self, messages, **kwargs):
            raise self.exc

    # 1) LLM недоступна
    monkeypatch.setattr("app.pipelines.prep.get_gateway", lambda: FailingGateway(LLMError("Внешняя модель недоступна")))
    r_err = client.post("/meetings/prepare", headers=h, json=payload)
    assert r_err.status_code == 503
    assert r_err.json()["code"] == "llm_unavailable"
    assert "недоступна" in r_err.json()["detail"]

    # 2) Таймаут LLM
    monkeypatch.setattr("app.pipelines.prep.get_gateway", lambda: FailingGateway(httpx.ReadTimeout("timeout")))
    r_to = client.post("/meetings/prepare", headers=h, json=payload)
    assert r_to.status_code == 503
    assert r_to.json()["code"] == "llm_unavailable"
    assert "не ответила вовремя" in r_to.json()["detail"] and "сохранены" in r_to.json()["detail"]

    audit_rows = client.get("/audit", headers=login(client, "director")).json()
    prep_err = next((a for a in audit_rows if a["action"] == "meeting.prepare" and a["result"] == "error"), None)
    assert prep_err is not None
    assert prep_err["details"]["topic"] == payload["topic"]
    assert prep_err["details"]["participant_ids"] == payload["participant_ids"]


# ---------------------------------------------------------------- делегирование


def test_delegation_suggests_who_committed_in_chat_and_waits_for_confirmation(client):
    _new_task("t-deleg-1", "Запросить сертификаты на партию Б-1102")
    h = login(client, "morozov")
    r = client.post("/delegation/propose", json={"task_id": "t-deleg-1"}, headers=h)
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["type"] == "delegate_task" and card["origin"] == "ai" and card["status"] == "pending"
    assert card["payload"]["owner_employee_id"] == "e-petrova" and "Telegram" in card["payload"]["reason"]
    assert card["preview"]["evidence"]["author"] == "Мария Петрова"
    with SessionLocal() as db:
        assert db.get(Task, "t-deleg-1").owner_employee_id is None  # до подтверждения ничего не меняется
    done = client.post(f"/actions/{card['id']}/confirm", json=conf(card), headers=h)
    assert done.status_code == 200 and done.json()["status"] == "executed"
    with SessionLocal() as db:
        t = db.get(Task, "t-deleg-1")
        assert (t.owner_employee_id, t.owner_origin) == ("e-petrova", "suggested") and t.delegation_reason
    assert client.post("/delegation/propose", json={"task_id": "t-deleg-1"}, headers=h).status_code == 409


def test_delegation_detects_stale_card_and_forbids_other_department(client):
    _new_task("t-deleg-2", "Сверить остаток бумаги Б-1102 на складе")
    h = login(client, "morozov")
    card = client.post("/delegation/propose", json={"task_id": "t-deleg-2"}, headers=h).json()
    with SessionLocal() as db:
        db.get(Task, "t-deleg-2").expected_result = "изменено вручную"
        db.commit()
    assert client.post(f"/actions/{card['id']}/confirm", json=conf(card), headers=h).status_code == 409
    with SessionLocal() as db:
        from app.actions import payload_hash, task_snapshot
        snap = payload_hash(task_snapshot(db.get(Task, "t-deleg-2")))
    r = client.post("/actions", headers=h, json={"type": "delegate_task", "payload": {
        "task_id": "t-deleg-2", "expected_hash": snap, "owner_employee_id": "e-belov", "reason": "любой"}})
    assert r.status_code == 422  # руководитель производства не поручает сотруднику поддержки


# ---------------------------------------------------------------- «Мой день»


def test_my_day_for_production_head(client):
    _new_task("t-myday-1", "Подтвердить сертификат партии Б-1102")
    out = client.get("/my-day", headers=login(client, "morozov")).json()
    metrics = {m["id"]: m for m in out["metrics"]}
    assert set(metrics) == {"defect_rate", "orders_on_time", "overdue_tasks"}  # SLA поддержки — не его подразделение
    d = metrics["defect_rate"]
    assert d["value"] == round(13 / 380 * 100, 1) and d["status"] == "critical" and d["formula"]
    assert metrics["orders_on_time"]["status"] == "no_data"
    assert any(t["id"] == "t-trials-1104" for t in out["overdue"])
    kinds = {n["kind"] for n in out["needs_decision"]}
    assert {"overdue_task", "critical_metric"} <= kinds
    assert any(a["payload"]["task_id"] == "t-myday-1" for a in out["delegation_suggestions"])
    assert out["now"].startswith("2026-11-11T12:00")


def test_my_day_for_director_and_idempotent_suggestions(client):
    h = login(client, "director")
    first = client.get("/my-day", headers=h).json()
    assert "support_sla" in {m["id"] for m in first["metrics"]}
    assert any(n["kind"] == "stale_source" for n in first["needs_decision"])
    client.get("/my-day", headers=h)
    with SessionLocal() as db:
        pending = db.scalars(select(ActionProposal).where(ActionProposal.type == "delegate_task", ActionProposal.status == "pending",
                                                          ActionProposal.created_by == "u-director")).all()
    per_task = Counter(a.payload["task_id"] for a in pending)
    assert per_task and all(n == 1 for n in per_task.values())


def test_commitment_watch_finds_promise_with_deadline_and_creates_action_card(client):
    h = login(client, "morozov")
    out = client.get("/my-day", headers=h).json()
    commitments = [n for n in out["needs_decision"] if n["kind"] == "commitment"]
    assert commitments, "Должны находиться обещания со сроком в доступной переписке"

    # 1) Поручение без владельца t-certificates-1104 -> карточка delegate_task со сроком из сообщения («пришлю до пятницы»)
    cert_need = next(n for n in commitments if "пришлю до пятницы" in n["detail"])
    assert cert_need["object_type"] == "action"
    card = client.get(f"/actions/{cert_need['object_id']}", headers=h).json()
    assert card["type"] == "delegate_task" and card["origin"] == "ai" and card["status"] == "pending"
    assert card["payload"]["task_id"] == "t-certificates-1104"
    assert card["payload"]["owner_employee_id"] == "e-petrova"
    assert card["payload"]["due_at"].startswith("2026-11-06T18:00:00")
    assert "пришлю до пятницы" in card["preview"]["evidence"]["quote"]

    # 2) Поручение с владельцем t-trials-1104 -> карточка update_task на перенос срока («на понедельник» -> 09.11)
    trials_need = next(n for n in commitments if "на понедельник" in n["detail"])
    assert trials_need["object_type"] == "action"
    upd_card = client.get(f"/actions/{trials_need['object_id']}", headers=h).json()
    assert upd_card["type"] == "update_task" and upd_card["origin"] == "ai" and upd_card["status"] == "pending"
    assert upd_card["payload"]["task_id"] == "t-trials-1104"
    assert upd_card["payload"]["changes"]["due_at"].startswith("2026-11-09T18:00:00")

    # Подтверждение карточки delegate_task назначает владельца и срок в реестре
    done = client.post(f"/actions/{card['id']}/confirm", json=conf(card), headers=h)
    assert done.status_code == 200 and done.json()["status"] == "executed"
    with SessionLocal() as db:
        t = db.get(Task, "t-certificates-1104")
        assert t.owner_employee_id == "e-petrova"
        assert t.due_at.strftime("%Y-%m-%d") == "2026-11-06"
        t.owner_employee_id = None
        t.owner_origin = "unknown"
        t.due_at = None
        t.due_origin = "unknown"
        db.commit()


def test_commitment_watch_detects_petrova_certificates_contradiction_with_two_sources(client):
    from datetime import date
    from app.models import Document, DocumentChunk

    h = login(client, "morozov")
    out = client.get("/my-day", headers=h).json()
    contradictions = [n for n in out["needs_decision"] if n["kind"] == "contradiction"]
    cert_contra = next((n for n in contradictions if n["object_id"] == "t-certificates-1104"), None)
    assert cert_contra is not None
    assert cert_contra["object_type"] == "task"
    assert "Первоисточник 1" in cert_contra["detail"] and "Первоисточник 2" in cert_contra["detail"]
    assert "пришлю до пятницы" in cert_contra["detail"]
    assert "направим до 13.11" in cert_contra["detail"]

    # Даже если в реестре уже стоит Мария Петрова и срок 06.11.2026, письмо о переносе на 13.11.2026 даёт противоречие
    body_text = (
        "После уточнения у поставщика вчера в производственном чате написала, что пришлю сертификаты "
        "до пятницы, 06.11.2026, но по партии Б-1102 лаборатория «БелЛиста» задерживает протокол. "
        "Новую дату ставлю до 13.11.2026."
    )
    with SessionLocal() as db:
        t = db.get(Task, "t-certificates-1104")
        t.owner_employee_id = "e-petrova"
        t.due_at = datetime.fromisoformat("2026-11-06T18:00:00+03:00")
        doc = Document(
            id="src:mail-petrova:01-certificates-owner.eml",
            title="Перенос срока по сертификатам Б-1012 и Б-1102",
            version=1,
            doc_date=date(2026, 11, 10),
            author="Мария Петрова",
            access_level="participants",
            department_id="production",
            ai_allowed=True,
            content=body_text,
            source_path="sources/email/petrova",
            tags=["email"],
            kind="email",
            channel_id="mail-morozov",
            external_id="01-certificates-owner.eml",
            allowed_employee_ids=["e-petrova", "e-morozov"],
        )
        chunk = DocumentChunk(
            id="src:mail-petrova:01-certificates-owner.eml#0",
            document_id=doc.id,
            ordinal=0,
            text=body_text,
            char_start=0,
            char_end=len(body_text),
            author_employee_id="e-petrova",
            author_raw="Мария Петрова <m.petrova@fotoprint.test>",
            sent_at=datetime.fromisoformat("2026-11-10T09:40:00+03:00"),
            injection_flag=False,
        )
        db.add_all([doc, chunk])
        db.commit()

    out2 = client.get("/my-day", headers=h).json()
    cert_contra2 = next(
        (n for n in out2["needs_decision"] if n["kind"] == "contradiction" and n["object_id"] == "t-certificates-1104"),
        None,
    )
    assert cert_contra2 is not None
    assert "Первоисточник 1" in cert_contra2["detail"] and "06.11.2026" in cert_contra2["detail"]
    assert "Первоисточник 2" in cert_contra2["detail"] and "13.11.2026" in cert_contra2["detail"]
    with SessionLocal() as db:
        t = db.get(Task, "t-certificates-1104")
        t.owner_employee_id = None
        t.due_at = None
        db.commit()


def test_commitment_watch_respects_readable_document_filter_for_private_chats(client):
    from datetime import date
    from app.models import Document, DocumentChunk

    # Директор не входит в tg-production и mail-morozov (access_level=participants) -> чужие чаты не видны
    dir_day = client.get("/my-day", headers=login(client, "director")).json()
    dir_watch = [n for n in dir_day["needs_decision"] if n["kind"] in {"commitment", "contradiction"}]
    assert dir_watch == []

    # Закрытая переписка между Петровой и Волковым без Морозова не должна попадать в «Мой день» Морозова
    secret_line = "[11:00] Мария Петрова: Подготовлю отдельный отчёт по фольге и пришлю до пятницы, 13.11.2026."
    with SessionLocal() as db:
        doc = Document(
            id="src:private-chat:secret",
            title="Личный чат Петровой и Волкова",
            version=1,
            doc_date=date(2026, 11, 10),
            author="Личный чат",
            access_level="participants",
            department_id="production",
            ai_allowed=True,
            content=secret_line,
            source_path="sources/chat/secret.json",
            tags=["chat"],
            kind="chat",
            allowed_employee_ids=["e-petrova", "e-volkov"],
        )
        chunk = DocumentChunk(
            id="src:private-chat:secret#0",
            document_id=doc.id,
            ordinal=0,
            text=secret_line,
            char_start=0,
            char_end=len(secret_line),
            author_employee_id="e-petrova",
            author_raw="Мария Петрова",
            sent_at=datetime.fromisoformat("2026-11-10T11:00:00+03:00"),
            injection_flag=False,
        )
        db.add_all([doc, chunk])
        db.commit()

    morozov_day = client.get("/my-day", headers=login(client, "morozov")).json()
    details = " ".join(n["detail"] for n in morozov_day["needs_decision"])
    assert "отдельный отчёт по фольге" not in details and "Личный чат Петровой и Волкова" not in details


# ---------------------------------------------------------------- напоминания, брифинг, здоровье почты


class FakeSMTP:
    def __init__(self, fail_after=None):
        self.messages, self.fail_after = [], fail_after

    def send(self, msg):
        if self.fail_after is not None and len(self.messages) >= self.fail_after:
            raise OSError("SMTP недоступен")
        self.messages.append(msg)
        return f"<m{len(self.messages)}@test>"


def test_reminders_are_one_card_one_letter_per_owner_and_resend_only_missing(client, monkeypatch):
    smtp = FakeSMTP(fail_after=0)
    monkeypatch.setattr("app.mailer.send_message", smtp.send)
    h = login(client, "morozov")
    card = client.post("/reminders/propose", headers=h).json()
    assert card["type"] == "send_reminders" and card["origin"] == "ai" and card["status"] == "pending"
    owners = [l["recipient_id"] for l in card["payload"]["letters"]]
    assert "e-volkov" in owners and len(owners) == len(set(owners))
    assert any("t-trials-1104" in l["task_ids"] for l in card["payload"]["letters"])
    assert smtp.messages == []  # до подтверждения ничего не уходит
    failed = client.post(f"/actions/{card['id']}/confirm", json=conf(card), headers=h).json()
    assert failed["status"] == "failed" and failed["result"]["sent_count"] == 0
    smtp.fail_after = None
    done = client.post(f"/actions/{card['id']}/retry", headers=h).json()
    assert done["status"] == "executed" and done["result"]["sent_count"] == len(owners) == len(smtp.messages)
    again = client.post(f"/actions/{card['id']}/confirm", json=conf(card), headers=h).json()
    assert again["status"] == "executed" and len(smtp.messages) == len(owners)


def test_brief_goes_only_to_self_after_confirmation(client, monkeypatch):
    smtp = FakeSMTP()
    monkeypatch.setattr("app.mailer.send_message", smtp.send)
    h = login(client, "morozov")
    card = client.post("/my-day/brief", headers=h).json()
    assert card["type"] == "send_brief" and card["payload"]["recipient_id"] == "e-morozov"
    assert "Показатели:" in card["payload"]["body"] and "Брак фотокниг" in card["payload"]["body"]
    hijack = {**card["payload"], "recipient_id": "e-volkov"}
    assert client.patch(f"/actions/{card['id']}", json={"revision": card["revision"], "payload": hijack}, headers=h).status_code == 422
    assert smtp.messages == []
    done = client.post(f"/actions/{card['id']}/confirm", json=conf(card), headers=h).json()
    assert done["status"] == "executed" and len(smtp.messages) == 1
    assert smtp.messages[0]["To"] == "i.morozov@fotoprint.test"


def test_health_reports_smtp(client, monkeypatch):
    async def down():
        return False

    monkeypatch.setattr("app.routers.system._smtp_ok", down)
    h = client.get("/health").json()
    assert h["smtp"] is False and h["status"] == "degraded"
