"""Тесты идут на отдельной БД fokus_test (создаётся в db-init) и на ФИКСИРОВАННОМ наборе tests/fixtures/data.

Демо-набор в data/ может меняться — тесты от него не зависят. Нужны новые данные для теста —
добавляйте в tests/fixtures/data или создавайте прямо в тесте.

Запуск:  docker compose run --rm api pytest
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://fokus:fokus@db:5432/fokus_test")
os.environ["SEED_ON_START"] = "false"
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DEMO_PASSWORD", "test-pass")
os.environ["DATA_DIR"] = str(Path(__file__).resolve().parent / "fixtures" / "data")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, drop_all, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_all  # noqa: E402


@pytest.fixture(scope="session")
def _db():
    # Нельзя стереть рабочую БД ошибочной переменной окружения.
    if not urlsplit(os.environ["DATABASE_URL"]).path.rstrip("/").endswith("_test"):
        pytest.fail("TEST_DATABASE_URL должен указывать на отдельную БД с суффиксом _test")
    drop_all()
    init_db()
    with SessionLocal() as db:
        seed_all(db)
    yield


@pytest.fixture()
def client(_db):
    with TestClient(app) as c:
        yield c


def login(client: TestClient, login_name: str) -> dict:
    r = client.post("/auth/login", json={"login": login_name, "password": os.environ["DEMO_PASSWORD"]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
