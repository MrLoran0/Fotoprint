"""Runner scoring checks; no API, database, or model is called."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fokus_eval', Path(__file__).with_name('run.py'))
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


class ScoringTests(unittest.TestCase):
    def test_number_and_date_formats(self):
        for text, needle in [('4.1%', '4,1'), ('12\u00a0500 BYN', '12500'),
                             ('2026-11-13T18:00:00+03:00', '13.11'), ('ТК-20-01', 'тк-20-01')]:
            self.assertTrue(run.contains(text, needle))

    def test_empty_denominator_is_not_perfect(self):
        self.assertEqual(run.ratio(0, 0), '0/0 (н/д)')
        self.assertIn('0.0%', run.ratio(0, 4))

    def test_citation_check_preserves_numbers_and_dates(self):
        self.assertFalse(run.quote_present('Брак 4,1%', 'Брак 41%'))
        self.assertFalse(run.quote_present('Срок 2026-11-13', 'Срок 13.11.2026'))
        self.assertTrue(run.quote_present('БРАК\n  4,1%', 'брак 4,1%'))

    def test_p95_uses_nearest_rank(self):
        self.assertEqual(run.latency(range(1, 21))['p95_ms'], 19)
        self.assertIsNone(run.latency([])['median_ms'])

    def test_duplicate_predictions_do_not_inflate_recall(self):
        expected = [{'kind': 'task', 'segment_id': 1, 'statement': 'запросить сертификат'}]
        actual = expected * 2
        pairs = run.match_items(expected, actual)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(len({g for g, _ in pairs}), 1)
        self.assertEqual(len({a for _, a in pairs}), 1)

    def test_same_segment_wrong_kind_does_not_match(self):
        expected = [{'kind': 'proposal', 'segment_id': 1}]
        self.assertEqual(run.match_items(expected, [{'kind': 'decision', 'segment_id': 1}]), [])

    def test_matching_uses_each_gold_once(self):
        expected = [{'kind': 'task', 'segment_id': 2, 'statement': 'запросить сертификат'},
                    {'kind': 'task', 'segment_id': 2, 'statement': 'сравнить бумаги'}]
        actual = [expected[1], expected[0], expected[0]]
        pairs = run.match_items(expected, actual)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(set(pairs), {(0, 1), (1, 0)})

    def answer(self):
        return {'status': 'answered', 'answer': '4.1%', 'claims': [{'text': '4.1%', 'sources': [
            {'source_id': 'quality', 'quote': '4.1%'}]}], 'model': 'unit-only', 'route': 'local'}

    def test_expected_source_must_be_verified(self):
        case = {'expect': 'answered', 'answer_contains': ['4,1'], 'sources': ['quality']}
        checks = run.score_question(case, self.answer(), {0: False})
        self.assertFalse(checks['expected_source'])
        self.assertFalse(checks['all_citations_verified'])

    def test_source_and_forbidden_value_in_expert_reason(self):
        answer = self.answer()
        answer['experts'] = [{'employee_id': 'e-1', 'reason': 'Закрыто: Кедр-14'}]
        case = {'expect': 'answered', 'forbidden_sources': ['quality'], 'forbidden_answer_contains': ['Кедр-14']}
        checks = run.score_question(case, answer, {0: True})
        self.assertFalse(checks['no_forbidden_source'])
        self.assertFalse(checks['no_forbidden_value'])

    def test_fact_with_no_source_fails(self):
        answer = self.answer()
        answer['claims'][0]['sources'] = []
        self.assertFalse(run.score_question({'expect': 'answered'}, answer, {})['facts_have_sources'])

    def test_partial_requires_gap_and_expert(self):
        case = {'expect': 'partial', 'gaps_contains': ['поступ'], 'experts_any': ['e-petrova']}
        answer = self.answer()
        answer.update(status='partial', gaps=['нет данных о поступлении'],
                      experts=[{'employee_id': 'e-petrova', 'reason': 'закупки'}])
        self.assertTrue(all(run.score_question(case, answer, {0: True}).values()))
        answer['gaps'] = []
        self.assertFalse(run.score_question(case, answer, {0: True})['gaps'])

    def test_missing_task_fields_and_quote_are_independent(self):
        gold = {'items': [{'kind': 'task', 'segment_id': 1, 'owner': None, 'due': None,
                           'owner_origin': 'unknown', 'due_origin': 'unknown',
                           'needs_clarification': ['owner_employee_id', 'due_at']}]}
        draft = {'segments': [{'n': 1, 'text': 'Нужно запросить сертификат'}], 'items': [
            {'kind': 'task', 'segment_id': 1, 'quote_verified': True, 'evidence_quote': 'несуществующая цитата',
             'owner_employee_id': None, 'due_at': None, 'owner_origin': 'unknown', 'due_origin': 'unknown',
             'needs_clarification': ['owner_employee_id', 'due_at']}]}
        score = run.score_protocol(gold, draft)
        self.assertEqual(score['owners_correct'], 1)
        self.assertEqual(score['due_correct'], 1)
        self.assertEqual(score['quotes_verified'], 0)

    def test_failed_protocol_keeps_gold_denominator(self):
        gold = {'items': [{'kind': 'task', 'segment_id': 1}, {'kind': 'proposal', 'segment_id': 2}]}
        score = run.score_protocol(gold, {'items': [], 'segments': []})
        self.assertEqual((score['matched_tasks'], score['gold_tasks'], score['actual_tasks']), (0, 1, 0))
        self.assertEqual(score['missing_gold'], [0, 1])

    def test_deferred_proposal_cannot_become_a_decision(self):
        gold = {'items': [{'kind': 'proposal', 'segment_id': 1, 'context_segment_ids': [2]}]}
        score = run.score_protocol(gold, {'items': [{'kind': 'decision', 'segment_id': 2, 'decision_status': 'decided'}]})
        self.assertEqual(score['proposal_as_decision'], 1)

    def test_provider_mismatch_stops_before_login_or_mutation(self):
        paths = []
        def respond(request):
            paths.append((request.method, request.url.path))
            return run.httpx.Response(200, json={'llm': {'provider': 'fake'}})
        client = run.httpx.Client(base_url='http://eval.test', transport=run.httpx.MockTransport(respond))
        with patch.object(run.httpx, 'Client', return_value=client):
            with self.assertRaisesRegex(ValueError, 'Provider API'):
                run.main(['--provider', 'gemini', '--drafts-ok'])
        self.assertEqual(paths, [('GET', '/health')])


if __name__ == '__main__':
    unittest.main()
