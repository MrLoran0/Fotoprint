import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from conftest import login
from app.config import get_settings
from app.db import SessionLocal
from app.llm import LLMError
from app.llm.gateway import LLMResult
from app.models import CalendarEvent, DeliveryJob, Meeting, Task, new_id

TEXT = "Илья Морозов: Дмитрий, проведи испытания бумаги до пятницы.\nМария Петрова: Предлагаю сменить поставщика."
DATA = {"summary": "Испытания бумаги и предложение по поставщику", "items": [
    {"kind": "task", "statement": "Провести испытания бумаги", "decision_status": "decided", "segment_id": 1,
     "evidence_quote": "Дмитрий, проведи испытания бумаги до пятницы.", "owner_raw": "Дмитрий", "due_raw": "до пятницы"},
    {"kind": "proposal", "statement": "Сменить поставщика", "decision_status": "proposed", "segment_id": 2,
     "evidence_quote": "Предлагаю сменить поставщика.", "owner_raw": None, "due_raw": None},
]}


class FakeGateway:
    route, model, provider = "local", "test-model", "ollama"

    def __init__(self, data=None, error=False):
        self.data = data if data is not None else DATA
        self.error = error
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if self.error:
            raise LLMError("Модель недоступна")
        return LLMResult(text="", data=copy.deepcopy(self.data), provider=self.provider,
                         model=self.model, route=self.route, latency_ms=1)


@pytest.fixture
def gateway(monkeypatch):
    gateway = FakeGateway()
    monkeypatch.setattr("app.routers.workflow.get_gateway", lambda: gateway)
    return gateway


def draft_and_card(client, headers):
    response = client.post("/meetings/m-quality-1104/transcript", json={"text": TEXT}, headers=headers)
    assert response.status_code == 200, response.text
    draft = response.json()
    response = client.post(f"/drafts/{draft['id']}/proposals", headers=headers)
    assert response.status_code == 200, response.text
    return draft, response.json()[0]


def confirmation(card):
    return {"revision": card["revision"], "payload_hash": card["payload_hash"]}


def task_count():
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Task))


def test_protocol_confirm_persists_links_once(client, gateway):
    headers = login(client, "morozov")
    before = task_count()
    draft, card = draft_and_card(client, headers)
    assert task_count() == before
    assert draft["items"][0]["owner_employee_id"] == "e-volkov"
    assert draft["items"][0]["due_at"].startswith("2026-11-06T18:00:00")
    assert card["preview"]["task_count"] == 1
    response = client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert task_count() == before + 1
    repeat = client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers)
    assert repeat.json()["result"] == result
    assert task_count() == before + 1
    task = client.get(f"/tasks/{result['task_ids'][0]}", headers=headers).json()
    assert task["meeting_id"] == draft["meeting_id"]
    assert task["protocol_item_id"] == draft["items"][0]["id"]
    assert client.get(f"/drafts/{draft['id']}", headers=headers).json()["status"] == "published"


def test_cancellation_has_no_business_effect(client, gateway):
    headers = login(client, "morozov")
    before = task_count()
    draft, card = draft_and_card(client, headers)
    assert client.post(f"/actions/{card['id']}/cancel", headers=headers).json()["status"] == "cancelled"
    assert task_count() == before
    assert client.get(f"/drafts/{draft['id']}", headers=headers).json()["status"] == "draft"
    assert client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers).status_code == 409


def test_edits_invalidate_old_confirmation_and_preserve_user_origin(client, gateway):
    headers = login(client, "morozov")
    draft, card = draft_and_card(client, headers)
    response = client.patch(f"/drafts/{draft['id']}", headers=headers,
                            json={"revision": 1, "items": [{"id": draft["items"][0]["id"], "due_at": None}]})
    assert response.status_code == 200, response.text
    assert "due_at" in response.json()["items"][0]["needs_clarification"]
    assert client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers).status_code == 409
    assert client.patch(f"/drafts/{draft['id']}", headers=headers, json={"revision": 1, "items": []}).status_code == 409
    card = client.post(f"/drafts/{draft['id']}/proposals", headers=headers).json()[0]
    payload = card["payload"]
    payload["items"][0]["due_at"] = "2026-11-10T18:00:00+03:00"
    edited = client.patch(f"/actions/{card['id']}", headers=headers, json={"revision": 1, "payload": payload})
    assert edited.status_code == 200, edited.text
    assert edited.json()["payload"]["items"][0]["due_origin"] == "user_entered"
    assert client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers).status_code == 409
    response = client.post(f"/actions/{card['id']}/confirm", json=confirmation(edited.json()), headers=headers)
    assert response.status_code == 200, response.text


