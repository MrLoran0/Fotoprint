"""Evaluate a running, isolated Fokus API. Never confirms actions or resets data.

Requires backend's httpx. Provider must already be selected on the API server.
Raw results contain synthetic answers, never authorization tokens or passwords.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from urllib.parse import quote

import httpx


def normalize(text):
    text = str(text).casefold().replace('ё', 'е').replace('\u00a0', ' ')
    text = re.sub(r'\b(20\d\d)-(\d\d)-(\d\d)(?!\d)', r'\3.\2.\1', text)
    text = re.sub(r'(?<=\d)\s+(?=\d)', '', text)
    text = re.sub(r'(?<=\d),(?=\d)', '.', text)
    return ' '.join(text.split())


def contains(text, fragment):
    return normalize(fragment) in normalize(text)


def quote_present(text, fragment):
    # Citation verification must not rewrite numbers or dates.
    clean = lambda s: ' '.join(str(s).casefold().replace('ё', 'е').split())
    return bool(clean(fragment)) and clean(fragment) in clean(text)


def ratio(numerator, denominator):
    return f'{numerator}/{denominator}' + (f' ({100*numerator/denominator:.1f}%)' if denominator else ' (н/д)')


def latency(values):
    if not values:
        return {'n': 0, 'median_ms': None, 'p95_ms': None}
    ordered = sorted(values)
    return {'n': len(values), 'median_ms': round(statistics.median(values), 1),
            'p95_ms': round(ordered[max(0, math.ceil(.95*len(values))-1)], 1)}


def source_refs(answer):
    return [s for c in answer.get('claims', []) for s in c.get('sources', [])]


def score_question(case, answer, verified_sources):
    """Lexical proxies and observable response checks; no claim of semantic entailment."""
    text = answer.get('answer', '') + '\n' + '\n'.join(c.get('text', '') for c in answer.get('claims', []))
    refs = source_refs(answer)
    serialized = json.dumps(answer, ensure_ascii=False)
    checks = {'status': answer.get('status') == case['expect']}
    if case.get('answer_contains'):
        checks['answer_contains'] = all(contains(text, x) for x in case['answer_contains'])
    if case.get('sources'):
        checks['expected_source'] = any(s.get('source_id') in case['sources'] and verified_sources.get(i, False)
                                        for i, s in enumerate(refs))
    if case.get('gaps_contains'):
        checks['gaps'] = all(contains('\n'.join(answer.get('gaps', [])), x) for x in case['gaps_contains'])
    if case.get('experts_any'):
        checks['experts'] = any(e.get('employee_id') in case['experts_any'] and e.get('reason', '').strip()
                                for e in answer.get('experts', []))
    checks['no_forbidden_source'] = not any(contains(serialized, x) for x in
                                           case.get('forbidden_sources', [])+case.get('forbidden_channels', []))
    checks['no_forbidden_value'] = not any(contains(serialized, x) for x in case.get('forbidden_answer_contains', []))
    checks['all_citations_verified'] = all(verified_sources.get(i, False) for i in range(len(refs)))
    checks['facts_have_sources'] = all(c.get('sources') for c in answer.get('claims', []) if c.get('kind', 'fact') == 'fact')
    if answer.get('status') in ('answered', 'partial'):
        checks['nonempty_claims'] = bool(answer.get('claims'))
    checks['route_visible'] = answer.get('route') in ('local', 'external') and bool(answer.get('model'))
    return checks


def match_items(gold, actual):
    """Maximum one-to-one structural matching by kind AND source segment.

    Wording is intentionally not declared semantically equivalent. Ties are
    resolved by lexical overlap, then indices; duplicates remain false positives.
    """
    def overlap(g, a):
        left, right = set(normalize(g.get('statement', '')).split()), set(normalize(a.get('statement', '')).split())
        return len(left & right) / max(1, len(left | right))

    @lru_cache(None)
    def best(index, used):
        if index == len(actual):
            return 0, 0.0, ()
        result = best(index+1, used)
        for j, expected in enumerate(gold):
            if used & (1 << j) or actual[index].get('kind') != expected['kind']:
                continue
            if actual[index].get('segment_id') != expected['segment_id']:
                continue
            count, weight, pairs = best(index+1, used | (1 << j))
            candidate = count+1, weight+overlap(expected, actual[index]), ((j, index),)+pairs
            if candidate[:2] > result[:2] or (candidate[:2] == result[:2] and candidate[2] < result[2]):
                result = candidate
        return result
    return list(best(0, 0)[2])


def score_protocol(gold, draft):
    actual = [i for i in draft['items'] if not i.get('removed')]
    expected = gold['items']
    pairs = match_items(expected, actual)
    task_pairs = [(g, a) for g, a in pairs if expected[g]['kind'] == 'task']
    segments = {s['n']: s['text'] for s in draft.get('segments', [])}
    verified = [bool(i.get('quote_verified')) and bool(i.get('evidence_quote', '').strip()) and
                quote_present(segments.get(i.get('segment_id'), ''), i.get('evidence_quote', '')) for i in actual]
    def due_date(value):
        if not value:
            return None
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(value).astimezone(ZoneInfo('Europe/Minsk')).date().isoformat()
    decided = [i for i in actual if i['kind'] == 'decision' and i.get('decision_status') == 'decided']
    proposals = [i for i in expected if i['kind'] == 'proposal']
    return {
        'matched_items': len(pairs), 'gold_items': len(expected), 'actual_items': len(actual),
        'matched_tasks': len(task_pairs), 'gold_tasks': sum(i['kind'] == 'task' for i in expected),
        'actual_tasks': sum(i['kind'] == 'task' for i in actual),
        'owners_correct': sum(actual[a].get('owner_employee_id') == expected[g].get('owner') for g, a in task_pairs),
        'due_correct': sum(due_date(actual[a].get('due_at')) == expected[g].get('due') for g, a in task_pairs),
        'clarification_correct': sum(set(actual[a].get('needs_clarification', [])) == set(expected[g].get('needs_clarification', [])) for g, a in task_pairs),
        'origins_correct': sum(all(actual[a].get(f) == expected[g].get(f) for f in ('owner_origin', 'due_origin')) for g, a in task_pairs),
        'status_correct': sum(actual[a].get('decision_status') == expected[g].get('decision_status') for g, a in pairs if expected[g]['kind'] != 'task'),
        'status_total': sum(expected[g]['kind'] != 'task' for g, a in pairs),
        'quotes_verified': sum(verified), 'quotes_total': len(actual),
        'proposal_as_decision': sum(any(a.get('segment_id') in [g['segment_id'], *g.get('context_segment_ids', [])] for a in decided) for g in proposals),
        'gold_proposals': len(proposals),
        'missing_gold': [i for i in range(len(expected)) if i not in {g for g, _ in pairs}],
        'extra_actual': [i for i in range(len(actual)) if i not in {a for _, a in pairs}],
        'matches': pairs,
    }


class Runner:
    def __init__(self, client):
        self.client, self.tokens, self.document_cache = client, {}, {}

    def request(self, method, path, login=None, **kwargs):
        headers = {'Authorization': 'Bearer '+self.tokens[login]} if login else {}
        started = time.perf_counter()
        response = self.client.request(method, path, headers=headers, **kwargs)
        elapsed = round((time.perf_counter()-started)*1000, 1)
        response.raise_for_status()
        return response.json(), elapsed

    def citations(self, answer, login):
        verified = {}
        for i, ref in enumerate(source_refs(answer)):
            key = login, ref.get('source_id')
            if key not in self.document_cache:
                try:
                    doc, _ = self.request('GET', '/documents/'+quote(str(key[1]), safe=''), login)
                    self.document_cache[key] = doc
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in (403, 404):
                        raise
                    self.document_cache[key] = {}
            doc = self.document_cache[key]
            # Current public viewer exposes full content, not chunks. Check quote,
            # ACL and ai_allowed here; exact chunk provenance is a server check.
            verified[i] = bool(doc.get('ai_allowed')) and quote_present(doc.get('content', ''), ref.get('quote', ''))
        return verified


def write_report(path, result):
    fake = result['provider'] == 'fake'
    label = 'ТЕХНИЧЕСКИЙ ПРОГОН fake — НЕ качество AI' if fake else 'Оценка ответов модели на синтетическом наборе'
    lines = ['# Eval «Фокус»', '', label, '',
             f"Дата UTC: {result['created_at']}. Commit: `{result['commit']}`.",
             f"Provider: `{result['provider']}`; model: `{result['health']['llm']['model']}`; route: `{result['health']['llm']['route']}`.",
             f"Время сценария: `{result['health']['scenario_now']}`. Последовательный прогон; повторов нет.", '',
             'Задержка ниже — полное время HTTP-запроса (SQL + модель + проверка), не чистое время модели и не время человека.', '',
             '| Машинная операция | Успешных / попыток | Медиана, мс | p95, мс |', '|---|---:|---:|---:|']
    for kind in ('questions', 'protocols', 'agenda'):
        rows = result[kind]
        stats = latency([r['elapsed_ms'] for r in rows if not r.get('error')])
        lines.append(f"| {kind} | {stats['n']}/{len(rows)} | {stats['median_ms']} | {stats['p95_ms']} |")
    questions = result['questions']
    good = [r for r in questions if not r.get('error')]
    checks = Counter(k for r in good for k, v in r['checks'].items() if v)
    totals = Counter(k for r in questions for k in r.get('checks', {}))
    lines += ['', '## Вопросы — наблюдаемые проверки'+(' заглушки' if fake else ''), '',
              f"HTTP/JSON: {ratio(len(good), len(questions))}; все проверки кейса: {ratio(sum(all(r['checks'].values()) for r in good), len(questions))}.",
              f"Дословные доступные цитаты: {ratio(sum(r.get('verified_citations', 0) for r in questions), sum(r.get('citation_count', 0) for r in questions))}. Кейсы без цитат не добавляются к этому знаменателю.", '',
              '| Проверка | Прошло / проверено |', '|---|---:|']
    for key in sorted(totals):
        lines.append(f'| {key} | {ratio(checks[key], totals[key])} |')
    lines += ['', '| Категория | Все проверки / кейсы |', '|---|---:|']
    for category in sorted({r['category'] for r in questions}):
        rows = [r for r in questions if r['category'] == category]
        lines.append(f"| {category} | {ratio(sum(not r.get('error') and all(r['checks'].values()) for r in rows), len(rows))} |")
    lines += ['', '## Протоколы: one-to-one по виду и номеру реплики', '',
              'Это структурный proxy, а не доказательство смысловой эквивалентности формулировок. Дубликат не увеличивает полноту. Пропуски HTTP учитываются как нулевые совпадения в знаменателе gold; точность при отсутствии предсказаний — н/д.', '',
              '| Split / текст | Задачи: полнота | Задачи: точность | Владельцы | Сроки | Статусы | Цитаты | Предложение → решение |', '|---|---:|---:|---:|---:|---:|---:|---:|']
    for row in result['protocols']:
        s = row['score']
        lines.append(f"| {row['split']} / {row['transcript']} | {ratio(s['matched_tasks'], s['gold_tasks'])} | {ratio(s['matched_tasks'], s['actual_tasks'])} | {ratio(s['owners_correct'], s['matched_tasks'])} | {ratio(s['due_correct'], s['matched_tasks'])} | {ratio(s['status_correct'], s['status_total'])} | {ratio(s['quotes_verified'], s['quotes_total'])} | {ratio(s['proposal_as_decision'], s['gold_proposals'])} |")
    for split in sorted({r['split'] for r in result['protocols']}):
        scores = [r['score'] for r in result['protocols'] if r['split'] == split]
        total = Counter()
        for s in scores:
            total.update({k: v for k, v in s.items() if isinstance(v, int)})
        lines += ['', f"Split `{split}`: задачи recall {ratio(total['matched_tasks'], total['gold_tasks'])}, precision {ratio(total['matched_tasks'], total['actual_tasks'])}; origin {ratio(total['origins_correct'], total['matched_tasks'])}; уточнения {ratio(total['clarification_correct'], total['matched_tasks'])}."]
    lines += ['', '## Ошибки и расхождения', '']
    for row in questions + result['protocols'] + result['agenda']:
        failed = [k for k, v in row.get('checks', {}).items() if not v]
        if row.get('error') or failed:
            lines.append(f"- `{row['id']}`: {row.get('error') or ', '.join(failed)}")
    lines += ['', '## Границы измерения', '',
              '- fake проверяет раннер и путь API; его доли выше не являются качеством Gemini/local/Ollama.',
              '- answer_contains — лексический proxy с нормализацией чисел/дат; смысл, правильность experts и достаточность основания проверяет человек.',
              '- Проверяются выданные цитаты: доступность документа, ai_allowed, дословность в полном тексте. Состав промпта, конкретный chunk, граф и полнота фильтрации всех атак этим HTTP-раннером не наблюдаются. Для ACL промпта отдельно eval/check_seed.py.',
              '- Инъекции: измерены статус/предметные контрольные фрагменты и доступные цитаты в трёх кейсах; стойкость ко всем инъекциям не заявляется. Проверок внешних побочных действий нет.',
              '- Протоколы создают только расшифровки/черновики и аудит; actions не подтверждаются, письма не отправляются. Использовать отдельный стенд, затем reset.',
              '- Holdout выключен по умолчанию; при --include-holdout оценивается отдельно, не используется для настройки. Ошибки нельзя исключать из знаменателя.',
              '- Время человека, экономия труда и качество реальной модели при запуске fake не измерены.', '']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', required=True, choices=['fake', 'ollama', 'gemini', 'openai', 'anthropic', 'local'])
    parser.add_argument('--base-url', default=os.getenv('EVAL_BASE_URL', 'http://127.0.0.1:8000'))
    parser.add_argument('--cases-dir', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--data-dir', type=Path, default=Path(os.getenv('DATA_DIR', str(Path(__file__).resolve().parents[1]/'data'))))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--include-holdout', action='store_true')
    parser.add_argument('--drafts-ok', action='store_true', help='Required: acknowledge writes of transcripts/drafts on isolated evaluation API')
    parser.add_argument('--strict', action='store_true', help='Also fail on lexical/status expectation mismatches')
    parser.add_argument('--timeout', type=float, default=240)
    args = parser.parse_args(argv)
    if not args.drafts_ok:
        parser.error('Use an isolated evaluation API and --drafts-ok; transcripts/drafts will be stored.')
    password = os.getenv('DEMO_PASSWORD', 'fokus-demo')
    if args.provider == 'gemini' and not os.getenv('GEMINI_API_KEY'):
        # A remote API can have a key unavailable to this process. /health is authoritative.
        print('Gemini: проверяю готовность настроенного API; ключ локально не читается и не печатается.')
    output = args.output or args.cases_dir/'report.md'
    with httpx.Client(base_url=args.base_url.rstrip('/'), timeout=args.timeout, trust_env=False) as client:
        runner = Runner(client)
        health, _ = runner.request('GET', '/health')
        if health['llm']['provider'] != args.provider:
            raise ValueError('Provider API не совпадает с --provider; настройте сервер явно, fallback запрещён')
        if not health['db'] or not health['llm']['reachable']:
            raise ValueError('БД/модель не готовы: '+health['llm'].get('detail', ''))
        for login in ('director', 'morozov'):
            auth, _ = runner.request('POST', '/auth/login', json={'login': login, 'password': password})
            runner.tokens[login] = auth['access_token']
        try:
            commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.cases_dir, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.SubprocessError):
            commit = 'unknown (git unavailable)'
        result = {'provider': args.provider, 'health': health, 'commit': commit,
                  'created_at': datetime.now(timezone.utc).isoformat(), 'questions': [], 'protocols': [], 'agenda': []}
        cases = [json.loads(line) for line in (args.cases_dir/'questions.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        for case in cases:
            row = {'id': case['id'], 'category': case.get('category', 'base'), 'expected': case['expect']}
            login = 'director' if case['role'] == 'director' else 'morozov'
            try:
                answer, elapsed = runner.request('POST', '/assistant/ask', login, json={'question': case['question']})
                verified = runner.citations(answer, login)
                row.update(answer=answer, elapsed_ms=elapsed, checks=score_question(case, answer, verified),
                           citation_count=len(verified), verified_citations=sum(verified.values()))
                row['checks']['configured_route'] = answer.get('route') == health['llm']['route']
                row['checks']['configured_model'] = answer.get('model') == health['llm']['model']
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                row['error'] = type(exc).__name__ + (f' HTTP {exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError) else '')
                # Failed observations remain in case-based denominators. This
                # means verification failed, not that a leak was observed.
                row['checks'] = {k: False for k in score_question(case, {}, {})}
                row['checks'].update(configured_route=False, configured_model=False)
            result['questions'].append(row)
            print(f"{case['id']}: {'error' if row.get('error') else answer['status']}", flush=True)
        for path in sorted((args.cases_dir/'gold').glob('*.json')):
            gold = json.loads(path.read_text(encoding='utf-8'))
            if gold['split'] == 'holdout' and not args.include_holdout:
                continue
            row = {'id': path.stem, 'transcript': gold['transcript'], 'split': gold['split']}
            try:
                draft, elapsed = runner.request('POST', '/meetings/'+quote(gold['meeting_id'], safe='')+'/transcript', 'morozov',
                                                json={'text': (args.data_dir/'transcripts'/gold['transcript']).read_text(encoding='utf-8')})
                row.update(draft=draft, elapsed_ms=elapsed, score=score_protocol(gold, draft))
                row['checks'] = {'configured_route': draft.get('route') == health['llm']['route'],
                                 'configured_model': draft.get('model') == health['llm']['model']}
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                row['error'] = type(exc).__name__ + (f' HTTP {exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError) else '')
                row['score'] = score_protocol(gold, {'items': [], 'segments': []})
            result['protocols'].append(row)
            print(f"{path.stem} ({gold['split']}): {'error' if row.get('error') else 'draft OK'}", flush=True)
        row = {'id': 'agenda-A'}
        try:
            agenda, elapsed = runner.request('POST', '/meetings/prepare', 'morozov', json={
                'topic': 'Брак фотокниг и сертификаты БелЛист',
                'participant_ids': ['e-morozov', 'e-kozlova', 'e-petrova', 'e-volkov'], 'previous_meeting_id': 'm-quality-1109'})
            row.update(elapsed_ms=elapsed, agenda=agenda)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            row['error'] = type(exc).__name__
        result['agenda'].append(row)
        write_report(output, result)
        raw = output.parent/'runs'/f"{args.provider}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Report: {output}; raw (gitignored): {raw}')
        transport_error = any(r.get('error') for key in ('questions', 'protocols', 'agenda') for r in result[key])
        security_error = any(not r.get('checks', {}).get(k, True) for r in result['questions']+result['protocols']
                             for k in ('no_forbidden_source', 'no_forbidden_value', 'all_citations_verified', 'facts_have_sources', 'route_visible', 'configured_route', 'configured_model'))
        mismatch = any(not all(r.get('checks', {}).values()) for r in result['questions'])
        mismatch = mismatch or any(s['matched_items'] != s['gold_items'] or s['matched_items'] != s['actual_items'] or
                                  s['owners_correct'] != s['matched_tasks'] or s['due_correct'] != s['matched_tasks'] or
                                  s['status_correct'] != s['status_total'] or s['proposal_as_decision'] > 0
                                  for s in (r['score'] for r in result['protocols']))
        return int(transport_error or security_error or (args.strict and mismatch))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # No response bodies, URLs with credentials, or authorization values.
        print(f'Eval не начат/прерван: {type(exc).__name__}: '+(str(exc) if isinstance(exc, ValueError) else 'API unavailable'), file=sys.stderr)
        raise SystemExit(2)
