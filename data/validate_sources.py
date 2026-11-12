"""Проверка экспортов, ACL эталонов и gold без БД и LLM; вызывается validate.py."""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from app.connectors import PARSERS
from app.ingest import parse_front_matter, parse_transcript
from zoneinfo import ZoneInfo


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_sources(root, employees, departments, tasks, meetings, users, static_documents, now):
    source_root = root / 'sources'
    channels = json.loads((source_root / 'channels.json').read_text(encoding='utf-8'))
    glossary = json.loads((root / 'glossary.json').read_text(encoding='utf-8'))
    require(len({c['id'] for c in channels}) == len(channels), 'Дубли ID каналов')
    require(len({e['id'] for e in glossary}) == len(glossary), 'Дубли ID глоссария')
    for entity in glossary:
        require(entity['type'] in {'product', 'supplier', 'equipment', 'topic'}, 'Тип сущности')
        require(entity['name'] and isinstance(entity['aliases'], list), 'Имя/алиасы сущности')
    channel_ids = {c['id'] for c in channels}
    require(Counter(c['kind'] for c in channels) == {'email': 3, 'telegram': 3, 'chat': 2}, 'Состав каналов v2')
    by_email = {e['email'].lower(): key for key, e in employees.items()}
    by_name = {name.casefold(): key for key, e in employees.items() for name in [e['full_name'], *e['aliases']]}
    documents = {key: {'access': doc['access'], 'department': doc.get('department'), 'allowed': set(),
                       'ai_allowed': doc['ai_allowed'], 'text': parse_front_matter((root / 'documents' / (key + '.md')).read_text(encoding='utf-8'))[1]}
                 for key, doc in static_documents.items()}
    messages = {}
    counts = {}
    for channel in channels:
        cid = channel['id']
        require(channel['access_level'] in {'public', 'department', 'participants'}, f'ACL канала {cid}')
        require(channel['department_id'] in departments and set(channel['member_ids']) <= employees.keys(), f'Участники {cid}')
        require(channel['member_ids'] or channel['access_level'] != 'participants', f'Пустые участники {cid}')
        path = (source_root / channel['path']).resolve()
        require(path.is_relative_to(source_root.resolve()) and path.exists(), f'Путь канала {cid}')
        expected = {'email': 'email_eml', 'telegram': 'telegram_export', 'chat': 'chat_json'}
        require(channel['connector'] == expected[channel['kind']], f'Коннектор {cid}')
        parsed = PARSERS[channel['connector']](path, ZoneInfo('Europe/Minsk'))
        require(parsed and len({m.external_id for m in parsed}) == len(parsed), f'Пустой канал/дубли сообщений {cid}')
        if channel['kind'] == 'telegram':
            exported = json.loads(path.read_text(encoding='utf-8'))
            for raw in exported['messages']:
                if raw['type'] != 'message':
                    continue
                require(re.fullmatch(r'user\d+', raw['from_id']), f'Идентификатор Telegram {cid}')
                if raw.get('date_unixtime'):
                    require(int(datetime.fromisoformat(raw['date']).timestamp()) == int(raw['date_unixtime']), f'Два времени Telegram не совпадают {cid}')
        counts[cid] = len(parsed)
        for message in parsed:
            require(message.text and '\ufffd' not in message.text, f'Текст/кодировка {cid}')
            require(message.sent_at.utcoffset() == timedelta(hours=3) and message.sent_at <= now, f'Время сообщения {cid}')
            author = by_email.get((message.author_email or '').lower()) or by_name.get(message.author_raw.casefold())
            if channel['kind'] != 'email':
                require(author in employees, f'Неизвестный автор {cid}')
                if channel['access_level'] == 'participants':
                    require(author in channel['member_ids'], f'Автор не участник {cid}')
            allowed = set(channel['member_ids'])
            if channel['kind'] == 'email':
                allowed.update(by_email[a] for a in message.recipients if a in by_email)
                if author:
                    allowed.add(author)
                doc_id = cid + ':' + hashlib.sha1(message.external_id.encode()).hexdigest()[:12]
            else:
                doc_id = cid + ':' + message.sent_at.date().isoformat()
            record = documents.setdefault(doc_id, {'access': channel['access_level'], 'department': channel['department_id'],
                                                   'allowed': allowed, 'ai_allowed': True, 'text': '', 'channel_id': cid})
            record['text'] += message.text + '\n'
            messages[cid, message.external_id] = {'document_id': doc_id, 'message': message}
    require(sum(counts.values()) == 30, f'Ожидалось 30 сообщений: {counts}')

    def visible(doc, user):
        if not doc['ai_allowed']:
            return False
        if doc['access'] == 'participants':
            return user['employee_id'] in doc['allowed']
        return user['role'] == 'director' or doc['access'] == 'public' or (doc['access'] == 'department' and doc['department'] == user['department_id'])

    eval_root = root.parent / 'eval'
    questions = [json.loads(line) for line in (eval_root / 'questions.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    require(len(questions) == 48 and len({q['id'] for q in questions}) == 48, '48 уникальных вопросов')
    require(Counter(q['expect'] for q in questions[:30]) == {'answered': 18, 'not_found': 10, 'refused': 2}, 'Состав первых 30 вопросов')
    for question in questions:
        role_users = [user for user in users.values() if user['role'] == question['role']]
        require(len(role_users) == 1, f"Роль вопроса {question['id']}")
        user = role_users[0]
        require(question['expect'] in {'answered', 'partial', 'not_found', 'refused'}, 'Статус ответа')
        for doc_id in question.get('sources', []):
            require(doc_id in documents and visible(documents[doc_id], user), f"Недоступное основание {question['id']}: {doc_id}")
        for doc_id in question.get('forbidden_sources', []):
            require(doc_id in documents and not visible(documents[doc_id], user), f"Основание не закрыто {question['id']}: {doc_id}")
        require(set(question.get('forbidden_channels', [])) <= channel_ids, 'Неизвестный закрытый канал')
        for cid in question.get('forbidden_channels', []):
            require(not any(visible(d, user) for d in documents.values() if d.get('channel_id') == cid), f'Канал не закрыт для роли: {cid}')
        allowed_text = '\n'.join(doc['text'].casefold() for doc in documents.values() if visible(doc, user))
        for value in question.get('forbidden_answer_contains', []):
            require(value.casefold() not in allowed_text, f"Секрет продублирован в доступном тексте {question['id']}")
        require(set(question.get('experts_any', [])) <= employees.keys(), 'Неизвестный эксперт')
    for pair in {q['privacy_pair'] for q in questions if 'privacy_pair' in q}:
        matched = [q for q in questions if q.get('privacy_pair') == pair]
        require(len(matched) == 2 and {q['expect'] for q in matched} == {'answered', 'not_found'}, f'Пара приватности {pair}')

    cases = json.loads((eval_root / 'source-cases.json').read_text(encoding='utf-8'))['cases']
    require(Counter(c['type'] for c in cases) == {'contradiction': 4, 'commitment': 4}, 'Состав противоречий/обещаний')
    for case in cases:
        source = case['source']
        entry = messages[source['channel_id'], source['external_id']]
        require(entry['document_id'] == source['document_id'] and case['quote'].casefold() in entry['message'].text.casefold(), f"Основание кейса {case['id']}")
        if case['task_id']:
            task = tasks[case['task_id']]
            require(all(task[key] == value for key, value in case['registry'].items()), f"Реестр кейса {case['id']}")
            require(any(task[key] != value for key, value in case['statement'].items()), f"Нет противоречия {case['id']}")

    gold_files = list((eval_root / 'gold').glob('*.json'))
    require(len(gold_files) == 4, 'Нужны gold по 3 расшифровкам и короткой сцене')
    gold_transcripts = set()
    item_count = 0
    for path in gold_files:
        gold = json.loads(path.read_text(encoding='utf-8'))
        filename = gold['transcript']
        require(Path(filename).name == filename, 'Некорректное имя расшифровки')
        gold_transcripts.add(filename)
        require(gold['meeting_id'] in meetings and gold['timezone'] == 'Europe/Minsk', 'Встреча/пояс gold')
        segments = parse_transcript((root / 'transcripts' / filename).read_text(encoding='utf-8'))
        keys = [(item['kind'], item['segment_id']) for item in gold['items']]
        require(len(set(keys)) == len(keys), 'Дубли пунктов gold')
        for item in gold['items']:
            require(1 <= item['segment_id'] <= len(segments), 'Сегмент gold')
            quote = item['evidence_quote']
            require(quote and quote in segments[item['segment_id'] - 1]['text'], 'Нет цитаты gold в реплике')
            if item['kind'] == 'proposal':
                require(item['decision_status'] == 'proposed' and item.get('context_segment_ids'), 'Предложение ошибочно стало решением')
            if item['kind'] == 'task':
                require(item['owner'] is None or item['owner'] in employees, 'Владелец gold')
                if item['owner']:
                    require(item['owner_raw'] in quote and by_name[item['owner_raw'].casefold()] == item['owner'], 'Алиас владельца gold')
                require(item['owner_origin'] == ('explicit' if item['owner'] else 'unknown'), 'Origin владельца gold')
                require(item['due_origin'] == ('explicit' if item['due'] else 'unknown'), 'Origin срока gold')
                if item['due']:
                    require(item['due_raw'] in quote and item['due_at'] == item['due'] + 'T18:00:00+03:00', 'Срок gold')
                    require(datetime.fromisoformat(item['due_at']) >= datetime.fromisoformat(meetings[gold['meeting_id']]['starts_at']), 'Срок gold до встречи')
                flags = ([] if item['owner'] else ['owner_employee_id']) + ([] if item['due'] else ['due_at'])
                require(item['needs_clarification'] == flags, 'Флаги уточнения gold')
        if filename != 'demo-short.txt':
            require({i['kind'] for i in gold['items']} == {'discussion', 'proposal', 'decision', 'task', 'question', 'risk'}, 'Шесть видов gold')
            require(sum(i['kind'] == 'task' for i in gold['items']) == 4, 'Четыре новые задачи gold')
        require((gold['split'] == 'holdout') == (filename == 'control-1111.txt'), 'Контрольная №3 должна остаться holdout')
        item_count += len(gold['items'])
    require(gold_transcripts == {p.name for p in (root / 'transcripts').glob('*.txt')}, 'Покрытие gold')
    print(f'Источники OK: {len(channels)} каналов, {sum(counts.values())} сообщений, {len(glossary)} сущностей')
    print(f'Eval OK: {len(questions)} вопросов, {len(cases)} кейсов, {len(gold_files)} gold / {item_count} пункта')
