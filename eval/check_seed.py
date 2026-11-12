"""SQL/API smoke нового seed без внешней LLM и без измерения качества ответов.

После app.cli reset: задать DATABASE_URL, DATA_DIR, DEMO_PASSWORD и выполнить
python eval/check_seed.py из корня. Данные не сбрасываются и не исправляются.
Авторизация участников проверяется отдельно от качества генерации.
"""

import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import models as m  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.connectors import PARSERS  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.evidence import resolve_due, resolve_owner  # noqa: E402
from app.llm.gateway import LLMResult  # noqa: E402
from app.main import app  # noqa: E402
from app.security import can_read_document, current_user, readable_document_filter  # noqa: E402


def require(condition, message):
    if not condition:
        raise ValueError(message)


class CaptureGateway:
    route, model, provider = 'local', 'seed-privacy-check-without-LLM', 'fake'

    def __init__(self):
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return LLMResult(text='', data={'status': 'not_found', 'claims': [], 'unanswered': []},
                         provider=self.provider, model=self.model, route=self.route, latency_ms=0)


def main():
    logging.getLogger('httpx').setLevel(logging.WARNING)
    settings = get_settings()
    channels = json.loads((settings.data_dir / 'sources/channels.json').read_text(encoding='utf-8'))
    with SessionLocal() as db:
        users = {u.login: u for u in db.scalars(select(m.User)).all()}
        docs = {d.id: d for d in db.scalars(select(m.Document)).all()}
        seeded = {s.id: s for s in db.scalars(select(m.SourceChannel)).all()}
        people = list(db.scalars(select(m.Employee)).all())
        require(set(seeded) == {c['id'] for c in channels}, 'Каналы в БД не совпадают с seed')
        for channel in channels:
            actual = seeded[channel['id']]
            count = len(PARSERS[channel['connector']](settings.data_dir / 'sources' / channel['path'], settings.tz))
            require(actual.status == 'ok' and actual.last_error is None and actual.items_count == count, f'Канал {actual.id}: {actual.status}')
        require(sum(s.items_count for s in seeded.values()) == 30, 'Количество сообщений')
        require(len(docs) == 43 and sum(bool(d.channel_id) for d in docs.values()) == 19, 'Количество документов')
        for user in users.values():
            sql_ids = set(db.scalars(select(m.Document.id).where(readable_document_filter(user, for_ai=True))))
            expected_ids = {d.id for d in docs.values() if d.ai_allowed and can_read_document(user, d)}
            require(sql_ids == expected_ids, f'SQL ACL не совпадает с ACL чтения: {user.login}')
        for path in (ROOT / 'eval/gold').glob('*.json'):
            gold = json.loads(path.read_text(encoding='utf-8'))
            meeting = db.get(m.Meeting, gold['meeting_id'])
            for item in gold['items']:
                if item['kind'] != 'task':
                    continue
                require(resolve_owner(item['owner_raw'], people) == item['owner'], 'Владелец gold не совпадает с сервером')
                due = resolve_due(item['due_raw'], meeting.starts_at, meeting.timezone)
                require((due.astimezone(ZoneInfo('Europe/Minsk')).isoformat() if due else None) == item['due_at'], 'Срок gold не совпадает с сервером')

    gateway, headers, seen = CaptureGateway(), {}, set()
    with TestClient(app) as client:
        for login in ('director', 'morozov'):
            response = client.post('/auth/login', json={'login': login, 'password': settings.demo_password})
            require(response.status_code == 200, f'Вход {login}')
            headers[login] = {'Authorization': 'Bearer ' + response.json()['access_token']}
            response = client.get('/sources', headers=headers[login])
            require(response.status_code == 200, f'/sources: {login}')
            rows = response.json()
            require(all(row['status'] == 'ok' for row in rows), f'Статусы /sources: {login}')
            expected = {c['id'] for c in channels if (users[login].employee_id in c['member_ids'] if c['access_level'] == 'participants'
                        else c['access_level'] == 'public' or users[login].role == 'director' or c['department_id'] == users[login].department_id)}
            require({row['id'] for row in rows} == expected, f'Видимость каналов: {login}')
            seen.update(expected)
            print(f'GET /sources {login}: {len(rows)} каналов, все ok')

        # У Петровой нет отдельной seed-учётки. Только для GET /sources подставляем
        # её идентичность; не меняем БД, не обходим participants для директора.
        petrova = m.User(id='qa-petrova', login='qa-petrova', full_name='Мария Петрова', role='production_head',
                         department_id='production', employee_id='e-petrova', is_active=True, password_hash='unused')
        app.dependency_overrides[current_user] = lambda: petrova
        try:
            rows = client.get('/sources').json()
            require({row['id'] for row in rows} == {'tg-production', 'chat-quality', 'mail-petrova', 'tg-procurement'}, 'Каналы Петровой')
            require(all(row['status'] == 'ok' for row in rows), 'Статусы Петровой')
            seen.update(row['id'] for row in rows)
        finally:
            app.dependency_overrides.pop(current_user, None)
        require(seen == set(seeded), 'Не все каналы проверены через /sources')
        print('GET /sources Петрова (тестовая идентичность): 4 канала, все ok; суммарно проверены 8/8')

        questions = [json.loads(line) for line in (ROOT / 'eval/questions.jsonl').read_text(encoding='utf-8').splitlines()]
        with patch('app.routers.assistant.get_gateway', lambda: gateway):
            for question in questions:
                login = 'director' if question['role'] == 'director' else 'morozov'
                user = users[login]
                gateway.calls.clear()
                response = client.post('/assistant/ask', json={'question': question['question']}, headers=headers[login])
                require(response.status_code == 200, f"Запрос {question['id']}")
                prompt = json.dumps(gateway.calls, ensure_ascii=False)
                for value in question.get('forbidden_answer_contains', []):
                    require(value.casefold() not in prompt.casefold(), f"Закрытое значение в промпте {question['id']}")
                for call in gateway.calls:
                    payload = json.loads(call[-1]['content'])
                    for fragment in payload['source_fragments']:
                        doc = docs[fragment['chunk_id'].rsplit('#', 1)[0]]
                        require(doc.ai_allowed and can_read_document(user, doc), f"Закрытый фрагмент {question['id']}")
                        require(doc.id not in question.get('forbidden_sources', []) and doc.channel_id not in question.get('forbidden_channels', []), 'Запрещённое основание в промпте')
                if question['expect'] == 'refused':
                    require(not gateway.calls and response.json()['status'] == 'refused', 'Чувствительный запрос вызвал модель')
        for login in ('director', 'morozov'):
            for focus in ('entity:topic-client-template', 'entity:topic-crm-refresh', 'employee:e-petrova'):
                response = client.get('/graph', params={'focus': focus}, headers=headers[login])
                require(response.status_code == 200, 'Граф недоступен')
                for node in response.json()['nodes']:
                    if node['ref_id'] in docs:
                        require(can_read_document(users[login], docs[node['ref_id']]), 'Закрытый узел в графе')
        for question in questions:
            login = 'director' if question['role'] == 'director' else 'morozov'
            for doc_id in question.get('forbidden_sources', []):
                response = client.get('/documents/' + doc_id, headers=headers[login])
                # ai_allowed=false запрещает контекст AI, а не разрешённый
                # директору просмотр реестра окладов человеком.
                expected = 200 if can_read_document(users[login], docs[doc_id]) else 403
                require(response.status_code == expected, f"ACL вьюера {question['id']}: {doc_id}")
    with SessionLocal() as db:
        require(db.scalar(select(func.count()).select_from(m.Task)) == 20, 'Вызовы ассистента изменили реестр')
        require(db.scalar(select(func.count()).select_from(m.ActionProposal)) == 0, 'Проверка создала карточки')
    print('SQL/API OK: ACL поиска, вьюера и графа; 48 запросов проверены на закрытые фрагменты в промпте')
    print('Gold: 14 владельцев/сроков согласуются с сервером. LLM не вызывалась, качество ответов не измерялось.')


if __name__ == '__main__':
    main()