def test_another_user_cannot_confirm_card(client, gateway):
    director, production = login(client, "director"), login(client, "morozov")
    draft, card = draft_and_card(client, director)
    assert client.get(f"/drafts/{draft['id']}", headers=production).status_code == 403
    assert client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=production).status_code == 403


def test_parallel_confirmations_create_one_task(client, gateway):
    headers = login(client, "morozov")
    before = task_count()
    _, card = draft_and_card(client, headers)
    def confirm_once(_):
        return client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(confirm_once, range(2)))
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert responses[0].json()["result"] == responses[1].json()["result"]
    assert task_count() == before + 1


def test_llm_failure_keeps_uploaded_transcript(client, gateway):
    gateway.error = True
    headers = login(client, "morozov")
    response = client.post("/meetings/m-quality-1104/transcript", json={"text": TEXT}, headers=headers)
    assert response.status_code == 503
    rows = client.get("/meetings/m-quality-1104/transcripts", headers=headers).json()
    assert rows[0]["text"] == TEXT


def test_invalid_model_output_is_a_service_error(client, gateway):
    gateway.data = {"items": "wrong"}
    response = client.post("/meetings/m-quality-1104/transcript", json={"text": TEXT}, headers=login(client, "morozov"))
    assert response.status_code == 503


def test_fabricated_evidence_cannot_be_published(client, gateway):
    gateway.data = copy.deepcopy(DATA)
    gateway.data["items"][0]["evidence_quote"] = "Выдуманная цитата"
    headers = login(client, "morozov")
    draft = client.post("/meetings/m-quality-1104/transcript", json={"text": TEXT}, headers=headers).json()
    assert not draft["items"][0]["quote_verified"]
    assert client.post(f"/drafts/{draft['id']}/proposals", headers=headers).status_code == 422


def test_task_changes_require_confirmation_and_detect_stale_state(client, gateway):
    headers = login(client, "morozov")
    _, card = draft_and_card(client, headers)
    result = client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers).json()["result"]
    task_id = result["task_ids"][0]
    url = f"/tasks/{task_id}/change-proposal"
    first = client.post(url, json={"status": "accepted"}, headers=headers).json()
    stale = client.post(url, json={"title": "Устаревшая правка"}, headers=headers).json()
    assert client.get(f"/tasks/{task_id}", headers=headers).json()["status"] == "assigned"
    assert client.post(f"/actions/{first['id']}/confirm", json=confirmation(first), headers=headers).status_code == 200
    assert client.post(f"/actions/{stale['id']}/confirm", json=confirmation(stale), headers=headers).status_code == 409
    assert client.post(url, json={"status": "done"}, headers=headers).status_code == 422


def test_meeting_acl_hides_protocol_tasks_and_transcripts(client, gateway):
    with SessionLocal() as db:
        meeting = Meeting(id=new_id("private"), title="Закрытая встреча", department_id="management", participant_ids=[])
        db.add(meeting)
        db.commit()
        meeting_id = meeting.id
    director, production = login(client, "director"), login(client, "morozov")
    draft = client.post(f"/meetings/{meeting_id}/transcript", json={"text": TEXT}, headers=director).json()
    card = client.post(f"/drafts/{draft['id']}/proposals", headers=director).json()[0]
    result = client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=director).json()["result"]
    for path in [f"/drafts/{draft['id']}", f"/tasks/{result['task_ids'][0]}", f"/meetings/{meeting_id}/transcripts"]:
        assert client.get(path, headers=production).status_code == 403
    assert all(t["id"] not in result["task_ids"] for t in client.get("/tasks", headers=production).json())


