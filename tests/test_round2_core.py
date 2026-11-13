"""Tests for the ground radius, coverage ledger, consolidate retry and mechanical mode (core: scripts/oneiros.py). No network, no model calls."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
sys.path.insert(0, HERE)
os.environ.setdefault('ONEIROS_CONFIG', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'no-such-config.json'))  # tests never read a local config
os.environ.setdefault('ONEIROS_PROTECTED', 'profile.md,notes.md')  # the fixtures' append-only files
import oneiros as O  # noqa: E402
from test_oneiros import SID, make_memory  # noqa: E402

TODAY = '2026-11-11'
DASH_EM, DASH_EN = chr(0x2014), chr(0x2013)


def render(lines, sid=SID):
    return '# session %s | t | 2026-11-03T09:00:00.000Z to 2026-11-03T10:00:00.000Z\n%s\n' % (sid, '\n'.join(lines))


def filler(n, prefix='< '):
    return [prefix + 'filler line number%d here' % i for i in range(n)]


class TestGroundRadius(unittest.TestCase):
    def verify(self, lines, statement, role='user', quote='the quote line'):
        kept, dropped = O.verify_candidates(render(lines), [
            {'quote': quote, 'role': role, 'statement': statement, 'date': ''}], SID)
        return kept, dropped

    def test_constant(self):
        self.assertEqual(O.GROUND_RADIUS, 4)

    def test_number_four_lines_after_is_kept(self):
        kept, _ = self.verify(['> the quote line'] + filler(3) + ['< Archive folder holds item 4711 now'], 'Item is 4711.')
        self.assertEqual(len(kept), 1)

    def test_number_four_lines_before_is_kept(self):
        kept, _ = self.verify(['< Archive folder holds item 4711 now'] + filler(3) + ['> the quote line'], 'Item is 4711.')
        self.assertEqual(len(kept), 1)

    def test_number_five_lines_away_is_dropped(self):
        for lines in (['> the quote line'] + filler(4) + ['< Archive folder holds item 4711 now'],
                      ['< Archive folder holds item 4711 now'] + filler(4) + ['> the quote line']):
            kept, dropped = self.verify(lines, 'Item is 4711.')
            self.assertEqual(kept, [])
            self.assertIn('4711', dropped[0]['why'])

    def test_tool_lines_count_as_render_lines(self):
        kept, _ = self.verify(['> the quote line', '. Bash: read Archive item 41 back'], 'Archive item 41.')
        self.assertEqual(len(kept), 1)

    def test_other_session_text_does_not_ground(self):
        # the header line is not a render line and a number only in another session's render means nothing here
        kept, _ = self.verify(['> the quote line'], 'Budget is 5,000.')
        self.assertEqual(kept, [])

    def test_pasted_span_ignored_for_a_user_candidate(self):
        lines = ['> the quote line', '> before [pasted: the plan costs EUR 61,500 in total ...] after']
        kept, dropped = self.verify(lines, 'Plan costs 61,500.')
        self.assertEqual(kept, [])
        self.assertIn('61500', dropped[0]['why'].replace(',', ''))

    def test_pasted_span_on_the_quote_line_is_ignored_for_user(self):
        lines = ['> the quote line [pasted: the plan costs EUR 61,500 in total ...] tail']
        kept, _ = self.verify(lines, 'Plan costs 61,500.')
        self.assertEqual(kept, [])

    def test_pasted_span_counts_for_a_pasted_candidate(self):
        lines = ['> intro [pasted: the quote line and EUR 61,500 ...] tail', '> second [pasted: grant 4812 ...] x']
        kept, _ = self.verify(lines, 'Plan costs 61,500 and grant 4812.', role='pasted')
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['role'], 'pasted')

    def test_user_text_next_to_a_paste_still_grounds(self):
        lines = ['> the quote line', '> I said 4812 [pasted: other 99 ...] ok']
        kept, _ = self.verify(lines, 'It is 4812.')
        self.assertEqual(len(kept), 1)
        kept, _ = self.verify(lines, 'It is 99.')
        self.assertEqual(kept, [])

    def test_dates_inside_the_session_stay_allowed(self):
        kept, _ = self.verify(['> the quote line'], 'Agreed on 3 Nov 2026.')
        self.assertEqual(len(kept), 1)

    def test_quote_line_numbers_still_ground(self):
        kept, _ = self.verify(['> the quote line has 12 items'], 'There are 12 items.', quote='the quote line has 12 items')
        self.assertEqual(len(kept), 1)


STAGE_FILES = {
    'MEMORY.md': '# Memory Index\n\n- [ZEPHYR](zephyr.md) hook\n- [Trip](trip.md) hook\n',
    'zephyr.md': '---\nname: p\ndescription: d\nmetadata:\n  type: project\n---\n\n'
                  'Budget is EUR 7,654,321 for the ZEPHYR bid.   Reference ID 100000001 was filed\non 20 Feb 2027.\n',
    'trip.md': '---\nname: t\ndescription: d\nmetadata:\n  type: project\n---\n\nThe Sorrento trip leaves on 9 Apr.\n',
}


def cand(n, statement, target='zephyr.md', role='user', session=SID, **kw):
    d = {'id': 'c%s-%d' % (session[:8], n), 'session': session, 'role': role, 'statement': statement,
         'quote': statement, 'date': '2026-11-03', 'kind': 'decision', 'durable_because': 'x', 'target': target}
    d.update(kw)
    return d


class StageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')
        self.stage = os.path.join(self.tmp, 'stage')
        os.makedirs(self.stage)
        for name, text in STAGE_FILES.items():
            with open(os.path.join(self.stage, name), 'w', encoding='utf-8') as f:
                f.write(text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCoverage(StageCase):
    def cov(self, cands, ops=(), disp=()):
        return O.coverage(cands, list(ops), list(disp), self.stage)

    def test_constants_and_schema(self):
        self.assertEqual(O.MAX_OPS, 40)  # the frozen 41-op gate test keeps the cap at 40
        item = O.OPS_SCHEMA['properties']['dispositions']['items']['properties']
        self.assertEqual(set(item['status']['enum']), {'op', 'present', 'skip'})
        self.assertEqual(set(item['reason']['enum']), {'one-off', 'not durable', 'contradicts CLAUDE.md', 'duplicate of another candidate'})
        self.assertIn('cand', item)
        self.assertIn('evidence', item)

    def test_op_only_when_an_op_cites_the_id(self):
        c = [cand(1, 'Budget is EUR 7,654,321.'), cand(2, 'Other fact 7.')]
        ops = [{'op': 'append', 'file': 'zephyr.md', 'text': 'x', 'cands': ['c11111111-1']}]
        disp = [{'cand': 'c11111111-1', 'status': 'op'}, {'cand': 'c11111111-2', 'status': 'op'}]
        self.assertEqual(self.cov(c, ops, disp), {'c11111111-1': 'op', 'c11111111-2': 'uncovered'})

    def test_op_that_does_not_cite_is_not_coverage(self):
        c = [cand(1, 'Budget is EUR 7,654,321.')]
        ops = [{'op': 'append', 'file': 'zephyr.md', 'text': 'Budget is EUR 7,654,321.'},
               {'op': 'append', 'file': 'zephyr.md', 'text': 'y', 'cands': 'c11111111-1'},
               'junk', {'op': 'append', 'cands': [3, None]}]
        self.assertEqual(self.cov(c, ops), {'c11111111-1': 'uncovered'})

    def test_present_with_real_evidence(self):
        c = [cand(1, 'Reference ID is 100000001.')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'Reference ID 100000001 was filed on 20 Feb 2027'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'present')  # whitespace and a line break are squashed

    def test_present_fake_evidence(self):
        c = [cand(1, 'Reference ID is 100000001.')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'Reference ID 999999999 was filed on 20 Feb 2027'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'uncovered')

    def test_present_evidence_too_short(self):
        c = [cand(1, 'Reference ID is 100000001.')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'ID 100000001'}]
        self.assertEqual(len('ID 100000001'), 12)
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'present')
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'D 100000001'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'uncovered')

    def test_present_evidence_without_the_key_token(self):
        c = [cand(1, 'Reference ID is 100000001.')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'was filed on'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'uncovered')
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'for the bid.   Reference ID'}]  # in the file, no key
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'uncovered')

    def test_present_key_may_be_an_uppercase_name(self):
        c = [cand(1, 'ZEPHYR is the bid name.')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'for the ZEPHYR bid'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'present')

    def test_present_candidate_without_key_tokens_needs_only_the_snippet(self):
        c = [cand(1, 'the budget was filed')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'was filed\non 20 Feb'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'present')

    def test_present_must_come_from_the_target_file(self):
        c = [cand(1, 'Reference ID is 100000001.', target='trip.md')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'Reference ID 100000001 was filed on 20 Feb 2027'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'uncovered')

    def test_present_for_a_new_target_may_come_from_any_topic_file(self):
        c = [cand(1, 'Reference ID is 100000001.', target='new')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'Reference ID 100000001 was filed on 20 Feb 2027'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'present')
        c = [cand(1, 'Reference ID is 100000001.', target='missing.md')]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'present')
        # MEMORY.md is not a topic file
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': '- [ZEPHYR](zephyr.md) hook'}]
        c = [cand(1, 'ZEPHYR hook.', target='new')]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'uncovered')

    def test_skip_needs_an_allowed_reason(self):
        c = [cand(i, 'Fact %d.' % i) for i in range(1, 7)]
        d = [{'cand': 'c11111111-1', 'status': 'skip', 'reason': 'one-off'},
             {'cand': 'c11111111-2', 'status': 'skip', 'reason': 'not durable'},
             {'cand': 'c11111111-3', 'status': 'skip', 'reason': 'contradicts CLAUDE.md'},
             {'cand': 'c11111111-4', 'status': 'skip', 'reason': 'duplicate of another candidate'},
             {'cand': 'c11111111-5', 'status': 'skip', 'reason': 'because I felt like it'},
             {'cand': 'c11111111-6', 'status': 'skip'}]
        got = self.cov(c, disp=d)
        self.assertEqual([got['c11111111-%d' % i] for i in range(1, 7)], ['skip'] * 4 + ['uncovered'] * 2)

    def test_garbage_dispositions_are_ignored(self):
        c = [cand(1, 'Fact 1.')]
        self.assertEqual(self.cov(c, disp=['x', {'status': 'skip', 'reason': 'one-off'}, {'cand': 5}]),
                         {'c11111111-1': 'uncovered'})
        self.assertEqual(O.coverage(c, None, 'nope', self.stage), {'c11111111-1': 'uncovered'})

    def test_a_valid_later_disposition_beats_an_invalid_earlier_one(self):
        c = [cand(1, 'Fact 1.')]
        d = [{'cand': 'c11111111-1', 'status': 'present', 'evidence': 'invented text that is not there'},
             {'cand': 'c11111111-1', 'status': 'skip', 'reason': 'duplicate of another candidate'}]
        self.assertEqual(self.cov(c, disp=d)['c11111111-1'], 'skip')


class TestEvidenceCase(StageCase):
    def test_lower_case_evidence_matches_an_upper_case_name(self):
        c = cand(1, 'The ZEPHYR bid is filed.')
        ev = 'for the ZEPHYR bid'
        self.assertTrue(O._evidence_ok(c, ev, self.stage))
        self.stage_lower = os.path.join(self.tmp, 'lower')
        os.makedirs(self.stage_lower)
        with open(os.path.join(self.stage_lower, 'zephyr.md'), 'w') as f:
            f.write('---\nname: p\n---\n\nWe have submitted both zephyr and borealis in spring.\n')
        c2 = dict(c, statement='ZEPHYR and BOREALIS were submitted.')
        self.assertTrue(O._evidence_ok(c2, 'submitted both zephyr and borealis', self.stage_lower))

    def test_evidence_still_needs_a_key_token(self):
        c = cand(1, 'The clip is 45 s long.')
        self.assertFalse(O._evidence_ok(c, 'The Sorrento trip leaves on 9 Apr.', self.stage))
        c2 = cand(2, 'Beneficiaries are 10 now.')
        self.assertFalse(O._evidence_ok(c2, 'Budget is EUR 7,654,321 for the ZEPHYR bid.', self.stage))


class TestApplyRepeats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')
        self.mem = make_memory(os.path.join(self.tmp, 'mem'))
        self.c = cand(1, 'Zeta note appears here.')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def op(self, text='Zeta note appears here.', **kw):
        return dict({'op': 'append', 'file': 'zephyr.md', 'text': text, 'sources': [SID], 'cands': [self.c['id']]}, **kw)

    def test_identical_op_twice_is_applied_once(self):
        applied, rejected = O.apply_ops(self.mem, [self.op(), self.op()], {SID}, TODAY, [self.c])
        self.assertEqual((len(applied), len(rejected)), (1, 1))
        self.assertIn('duplicate', rejected[0]['reason'])
        self.assertEqual(O.read(os.path.join(self.mem, 'zephyr.md')).count('Zeta note appears here.'), 1)

    def test_same_text_in_another_op_is_not_appended_again(self):
        applied, rejected = O.apply_ops(self.mem, [self.op(), self.op(sources=[SID, 'zzzzzzzz'])], {SID, 'zzzzzzzz'},
                                        TODAY, [self.c])
        self.assertEqual((len(applied), len(rejected)), (1, 1))
        self.assertEqual(rejected[0]['reason'], 'text already in the file')

    def test_text_already_in_the_file_is_not_appended(self):
        with open(os.path.join(self.mem, 'zephyr.md'), 'a', encoding='utf-8') as f:
            f.write('Budget EUR 6,400,000 (r4).\n')
        c = cand(2, 'Budget EUR 6,400,000 (r4).')
        applied, rejected = O.apply_ops(self.mem, [self.op('Budget EUR 6,400,000  (r4).', cands=[c['id']])], {SID}, TODAY, [c])
        self.assertEqual((applied, rejected[0]['reason']), ([], 'text already in the file'))

    def test_conflict_text_is_not_repeated(self):
        op = dict(self.op(), op='conflict')
        applied, rejected = O.apply_ops(self.mem, [op, dict(op, sources=[SID, 'zzzzzzzz'])], {SID, 'zzzzzzzz'}, TODAY, [self.c])
        self.assertEqual((len(applied), len(rejected)), (1, 1))


class TestGateCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')
        self.before = make_memory(os.path.join(self.tmp, 'before'))
        self.mem = os.path.join(self.tmp, 'stage')
        shutil.copytree(self.before, self.mem)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, n):
        return [{'op': 'append', 'file': 'zephyr.md', 'text': 'fact %d' % i} for i in range(n)]

    def test_sixty_distinct_ops_pass_sixty_one_are_withheld(self):
        self.assertEqual(O.gate(self.before, self.mem, self.fake(O.MAX_OPS), [])[0], 0)
        code, problems, _ = O.gate(self.before, self.mem, self.fake(O.MAX_OPS + 1), [])
        self.assertEqual(code, 2)
        self.assertIn('cap %d' % O.MAX_OPS, problems[0])

    def test_identical_applied_operations_are_withheld(self):
        op = {'op': 'append', 'file': 'zephyr.md'}
        code, problems, _ = O.gate(self.before, self.mem, [op] * 2, [])
        self.assertEqual(code, 2)
        self.assertIn('identical operations', problems[0])
        self.assertEqual(O.gate(self.before, self.mem, [op], [])[0], 0)

    def test_ops_cut_by_the_cap_warn(self):
        rej = [{'op': {'op': 'append'}, 'reason': 'over the %d operation cap' % O.MAX_OPS}] * 3
        code, problems, warnings = O.gate(self.before, self.mem, [], rej)
        self.assertEqual((code, problems), (1, []))
        self.assertIn('3 operation(s) cut', warnings[0])


class TestConsolidateRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')
        self.d = os.path.join(self.tmp, 'runs', 'R1')
        make_memory(os.path.join(self.d, 'before'))
        shutil.copytree(os.path.join(self.d, 'before'), os.path.join(self.d, 'stage'))
        os.makedirs(os.path.join(self.d, 'candidates'))
        base = {'quote': 'xxxxxxxxxx', 'role': 'user', 'session': SID, 'target': 'zephyr.md', 'kind': 'decision',
                'durable_because': 'x'}
        O.save(os.path.join(self.d, 'candidates', SID + '.json'), {'kept': [
            dict(base, statement='Budget is EUR 6,400,000 now.'), dict(base, statement='Beneficiaries are 9.'),
            dict(base, statement='Submitted on 20 Feb 2027.'), dict(base, statement='A chat remark.')], 'dropped': []})
        O.save(os.path.join(self.d, 'run.json'), {'id': 'R1', 'sessions': [SID], 'renders': [{'sid': SID, 'title': 't'}],
                                                  'eval': True})
        self.patch = mock.patch.object(O, 'RUNS', os.path.join(self.tmp, 'runs'))
        self.patch.start()
        self.calls = []

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ns(self):
        return argparse.Namespace(run='R1', model='sonnet', effort='medium', budget=1.0, workers=1, today=TODAY)

    def fake(self, outs):
        def claude_call(prompt, schema, cwd=None, **kw):
            self.calls.append((prompt, dict(kw, uncovered=self.read_uncovered(kw.get('inputs') or {}))))
            out = outs.pop(0)
            return out, {'tokens': 10, 'cost_usd': 0.5, 'denials': []}
        return claude_call

    def read_uncovered(self, inputs):
        p = inputs.get('uncovered.json')
        return O.load(p, None) if p else None

    def op(self, cid, text):
        return {'op': 'append', 'file': 'zephyr.md', 'text': text, 'sources': [SID], 'cands': [cid]}

    def run_consolidate(self, outs):
        with mock.patch.object(O, 'claude_call', self.fake(outs)), mock.patch('builtins.print'):
            return O.cmd_consolidate(self.ns())

    def test_retry_merges_ops_and_dispositions(self):
        first = {'ops': [self.op('c11111111-1', 'Budget is EUR 6,400,000 now.')], 'flags': ['f1'], 'summary': 'first',
                 'dispositions': [{'cand': 'c11111111-1', 'status': 'op'},
                                  {'cand': 'c11111111-2', 'status': 'present', 'evidence': 'made up quote that is absent'}]}
        second = {'ops': [self.op('c11111111-2', 'Beneficiaries are 9.')], 'flags': ['f2'], 'summary': 'second',
                  'dispositions': [{'cand': 'c11111111-2', 'status': 'op'},
                                   {'cand': 'c11111111-3', 'status': 'skip', 'reason': 'one-off'}]}
        self.assertEqual(self.run_consolidate([first, second]), 0)
        self.assertEqual(len(self.calls), 2)
        # the retry gets the same inputs plus uncovered.json, which lists exactly the uncovered candidates
        self.assertEqual(set(self.calls[0][1]['inputs']), {'stage', 'candidates.json'})
        self.assertEqual(set(self.calls[1][1]['inputs']), {'stage', 'candidates.json', 'uncovered.json'})
        self.assertEqual([c['id'] for c in self.calls[1][1]['uncovered']],
                         ['c11111111-2', 'c11111111-3', 'c11111111-4'])
        self.assertIn('uncovered.json', self.calls[1][0])
        ops = O.load(os.path.join(self.d, 'ops.json'), {})
        self.assertEqual([o['cands'] for o in ops['ops']], [['c11111111-1'], ['c11111111-2']])
        self.assertEqual(ops['flags'], ['f1', 'f2'])
        self.assertEqual(len(ops['dispositions']), 4)
        cov = O.load(os.path.join(self.d, 'coverage.json'), {})
        self.assertEqual(cov['counts'], {'op': 2, 'present': 0, 'skip': 1, 'uncovered': 1})
        self.assertEqual(cov['uncovered'], [{'id': 'c11111111-4', 'statement': 'A chat remark.'}])
        self.assertTrue(cov['retried'])
        log = O.load(os.path.join(self.d, 'calls.json'), [])
        self.assertEqual([c['step'] for c in log], ['consolidate', 'consolidate-retry'])

    def test_no_retry_when_everything_is_covered(self):
        first = {'ops': [self.op('c11111111-1', 'Budget is EUR 6,400,000 now.')], 'flags': [], 'summary': 's',
                 'dispositions': [{'cand': 'c11111111-%d' % i, 'status': 'skip', 'reason': 'duplicate of another candidate'} for i in (2, 3, 4)]}
        self.assertEqual(self.run_consolidate([first]), 0)
        self.assertEqual(len(self.calls), 1)
        cov = O.load(os.path.join(self.d, 'coverage.json'), {})
        self.assertEqual((cov['counts']['uncovered'], cov['retried']), (0, False))
        self.assertFalse(os.path.exists(os.path.join(self.d, 'uncovered.json')))

    def test_a_failed_retry_keeps_the_first_result_and_flags_it(self):
        first = {'ops': [], 'flags': [], 'summary': 's', 'dispositions': []}
        self.assertEqual(self.run_consolidate([first, None]), 0)
        ops = O.load(os.path.join(self.d, 'ops.json'), {})
        self.assertTrue(any('retry failed' in f for f in ops['flags']))
        self.assertEqual(O.load(os.path.join(self.d, 'coverage.json'), {})['counts']['uncovered'], 4)

    def test_retry_ops_are_cut_to_what_is_left_and_deduplicated(self):
        many = [self.op('c11111111-1', 'Alpha fact %s here.' % w) for w in ('one', 'two', 'three', 'four', 'five')]
        many += [self.op('c11111111-1', 'Alpha fact six here.')] * (O.MAX_OPS - 6)  # MAX_OPS - 1 ops in all
        first = {'ops': many, 'flags': [], 'summary': 's', 'dispositions': []}
        repeat = many[0]  # an op the first call already made
        second = {'ops': [repeat] + [self.op('c11111111-2', 'Beta fact %s here.' % w) for w in ('aa', 'bb', 'cc')],
                  'flags': [], 'summary': 's', 'dispositions': []}
        self.assertEqual(self.run_consolidate([first, second]), 0)
        ops = O.load(os.path.join(self.d, 'ops.json'), {})
        self.assertEqual(len(ops['ops']), O.MAX_OPS)  # fills the cap; the repeat and two more were dropped
        self.assertEqual(ops['ops'][-1]['text'], 'Beta fact aa here.')
        self.assertEqual(sum(1 for o in ops['ops'] if o is repeat or o == repeat), 1)
        self.assertIn('at most 1 operations', self.calls[1][0])
        self.assertTrue(any('cut to the 1 left' in f for f in ops['flags']))

    def test_apply_cap_and_coverage_of_ops_that_were_not_applied(self):
        # 62 distinct appends cite c1; the last two are over the cap. One op on a missing file cites c2.
        ops = [self.op('c11111111-1', 'Gamma note %s here.' % (chr(97 + i // 26) + chr(97 + i % 26))) for i in range(O.MAX_OPS - 1)]
        ops.append(dict(self.op('c11111111-2', 'Delta note here.'), file='nonexistent.md'))
        ops.append(self.op('c11111111-3', 'Epsilon note here.'))  # position 61: over the cap
        O.save(os.path.join(self.d, 'ops.json'), {'ops': ops})
        kept = O.load(os.path.join(self.d, 'candidates', SID + '.json'), {})['kept']
        O.save(os.path.join(self.d, 'candidates.json'), [dict(c, id='c11111111-%d' % n) for n, c in enumerate(kept, 1)])
        O.save(os.path.join(self.d, 'coverage.json'), {'status': {
            'c11111111-1': 'op', 'c11111111-2': 'op', 'c11111111-3': 'op', 'c11111111-4': 'uncovered'},
            'counts': {'op': 3, 'present': 0, 'skip': 0, 'uncovered': 1}, 'retried': False, 'uncovered': []})
        with mock.patch('builtins.print'):
            O.cmd_apply(argparse.Namespace(run='R1', today=TODAY))
            code = O.cmd_gate(argparse.Namespace(run='R1'))
            O.cmd_report(argparse.Namespace(run='R1', notify=False))
        res = O.load(os.path.join(self.d, 'applied.json'), {})
        self.assertEqual(len(res['applied']), O.MAX_OPS - 1)
        reasons = sorted(r['reason'] for r in res['rejected'])
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any('operation cap' in r for r in reasons))
        cov = O.load(os.path.join(self.d, 'coverage.json'), {})
        self.assertEqual([x['id'] for x in cov['not_applied']], ['c11111111-2', 'c11111111-3'])
        self.assertEqual(cov['counts']['op'], 3)  # the consolidate-time ledger is untouched
        self.assertEqual(code, 1)  # warning: an op was cut by the cap
        self.assertIn('cut by the %d operation cap' % O.MAX_OPS, O.load(os.path.join(self.d, 'gate.json'), {})['warnings'][0])
        text = O.read(os.path.join(self.d, 'REPORT.md'))
        self.assertIn('cited by an op that was not applied c11111111-2', text)
        self.assertIn('cited by an op that was not applied c11111111-3', text)

    def test_report_lists_coverage_and_uncovered_statements(self):
        first = {'ops': [self.op('c11111111-1', 'Budget is EUR 6,400,000 now.')], 'flags': [], 'summary': 's',
                 'dispositions': []}
        self.run_consolidate([first, {'ops': [], 'flags': [], 'summary': 's', 'dispositions': []}])
        with mock.patch('builtins.print'):
            O.cmd_apply(argparse.Namespace(run='R1', today=TODAY))
            O.cmd_gate(argparse.Namespace(run='R1'))
            O.cmd_report(argparse.Namespace(run='R1', notify=False))
        text = O.read(os.path.join(self.d, 'REPORT.md'))
        part = text.split('## Coverage')[1]
        self.assertIn('op 1, present 0, skip 0, uncovered 3', part)
        self.assertIn('uncovered c11111111-4: A chat remark.', part)
        self.assertIn('uncovered c11111111-2: Beneficiaries are 9.', part)


class TestPrompts(unittest.TestCase):
    def test_consolidate_prompt_round_two(self):
        text = O.read(os.path.join(O.PROMPTS, 'consolidate.md'))
        self.assertNotIn('Fewer', text)
        self.assertNotIn('fewer', text)
        self.assertIn('dispositions', text)
        self.assertIn('12 characters', text)
        self.assertIn('{{max_ops}}', text)
        for r in O.SKIP_REASONS:
            self.assertIn(r, text)
        self.assertNotIn(DASH_EM, text.replace(DASH_EM + ' short hook', ''))
        self.assertNotIn(DASH_EN, text)


class TestMechanical(StageCase):
    SRC = 'R1'

    def setUp(self):
        super().setUp()
        self.runs = os.path.join(self.tmp, 'runs')
        d = os.path.join(self.runs, self.SRC)
        shutil.copytree(self.stage, os.path.join(d, 'before'))
        shutil.copytree(self.stage, os.path.join(d, 'stage'))
        os.makedirs(os.path.join(d, 'renders'))
        with open(os.path.join(d, 'renders', SID + '.txt'), 'w') as f:
            f.write(render(['> hello']))
        os.makedirs(os.path.join(d, 'candidates'))
        self.cands = [
            cand(1, 'Budget is EUR 7,654,321 for the ZEPHYR bid.'),        # all tokens present: skipped
            cand(2, 'Beneficiaries are 10 after the change.'),               # 10 is new: op
            cand(3, 'Brand new fact 55.', target='new'),                     # new target: skipped
            cand(4, 'Fact for the index 66.', target='MEMORY.md'),           # index: skipped
            cand(5, 'Ghost file fact 77.', target='nofile.md'),              # no such file: skipped
            cand(6, 'Trip leaves on 11 Apr.', target='trip.md'),             # 11 new: op
        ]
        O.save(os.path.join(d, 'candidates', SID + '.json'), {'kept': self.cands, 'dropped': [{'why': 'x'}]})
        O.save(os.path.join(d, 'candidates.json'), self.cands)
        O.save(os.path.join(d, 'run.json'), {'id': self.SRC, 'sessions': [SID], 'renders': [{'sid': SID, 'title': 't'}],
                                             'memory_source': '/x', 'eval': True})
        self.patch = mock.patch.object(O, 'RUNS', self.runs)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def mech(self, rid='M1', **kw):
        with mock.patch('builtins.print'):
            return O.cmd_mechanical(argparse.Namespace(run=rid, from_run=self.SRC, today=TODAY, **kw))

    def ops(self, rid='M1'):
        return O.load(os.path.join(self.runs, rid, 'ops.json'), {})

    def test_creates_the_run_and_never_calls_the_model(self):
        with mock.patch.object(O, 'claude_call', side_effect=AssertionError('model call')):
            self.assertEqual(self.mech(), 0)
        d = os.path.join(self.runs, 'M1')
        for rel in ('before/zephyr.md', 'stage/zephyr.md', 'renders/%s.txt' % SID, 'candidates/%s.json' % SID, 'candidates.json'):
            self.assertTrue(os.path.isfile(os.path.join(d, rel)), rel)
        self.assertEqual(O.load(os.path.join(d, 'candidates.json'), []), self.cands)  # ids kept
        self.assertEqual(O.load(os.path.join(d, 'candidates', SID + '.json'), {}),
                         O.load(os.path.join(self.runs, self.SRC, 'candidates', SID + '.json'), {}))
        run = O.load(os.path.join(d, 'run.json'), {})
        self.assertEqual((run['from'], run['id'], run['sessions'], run['today']), (self.SRC, 'M1', [SID], TODAY))
        self.assertEqual(run['before_manifest'], O.manifest(os.path.join(d, 'before')))
        self.assertTrue(run['eval'])
        self.assertEqual(O.manifest(os.path.join(d, 'before')), O.manifest(os.path.join(d, 'stage')))

    def test_skips_present_facts_new_targets_index_and_missing_files(self):
        self.mech()
        ops = self.ops()
        self.assertEqual(ops['source'], 'mechanical')
        self.assertEqual([o['cands'] for o in ops['ops']], [['c11111111-2'], ['c11111111-6']])
        o = ops['ops'][0]
        self.assertEqual((o['op'], o['file'], o['text'], o['sources'], o['role']),
                         ('append', 'zephyr.md', 'Beneficiaries are 10 after the change.', [SID[:8]], 'user'))

    def test_capitalised_names_count_as_tokens(self):
        c = [cand(1, 'Tomas leads the bid.'), cand(2, 'Budget leads the bid.'), cand(3, 'The ZEPHYR bid is on.')]
        ops = O.mechanical_ops(c, self.stage)
        self.assertEqual([o['cands'] for o in ops], [['c11111111-1']])  # Tomas absent; Budget, ZEPHYR present

    def test_a_sentence_start_or_a_nationality_is_not_a_missing_name(self):
        c = [cand(1, 'Nothing was sent: reports are 41 to 44.', target='trip.md'),
             cand(2, 'Revenue is EUR 7,654,321 for Greek and German sales.')]
        with open(os.path.join(self.stage, 'trip.md'), 'a') as f:
            f.write('Reports 41 and 44 saved.\n')
        self.assertEqual(O.mechanical_ops(c, self.stage), [])

    def test_the_same_fact_twice_gives_one_op_and_a_new_number_still_does(self):
        c = [cand(1, 'Same fact 9911 for GAMMA.'), cand(2, 'Same fact 9911 for GAMMA.', session='22222222-aaaa'),
             cand(3, 'Other fact 9922 for GAMMA.')]
        ops = O.mechanical_ops(c, self.stage)
        self.assertEqual([o['cands'] for o in ops], [['c11111111-1'], ['c11111111-3']])

    def test_whitespace_case_and_thousands_separators_do_not_hide_a_fact(self):
        c = [cand(1, 'BUDGET is eur 7654321 and REFERENCE id 100,000,001.')]
        self.assertEqual(O.mechanical_ops(c, self.stage), [])

    def test_applies_through_apply_ops_and_gate(self):
        self.mech()
        with mock.patch('builtins.print'):
            self.assertEqual(O.cmd_apply(argparse.Namespace(run='M1', today=None)), 0)
            code = O.cmd_gate(argparse.Namespace(run='M1'))
            O.cmd_report(argparse.Namespace(run='M1', notify=False))
        res = O.load(os.path.join(self.runs, 'M1', 'applied.json'), {})
        self.assertEqual((len(res['applied']), len(res['rejected'])), (2, 0))
        text = O.read(os.path.join(self.runs, 'M1', 'stage', 'zephyr.md'))
        self.assertIn('Beneficiaries are 10 after the change. (dream 2026-11-11: session 11111111, user)', text)
        self.assertIn('Trip leaves on 11 Apr. (dream 2026-11-11: session 11111111, user)',
                      O.read(os.path.join(self.runs, 'M1', 'stage', 'trip.md')))
        self.assertEqual(code, 0)
        self.assertIn('mechanical append ops from R1', O.read(os.path.join(self.runs, 'M1', 'REPORT.md')))

    def test_user_and_feedback_targets_need_role_user(self):
        for name, typ in (('fbk.md', 'feedback'), ('urs.md', 'user')):
            for rel in ('before', 'stage'):
                with open(os.path.join(self.runs, self.SRC, rel, name), 'w') as f:
                    f.write('---\nname: n\ndescription: d\nmetadata:\n  type: %s\n---\n\nPlain.\n' % typ)
        cs = [cand(1, 'Rule from the assistant 31.', target='fbk.md', role='assistant'),
              cand(2, 'Rule from the pasted brief 32.', target='urs.md', role='pasted'),
              cand(3, 'Rule from the person 33.', target='fbk.md', role='user'),
              cand(4, 'Observed outcome 34.', target='zephyr.md', role='assistant')]
        O.save(os.path.join(self.runs, self.SRC, 'candidates', SID + '.json'), {'kept': cs, 'dropped': []})
        O.save(os.path.join(self.runs, self.SRC, 'candidates.json'), cs)
        self.mech()
        self.assertEqual(len(self.ops()['ops']), 4)
        with mock.patch('builtins.print'):
            O.cmd_apply(argparse.Namespace(run='M1', today=None))
        res = O.load(os.path.join(self.runs, 'M1', 'applied.json'), {})
        self.assertEqual([o['cands'] for o in res['applied']], [['c11111111-3'], ['c11111111-4']])
        self.assertEqual(len(res['rejected']), 2)
        self.assertTrue(all('role user' in r['reason'] for r in res['rejected']))

    def test_statement_with_a_dash_is_rejected_by_apply(self):
        cs = [cand(1, 'Plan has 88 steps ' + DASH_EM + ' all done.', target='zephyr.md')]
        O.save(os.path.join(self.runs, self.SRC, 'candidates', SID + '.json'), {'kept': cs, 'dropped': []})
        self.mech()
        with mock.patch('builtins.print'):
            O.cmd_apply(argparse.Namespace(run='M1', today=None))
        res = O.load(os.path.join(self.runs, 'M1', 'applied.json'), {})
        self.assertEqual((len(res['applied']), len(res['rejected'])), (0, 1))

    def test_refuses_missing_source_and_existing_target(self):
        with mock.patch('builtins.print'):
            self.assertEqual(O.cmd_mechanical(argparse.Namespace(run='M2', from_run='nope', today=None)), 2)
            self.assertEqual(self.mech(), 0)
            self.assertEqual(self.mech(), 2)  # M1 exists now
        self.assertFalse(os.path.exists(os.path.join(self.runs, 'M2')))

    def test_command_is_registered(self):
        with mock.patch.object(O, 'cmd_mechanical', return_value=0) as m:
            O.main(['mechanical', 'M9', '--from', 'R1', '--today', TODAY])
        a = m.call_args[0][0]
        self.assertEqual((a.run, a.from_run, a.today), ('M9', 'R1', TODAY))


if __name__ == '__main__':
    unittest.main()
