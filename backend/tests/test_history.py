from conftest import login
from app.db import SessionLocal
from app.models import Meeting, new_id


def test_audit_is_scoped_and_does_not_copy_answer_text(client):
    director, production = login(client, "director"), login(client, "morozov")
    client.get("/documents/sample-director-decision", headers=production)
    rows = client.get("/audit", headers=production).json()
    assert rows and all(row["user_id"] == "u-morozov" for row in rows)
    assert any(row["result"] == "denied" and row["object_id"] == "sample-director-decision" for row in rows)
    assert any(row["user_id"] == "u-director" for row in client.get("/audit", headers=director).json())
    assert client.get("/audit?limit=1000", headers=production).status_code == 422


def test_revoked_meeting_hides_saved_draft_card_and_audit(client, monkeypatch):
    from test_workflow import FakeGateway, TEXT, confirmation
    gateway = FakeGateway()
    monkeypatch.setattr("app.routers.workflow.get_gateway", lambda: gateway)
    headers = login(client, "morozov")
    with SessionLocal() as db:
        meeting = Meeting(id=new_id("scoped"), title="Встреча", department_id="production", participant_ids=[])
        db.add(meeting)
        db.commit()
        meeting_id = meeting.id
    draft = client.post(f"/meetings/{meeting_id}/transcript", headers=headers, json={"text": TEXT}).json()
    card = client.post(f"/drafts/{draft['id']}/proposals", headers=headers).json()[0]
    with SessionLocal() as db:
        db.get(Meeting, meeting_id).department_id = "management"
        db.commit()
    assert card["id"] not in {row["id"] for row in client.get("/actions", headers=headers).json()}
    assert client.post(f"/actions/{card['id']}/confirm", headers=headers, json=confirmation(card)).status_code == 403
    assert client.get(f"/drafts/{draft['id']}", headers=headers).status_code == 403
    rows = client.get("/audit", headers=headers).json()
    assert not any(row["object_id"] in {draft["id"], card["id"]} and row["result"] != "denied" for row in rows)