class FakeSMTP:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, msg):
        if self.fail:
            raise OSError("SMTP Mailpit недоступен")
        self.messages.append(msg)
        return str(msg.get("Message-ID") or f"<msg-{len(self.messages)}@fotoprint.test>")


@pytest.fixture
def smtp(monkeypatch):
    fake_smtp = FakeSMTP()
    monkeypatch.setattr("app.actions.send_message", fake_smtp.send)
    monkeypatch.setattr("app.mailer.send_message", fake_smtp.send)
    return fake_smtp


def event_count():
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(CalendarEvent))


def test_free_slots_respects_busy_events_and_work_hours(client, monkeypatch):
    from datetime import datetime
    fixed_now = datetime.fromisoformat("2026-11-12T08:00:00+03:00")
    monkeypatch.setattr(get_settings().__class__, "now", lambda self: fixed_now)

    headers = login(client, "morozov")
    r = client.post(
        "/calendar/free-slots",
        json={"participant_ids": ["e-morozov", "e-volkov", "e-petrova"], "duration_minutes": 45, "days_ahead": 1},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    slots = r.json()
    assert slots
    # 09:30–10:30 занято у Морозова и Волкова, 14:00–14:30 — у Петровой; в 09:00–09:30 45 мин не помещаются.
    assert slots[0]["starts_at"].startswith("2026-11-12T10:30:00")
    assert slots[0]["ends_at"].startswith("2026-11-12T11:15:00")
    for s in slots:
        start = datetime.fromisoformat(s["starts_at"])
        end = datetime.fromisoformat(s["ends_at"])
        assert (end - start).total_seconds() == 45 * 60
        assert not (start < datetime.fromisoformat("2026-11-12T10:30:00+03:00") and end > datetime.fromisoformat("2026-11-12T09:30:00+03:00"))
        assert not (start < datetime.fromisoformat("2026-11-12T14:30:00+03:00") and end > datetime.fromisoformat("2026-11-12T14:00:00+03:00"))

    # Для 30-минутной встречи окно 09:00–09:30 подходит.
    r30 = client.post(
        "/calendar/free-slots",
        json={"participant_ids": ["e-morozov", "e-volkov"], "duration_minutes": 30, "days_ahead": 1},
        headers=headers,
    )
    assert r30.status_code == 200, r30.text
    assert r30.json()[0]["starts_at"].startswith("2026-11-12T09:00:00")

    assert client.post("/calendar/free-slots", json={"participant_ids": []}, headers=headers).status_code == 422
    assert client.post("/calendar/free-slots", json={"participant_ids": ["e-unknown"]}, headers=headers).status_code == 422


def test_free_slots_skip_weekends(client, monkeypatch):
    from datetime import datetime
    fixed_now = datetime.fromisoformat("2026-11-13T17:00:00+03:00")  # пятница, конец дня
    monkeypatch.setattr(get_settings().__class__, "now", lambda self: fixed_now)
    r = client.post("/calendar/free-slots", json={"participant_ids": ["e-morozov"], "duration_minutes": 30, "days_ahead": 2},
                    headers=login(client, "morozov"))
    assert r.status_code == 200, r.text
    days = {datetime.fromisoformat(s["starts_at"]).astimezone(get_settings().tz).date() for s in r.json()}
    assert days and all(d.weekday() < 5 for d in days)
    assert min(d.isoformat() for d in days) in ("2026-11-13", "2026-11-16")


def test_create_event_confirm_creates_event_ics_and_is_idempotent(client, smtp):
    headers = login(client, "morozov")
    before = event_count()
    payload = {
        "title": "Разбор испытаний бумаги",
        "topic": "Сравнение двух партий бумаги",
        "starts_at": "2026-11-13T11:00:00+03:00",
        "ends_at": "2026-11-13T11:45:00+03:00",
        "timezone": "Europe/Minsk",
        "attendee_ids": ["e-morozov", "e-volkov"],
        "agenda": [{"title": "Результаты измерений плотности", "minutes": 20, "sources": [], "returned_task_id": None}],
        "message": "Возьмите результаты лабораторных замеров.",
    }
    prop = client.post("/calendar/events/proposal", json=payload, headers=headers)
    assert prop.status_code == 200, prop.text
    card = prop.json()
    assert card["type"] == "create_event"
    assert card["status"] == "pending"
    assert card["preview"]["attendee_count"] == 2
    assert event_count() == before
    assert not smtp.messages

    # Правка карточки через PATCH /actions/{id} обновляет ревизию и хеш
    edited_payload = copy.deepcopy(card["payload"])
    edited_payload["message"] = "Обновлённый текст приглашения"
    edited = client.patch(f"/actions/{card['id']}", json={"revision": card["revision"], "payload": edited_payload}, headers=headers)
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2
    assert client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers).status_code == 409

    confirmed = client.post(f"/actions/{card['id']}/confirm", json=confirmation(edited.json()), headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    out = confirmed.json()
    assert out["status"] == "executed"
    assert out["result"]["delivery_status"] == "sent"
    assert event_count() == before + 1
    assert len(smtp.messages) == 1

    msg = smtp.messages[0]
    assert "i.morozov@fotoprint.test" in msg["To"]
    assert "d.volkov@fotoprint.test" in msg["To"]
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "invite.ics"
    ics_text = attachments[0].get_content()
    assert "BEGIN:VCALENDAR" in ics_text
    assert "BEGIN:VEVENT" in ics_text
    assert "SUMMARY:Разбор испытаний бумаги" in ics_text
    assert "ATTENDEE;RSVP=TRUE:mailto:d.volkov@fotoprint.test" in ics_text

    events = client.get("/calendar/events", headers=headers).json()
    assert any(ev["id"] == out["result"]["event_id"] and ev["source"] == "fokus" for ev in events)

    # Повторное подтверждение идемпотентно: не создаёт второе событие и не шлёт второе письмо
    repeat = client.post(f"/actions/{card['id']}/confirm", json=confirmation(edited.json()), headers=headers)
    assert repeat.status_code == 200
    assert repeat.json()["result"] == out["result"]
    assert event_count() == before + 1
    assert len(smtp.messages) == 1


def test_create_event_cancellation_has_no_business_effect(client, smtp):
    headers = login(client, "morozov")
    before = event_count()
    payload = {
        "title": "Отменяемая встреча",
        "starts_at": "2026-11-13T15:00:00+03:00",
        "ends_at": "2026-11-13T15:45:00+03:00",
        "timezone": "Europe/Minsk",
        "attendee_ids": ["e-morozov"],
    }
    card = client.post("/calendar/events/proposal", json=payload, headers=headers).json()
    cancelled = client.post(f"/actions/{card['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert event_count() == before
    assert not smtp.messages
    assert client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers).status_code == 409


def test_create_event_cannot_rewrite_held_meeting(client, smtp):
    headers = login(client, "morozov")
    with SessionLocal() as db:
        before = db.get(Meeting, "m-quality-1104")
        snapshot = (before.status, before.starts_at, before.title, list(before.participant_ids))
    payload = {
        "meeting_id": "m-quality-1104",
        "title": "Переписать прошедшую встречу",
        "starts_at": "2026-11-13T16:00:00+03:00",
        "ends_at": "2026-11-13T16:30:00+03:00",
        "timezone": "Europe/Minsk",
        "attendee_ids": ["e-morozov"],
    }
    r = client.post("/calendar/events/proposal", json=payload, headers=headers)
    assert r.status_code == 409
    with SessionLocal() as db:
        after = db.get(Meeting, "m-quality-1104")
        assert (after.status, after.starts_at, after.title, list(after.participant_ids)) == snapshot
    assert not smtp.messages


def test_create_event_rechecks_busy_slot_on_confirm(client, smtp):
    headers = login(client, "morozov")
    # Уже занятый слот в seed (ev-planning-1112: 09:30–10:30 12.11) отклоняется сразу
    busy_payload = {
        "title": "Конфликт с планированием",
        "starts_at": "2026-11-12T09:45:00+03:00",
        "ends_at": "2026-11-12T10:15:00+03:00",
        "timezone": "Europe/Minsk",
        "attendee_ids": ["e-morozov"],
    }
    assert client.post("/calendar/events/proposal", json=busy_payload, headers=headers).status_code == 409

    # Две разные карточки на один свободный слот: первая подтверждается, вторая получает 409 при повторной проверке
    slot_a = {
        "title": "Встреча А",
        "starts_at": "2026-11-16T11:00:00+03:00",
        "ends_at": "2026-11-16T11:45:00+03:00",
        "timezone": "Europe/Minsk",
        "attendee_ids": ["e-morozov", "e-volkov"],
    }
    slot_b = {
        "title": "Встреча Б в то же время",
        "starts_at": "2026-11-16T11:15:00+03:00",
        "ends_at": "2026-11-16T12:00:00+03:00",
        "timezone": "Europe/Minsk",
        "attendee_ids": ["e-volkov"],
    }
    card_a = client.post("/calendar/events/proposal", json=slot_a, headers=headers).json()
    card_b = client.post("/calendar/events/proposal", json=slot_b, headers=headers).json()

    before = event_count()
    r1 = client.post(f"/actions/{card_a['id']}/confirm", json=confirmation(card_a), headers=headers)
    assert r1.status_code == 200, r1.text
    assert event_count() == before + 1

    r2 = client.post(f"/actions/{card_b['id']}/confirm", json=confirmation(card_b), headers=headers)
    assert r2.status_code == 409
    assert event_count() == before + 1
    assert len(smtp.messages) == 1


def test_create_event_smtp_failure_and_retry_without_duplicate_event(client, smtp):
    headers = login(client, "morozov")
    before = event_count()
    payload = {
        "title": "Встреча при сбое почты",
        "starts_at": "2026-11-17T10:00:00+03:00",
        "ends_at": "2026-11-17T10:45:00+03:00",
        "timezone": "Europe/Minsk",
        "attendee_ids": ["e-morozov", "e-petrova"],
    }
    card = client.post("/calendar/events/proposal", json=payload, headers=headers).json()

    smtp.fail = True
    first = client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers)
    assert first.status_code == 200, first.text
    first_out = first.json()
    # Событие создано, но доставка упала — раздельные статусы
    assert first_out["status"] == "partially_failed"
    assert first_out["result"]["delivery_status"] == "failed"
    assert first_out["error"]
    assert event_count() == before + 1
    assert not smtp.messages

    with SessionLocal() as db:
        job = db.get(DeliveryJob, first_out["result"]["delivery_job_id"])
        assert job.status == "failed" and job.attempts == 1 and job.last_error

    # Повтор после восстановления SMTP отправляет письмо и НЕ создаёт второе событие
    smtp.fail = False
    retried = client.post(f"/actions/{card['id']}/confirm", json=confirmation(card), headers=headers)
    assert retried.status_code == 200, retried.text
    retried_out = retried.json()
    assert retried_out["status"] == "executed"
    assert retried_out["result"]["delivery_status"] == "sent"
    assert retried_out["result"]["event_id"] == first_out["result"]["event_id"]
    assert retried_out["error"] is None
    assert event_count() == before + 1
    assert len(smtp.messages) == 1

    with SessionLocal() as db:
        job = db.get(DeliveryJob, first_out["result"]["delivery_job_id"])
        assert job.status == "sent" and job.attempts == 2 and job.last_error is None


