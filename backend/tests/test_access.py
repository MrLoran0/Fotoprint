"""Критические проверки доступа (раздел 7 плана): сервер, а не интерфейс, решает, что видно."""

from conftest import login

from app.db import SessionLocal
from app.models import AuditLog


def test_login_wrong_password_is_denied_and_audited(client):
    r = client.post("/auth/login", json={"login": "director", "password": "nope"})
    assert r.status_code == 401
    with SessionLocal() as db:
        last = db.query(AuditLog).filter_by(action="auth.login").order_by(AuditLog.id.desc()).first()
        assert last.result == "denied"


def test_me_requires_token(client):
    assert client.get("/me").status_code == 401


def test_production_head_cannot_open_confidential_document(client):
    h = login(client, "morozov")
    r = client.get("/documents/sample-director-decision", headers=h)
    assert r.status_code == 403
    listed = {d["id"] for d in client.get("/documents", headers=h).json()}
    assert "sample-director-decision" not in listed
    with SessionLocal() as db:
        last = db.query(AuditLog).filter_by(action="document.open").order_by(AuditLog.id.desc()).first()
        assert (last.object_id, last.result) == ("sample-director-decision", "denied")


def test_director_can_open_confidential_document(client):
    h = login(client, "director")
    assert client.get("/documents/sample-director-decision", headers=h).status_code == 200


def test_missing_and_forbidden_documents_look_the_same(client):
    h = login(client, "morozov")
    a = client.get("/documents/sample-director-decision", headers=h)
    b = client.get("/documents/does-not-exist", headers=h)
    assert a.status_code == b.status_code == 403
    assert a.json() == b.json()