def test_send_summary_delivers_personal_tasks_and_common_decisions(client, gateway, smtp):
    gateway.data = {
        "summary": "Возврат входного контроля и испытания бумаги",
        "items": [
            {
                "kind": "decision",
                "statement": "Вернуть входной контроль каждой партии бумаги",
                "decision_status": "decided",
                "segment_id": 1,
                "evidence_quote": "Дмитрий, проведи испытания бумаги до пятницы.",
                "owner_raw": None,
                "due_raw": None,
            },
            {
                "kind": "task",
                "statement": "Провести испытания бумаги",
                "decision_status": "decided",
                "segment_id": 1,
                "evidence_quote": "Дмитрий, проведи испытания бумаги до пятницы.",
                "owner_raw": "Дмитрий",
                "due_raw": "до пятницы",
            },
        ],
    }
    headers = login(client, "morozov")
    draft, pub_card = draft_and_card(client, headers)
    pub_res = client.post(f"/actions/{pub_card['id']}/confirm", json=confirmation(pub_card), headers=headers)
    assert pub_res.status_code == 200, pub_res.text
    task_id = pub_res.json()["result"]["task_ids"][0]

    # После публикации протокола доступна карточка send_summary
    summary_cards = client.post(f"/drafts/{draft['id']}/proposals", headers=headers)
    assert summary_cards.status_code == 200, summary_cards.text
    summary_card = summary_cards.json()[0]
    assert summary_card["type"] == "send_summary"
    assert summary_card["status"] == "pending"
    assert summary_card["preview"]["recipient_count"] == 4
    assert summary_card["preview"]["task_count"] == 1

    letters_by_recipient = {l["recipient_id"]: l for l in summary_card["payload"]["letters"]}
    assert set(letters_by_recipient) == {"e-morozov", "e-petrova", "e-volkov", "e-kozlova"}
    assert letters_by_recipient["e-volkov"]["task_ids"] == [task_id]
    assert "Провести испытания бумаги" in letters_by_recipient["e-volkov"]["body"]
    assert "Вернуть входной контроль каждой партии бумаги" in letters_by_recipient["e-volkov"]["body"]
    assert letters_by_recipient["e-petrova"]["task_ids"] == []
    assert "Вернуть входной контроль каждой партии бумаги" in letters_by_recipient["e-petrova"]["body"]
    assert not smtp.messages

    # Проверяем сбой SMTP и последующий повтор для send_summary
    smtp.fail = True
    failed = client.post(f"/actions/{summary_card['id']}/confirm", json=confirmation(summary_card), headers=headers)
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert not smtp.messages

    smtp.fail = False
    confirmed = client.post(f"/actions/{summary_card['id']}/retry", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "executed"
    assert confirmed.json()["result"]["sent_count"] == 4
    assert len(smtp.messages) == 4
    recipients_sent = {m["To"] for m in smtp.messages}
    assert recipients_sent == {
        "i.morozov@fotoprint.test",
        "m.petrova@fotoprint.test",
        "d.volkov@fotoprint.test",
        "e.kozlova@fotoprint.test",
    }


def test_calendar_and_summary_acl_and_cancellation(client, gateway, smtp):
    director, production = login(client, "director"), login(client, "morozov")

    # Приватное событие директора не видно руководителю производства
    priv_payload = {
        "title": "Закрытое совещание у директора",
        "starts_at": "2026-11-18T11:00:00+03:00",
        "ends_at": "2026-11-18T11:45:00+03:00",
        "timezone": "Europe/Minsk",
        "attendee_ids": ["e-smirnova"],
    }
    priv_card = client.post("/calendar/events/proposal", json=priv_payload, headers=director).json()
    assert client.post(f"/actions/{priv_card['id']}/confirm", json=confirmation(priv_card), headers=production).status_code == 403
    priv_out = client.post(f"/actions/{priv_card['id']}/confirm", json=confirmation(priv_card), headers=director).json()
    assert priv_out["status"] == "executed"
    prod_events = {ev["id"] for ev in client.get("/calendar/events", headers=production).json()}
    dir_events = {ev["id"] for ev in client.get("/calendar/events", headers=director).json()}
    assert priv_out["result"]["event_id"] in dir_events
    assert priv_out["result"]["event_id"] not in prod_events

    # Отмена карточки send_summary не отправляет писем и позволяет создать новую карточку
    smtp.messages.clear()
    draft, pub_card = draft_and_card(client, production)
    client.post(f"/actions/{pub_card['id']}/confirm", json=confirmation(pub_card), headers=production)
    summary_card = client.post(f"/drafts/{draft['id']}/proposals", headers=production).json()[0]
    assert client.post(f"/actions/{summary_card['id']}/cancel", headers=production).json()["status"] == "cancelled"
    assert not smtp.messages
    reproposed = client.post(f"/drafts/{draft['id']}/summary-proposal", headers=production)
    assert reproposed.status_code == 200
    assert reproposed.json()["id"] != summary_card["id"]
    assert reproposed.json()["status"] == "pending"


def test_published_protocol_kpi_decision_proposes_create_decision_card_only(client, gateway):
    from app.models import Decision

    kpi_text = (
        "Илья Морозов: Решили снизить брак на ламинации: Елена Козлова до пятницы вводит 100% входной контроль клея.\n"
        "Дмитрий Волков: Проведу замеры температуры валов до пятницы."
    )
    gateway.data = {
        "summary": "Решение по снижению брака и замеры температуры",
        "items": [
            {
                "kind": "decision",
                "statement": "Снизить брак на ламинации: ввести 100% входной контроль клея",
                "decision_status": "decided",
                "segment_id": 1,
                "evidence_quote": "Решили снизить брак на ламинации: Елена Козлова до пятницы вводит 100% входной контроль клея.",
                "owner_raw": "Елена Козлова",
                "due_raw": "до пятницы",
            },
            {
                "kind": "task",
                "statement": "Провести замеры температуры валов",
                "decision_status": "decided",
                "segment_id": 2,
                "evidence_quote": "Проведу замеры температуры валов до пятницы.",
                "owner_raw": None,
                "due_raw": "до пятницы",
            },
        ],
    }
    headers = login(client, "morozov")
    with SessionLocal() as db:
        decisions_before = db.scalar(select(func.count()).select_from(Decision))

    r_draft = client.post("/meetings/m-quality-1104/transcript", json={"text": kpi_text}, headers=headers)
    assert r_draft.status_code == 200, r_draft.text
    draft = r_draft.json()
    pub_card = client.post(f"/drafts/{draft['id']}/proposals", headers=headers).json()[0]

    pub_res = client.post(f"/actions/{pub_card['id']}/confirm", json=confirmation(pub_card), headers=headers)
    assert pub_res.status_code == 200, pub_res.text

    # Публикация протокола сама решение в БД НЕ создаёт — только предлагает карточку create_decision
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Decision)) == decisions_before

    post_cards = client.post(f"/drafts/{draft['id']}/proposals", headers=headers).json()
    dec_card = next((c for c in post_cards if c["type"] == "create_decision"), None)
    assert dec_card is not None
    assert dec_card["status"] == "pending" and dec_card["origin"] == "ai"
    assert dec_card["payload"]["metric_id"] == "defect_rate"
    assert dec_card["payload"]["action_text"] == "Снизить брак на ламинации: ввести 100% входной контроль клея"
    assert dec_card["payload"]["owner_employee_id"] == "e-kozlova"
    assert dec_card["payload"]["due_at"].startswith("2026-11-06T18:00:00")

    # И только после подтверждения карточки create_decision решение появляется в реестре
    conf_dec = client.post(f"/actions/{dec_card['id']}/confirm", json=confirmation(dec_card), headers=headers)
    assert conf_dec.status_code == 200, conf_dec.text
    assert conf_dec.json()["status"] == "executed"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Decision)) == decisions_before + 1


def test_protocol_timeout_returns_503_and_keeps_uploaded_transcript(client, monkeypatch):
    import httpx

    class TimeoutGateway:
        route, model, provider = "local", "test-model", "ollama"

        async def chat(self, messages, **kwargs):
            raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr("app.routers.workflow.get_gateway", lambda: TimeoutGateway())
    headers = login(client, "morozov")
    timeout_text = "Илья Морозов: Проверим сохранность этой расшифровки при таймауте модели."
    response = client.post("/meetings/m-quality-1104/transcript", json={"text": timeout_text}, headers=headers)
    assert response.status_code == 503
    assert response.json()["code"] == "llm_unavailable"
    assert "не ответила вовремя" in response.json()["detail"] and "Расшифровка сохранена" in response.json()["detail"]
    rows = client.get("/meetings/m-quality-1104/transcripts", headers=headers).json()
    assert rows[0]["text"] == timeout_text
