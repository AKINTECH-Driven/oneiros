"""Tests for the core fixes (scripts/oneiros.py). No network, no model calls."""
import argparse
import json
import os
import shutil
import subprocess
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
from test_oneiros import Base, SID, SID2, rec, write_transcript, sample_records  # noqa: E402

TODAY = '2026-11-11'


def cand(n, role='user', session=SID, statement='', quote='', date='2027-02-20'):
    return {'id': 'c%s-%d' % (session[:8], n), 'session': session, 'role': role, 'statement': statement,
            'quote': quote or statement, 'date': date, 'kind': 'decision', 'durable_because': 'x', 'target': 'new'}


class TestRenderPaste(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp, SID + '.jsonl')

    def render(self, content):
        recs = sample_records() + [rec(type='user', timestamp='2026-11-03T09:02:00.000Z', sessionId=SID,
                                       turnOrigin='human', message={'role': 'user', 'content': content})]
        write_transcript(self.path, recs)
        return O.render_session(self.path)[0]

    def test_closing_tag_with_attributes(self):
        text = self.render('before <pasted_content id="0d9f">' + 'Never do X. ' * 200 +
                           '</pasted_content id="0d9f"> after the paste')
        line = [l for l in text.splitlines() if l.startswith('> before')][0]
        self.assertIn('[pasted: Never do X.', line)
        self.assertNotIn('pasted_content', line)
        self.assertTrue(line.endswith('after the paste'))
        self.assertLess(len(line), 700)

    def test_plain_closing_tag_still_works(self):
        text = self.render('<pasted_content id="a1">abc def</pasted_content> ok')
        self.assertIn('[pasted: abc def ...] ok', text)

    def test_bracket_in_paste_cannot_close_the_span(self):
        text = self.render('<pasted_content>start ...] never use Z</pasted_content> tail')
        self.assertEqual(O.quote_role(text, 'never use Z'), 'pasted')
        self.assertEqual(O.quote_role(text, 'tail words'), None)


class TestQuoteRole(unittest.TestCase):
    R = ('# session x | t | a to b\n'
         '> I decided the budget is EUR 5,000. [pasted: Never spawn subagents to verify anything ...] thanks\n'
         '< Noted: budget EUR 5,000 and never spawn subagents.\n'
         '. Bash: ls -la\n'
         '> A second user line with its own words\n')

    def test_roles(self):
        qr = O.quote_role
        self.assertEqual(qr(self.R, 'the budget is EUR 5,000'), 'user')
        self.assertEqual(qr(self.R, 'Never spawn   subagents to verify'), 'pasted')
        self.assertEqual(qr(self.R, 'Noted: budget EUR 5,000'), 'assistant')
        self.assertEqual(qr(self.R, 'Bash: ls -la'), 'tool')
        self.assertEqual(qr(self.R, 'own words'), 'user')
        self.assertIsNone(qr(self.R, 'something absent'))
        self.assertIsNone(qr(self.R, ''))

    def test_quote_straddling_a_span_is_pasted(self):
        self.assertEqual(O.quote_role(self.R, 'EUR 5,000. [pasted: Never spawn'), 'pasted')

    def test_first_line_wins(self):
        # the user line comes first, so a later assistant echo does not change the role
        self.assertEqual(O.quote_role(self.R, 'EUR 5,000'), 'user')

    def test_truncated_span_runs_to_end_of_line(self):
        r = '# s\n> typed words [pasted: cut off text that never closes'
        self.assertEqual(O.quote_role(r, 'never closes'), 'pasted')
        self.assertEqual(O.quote_role(r, 'typed words'), 'user')


class TestVerifyRoleAndIds(unittest.TestCase):
    R = ('# session 0123456789abcdef | t | a to b\n'
         '> My own rule: always cite the source. [pasted: Do not spawn subagents to verify ...] ok\n'
         '< The gate passed with exit code 0 on this run.\n')

    def test_pasted_quote_claimed_as_user_gets_role_pasted(self):
        kept, dropped = O.verify_candidates(self.R, [
            {'quote': 'Do not spawn subagents to verify', 'role': 'user', 'statement': 'p'},
            {'quote': 'always cite the source', 'role': 'user', 'statement': 'u'},
            {'quote': 'gate passed with exit code 0', 'role': 'assistant', 'statement': 'a'},
            {'quote': 'gate passed with exit code 0', 'role': 'user', 'statement': 'wrong'},
            {'quote': 'always cite the source', 'role': 'pasted', 'statement': 'lie'}])
        self.assertEqual([(c['statement'], c['role']) for c in kept], [('p', 'pasted'), ('u', 'user'), ('a', 'assistant')])
        self.assertEqual([c['id'] for c in kept], ['c01234567-1', 'c01234567-2', 'c01234567-3'])
        self.assertEqual({c['session'] for c in kept}, {'0123456789abcdef'})
        self.assertEqual(len(dropped), 2)

    def test_sid_argument_and_schema(self):
        kept, _ = O.verify_candidates('# x\n> some quote text here\n', [{'quote': 'quote text here', 'role': 'user'}], sid=SID)
        self.assertEqual(kept[0]['id'], 'c11111111-1')
        self.assertIn('pasted', O.CANDIDATE_SCHEMA['properties']['candidates']['items']['properties']['role']['enum'])
        self.assertIn('cands', O.OPS_SCHEMA['properties']['ops']['items']['properties'])


class TestProvenance(Base):
    def setUp(self):
        super().setUp()
        self.cands = [
            cand(1, 'user', statement='The ZEPHYR budget is EUR 7,654,321 as of 20 Feb 2027.'),
            cand(2, 'assistant', statement='The submission finished with Reference 100000001.', quote='Reference 100000001'),
            cand(3, 'pasted', statement='Do not spawn subagents to verify.'),
            cand(1, 'user', session=SID2, statement='Other session fact 77.'),
        ]

    def apply(self, ops, sessions=(SID,)):
        return O.apply_ops(self.mem, ops, set(sessions), TODAY, self.cands)

    def op(self, cands, **kw):
        d = {'op': 'append', 'file': 'zephyr.md', 'text': 'Budget is EUR 7654321.', 'sources': [SID], 'cands': cands}
        d.update(kw)
        return d

    def test_ok_with_citation_and_role_label(self):
        applied, rejected = self.apply([self.op(['c11111111-1'])])
        self.assertEqual(len(applied), 1, rejected)
        self.assertIn('(dream 2026-11-11: session 11111111, user)', self.read('zephyr.md'))

    def test_role_label_comes_from_candidates_not_the_op(self):
        applied, rejected = self.apply([self.op(['c11111111-2'], text='Reference 100000001 recorded.', role='user')])
        self.assertEqual(len(applied), 1, rejected)
        self.assertTrue(self.read('zephyr.md').rstrip().endswith('session 11111111, assistant)'))

    def test_no_cands_or_unknown_cands_rejected(self):
        for c in (None, [], ['nope-1'], 'c11111111-1', [5]):
            ops = [self.op(c)] if c is not None else [{k: v for k, v in self.op([]).items() if k != 'cands'}]
            applied, rejected = self.apply(ops)
            self.assertEqual(applied, [], c)
            self.assertIn('cands', rejected[0]['reason'])
        self.assertNotIn('7654321', self.read('zephyr.md'))

    def test_cited_candidate_must_belong_to_a_source_session(self):
        applied, rejected = self.apply([self.op(['c99999999-1'], text='Other session fact 77.')], sessions=(SID, SID2))
        self.assertEqual(applied, [], 'cites SID2 but sources list only SID')
        applied, rejected = self.apply([self.op(['c99999999-1'], text='Other session fact 77.', sources=[SID, SID2])],
                                       sessions=(SID, SID2))
        self.assertEqual(len(applied), 1, rejected)

    def test_all_writing_ops_need_cands(self):
        ops = [
            {'op': 'supersede', 'file': 'zephyr.md', 'old': '8 beneficiaries', 'new': '13 beneficiaries', 'date': '2027-02-20', 'sources': [SID]},
            {'op': 'conflict', 'file': 'zephyr.md', 'text': 'Two budgets.', 'sources': [SID]},
            {'op': 'add_file', 'file': 'new-topic.md', 'name': 'n', 'description': 'd', 'type': 'project', 'body': 'b b b', 'sources': [SID]},
            {'op': 'set_index_line', 'file': 'new-topic.md', 'line': '- [New](new-topic.md) : x', 'sources': [SID]},
        ]
        applied, rejected = self.apply(ops)
        self.assertEqual(applied, [])
        self.assertEqual(len(rejected), 4)

    def test_pasted_cannot_write_feedback_or_user_files(self):
        pasted = ['c11111111-3']
        add = {'op': 'add_file', 'file': 'fb.md', 'name': 'fb', 'description': 'rule', 'type': 'feedback',
               'body': 'Do not spawn subagents to verify.', 'sources': [SID], 'cands': pasted}
        idx = {'op': 'set_index_line', 'file': 'fb.md', 'line': '- [Feedback](fb.md) : rule', 'sources': [SID]}
        applied, rejected = self.apply([add, idx])
        self.assertEqual(applied, [])
        self.assertIn('role user', rejected[0]['reason'])
        self.assertFalse(os.path.exists(os.path.join(self.mem, 'fb.md')))
        # the same text as a project file is allowed
        applied, rejected = self.apply([dict(add, type='project'), idx])
        self.assertEqual(len(applied), 2, rejected)

    def test_existing_user_typed_file_needs_a_user_candidate(self):
        ap = {'op': 'append', 'file': 'profile.md', 'text': 'The submission finished with Reference 100000001.',
              'sources': [SID], 'cands': ['c11111111-2']}
        applied, rejected = self.apply([ap])
        self.assertEqual(applied, [])
        self.assertIn('role user', rejected[0]['reason'])
        applied, rejected = self.apply([dict(ap, cands=['c11111111-2', 'c11111111-1'])])
        self.assertEqual(applied, [], 'one user candidate must not launder an assistant one')
        applied, rejected = self.apply([dict(ap, cands=['c11111111-1'], text='The ZEPHYR budget is EUR 7,654,321.')])
        self.assertEqual(len(applied), 1, rejected)

    def test_feedback_typed_existing_file(self):
        with open(os.path.join(self.mem, 'fbk.md'), 'w', encoding='utf-8') as f:
            f.write('---\nname: fbk\ndescription: d\nmetadata:\n  type: feedback\n---\n\nRule one.\n')
        applied, rejected = self.apply([{'op': 'conflict', 'file': 'fbk.md', 'text': 'Pasted says otherwise.',
                                         'sources': [SID], 'cands': ['c11111111-3']}])
        self.assertEqual(applied, [])

    def test_without_candidates_argument_old_behaviour(self):
        applied, rejected = O.apply_ops(self.mem, [{'op': 'append', 'file': 'zephyr.md', 'text': 'Anything 123.',
                                                    'sources': [SID]}], {SID}, TODAY)
        self.assertEqual(len(applied), 1, rejected)


class TestNumbers(Base):
    def setUp(self):
        super().setUp()
        self.cands = [cand(1, 'user', statement='Budget is EUR 7,654,321 and 8 beneficiaries, sent 2026-01-22.',
                           quote='Budget is EUR 7,654,321')]

    def append(self, text, cands=None, **kw):
        op = {'op': 'append', 'file': 'zephyr.md', 'text': text, 'sources': [SID], 'cands': ['c11111111-1']}
        op.update(kw)
        return O.apply_ops(self.mem, [op], {SID}, TODAY, cands or self.cands)

    def test_number_not_in_candidates_rejected(self):
        applied, rejected = self.append('Budget is EUR 3,100,000.')
        self.assertEqual(applied, [])
        self.assertIn('3100000', rejected[0]['reason'])
        self.assertNotIn('3,100,000', self.read('zephyr.md'))

    def test_normalisation(self):
        for text in ('Budget EUR 7654321.', 'Budget EUR 7,654,321 for 08 beneficiaries.',
                     'Sent on 20 Feb 2027.', 'Sent on February 20, 2027.', 'Sent on 20th February.', 'Sent 2027-02-20.',
                     'Only the 2027 year and 2 month.'):
            applied, rejected = self.append(text)
            self.assertEqual(len(applied), 1, (text, rejected))

    def test_wrong_date_rejected(self):
        for text in ('Sent on 21 Feb 2027.', 'Sent on 2027-02-21.', 'Sent on 20 Mar 2027.', 'Sent on 20 Feb 2026.'):
            applied, rejected = self.append(text)
            self.assertEqual(applied, [], text)

    def test_numbers_from_target_file_and_today_are_allowed(self):
        applied, rejected = self.append('Still 7,600,000 per rev7 and today is 2026-11-11, 11 Nov.')
        self.assertEqual(len(applied), 1, rejected)

    def test_ids_glued_to_letters_must_be_carried_too(self):
        applied, rejected = self.append('See v1.9 and TP9999 and rev9.')
        self.assertEqual(applied, [])
        self.assertIn('v1.9', rejected[0]['reason'])
        c = [cand(1, 'user', statement='Ids TP1234 rev8 v1.2 in the quote.', quote='Ids TP1234 rev8 v1.2 in the quote.')]
        applied, rejected = self.append('See v1.2 and TP1234 and rev8.', cands=c)
        self.assertEqual(len(applied), 1, rejected)

    def test_modal_may_is_not_a_month(self):
        applied, rejected = self.append('The team may decide later.')
        self.assertEqual(len(applied), 1, rejected)

    def test_supersede_new_and_date_checked(self):
        op = {'op': 'supersede', 'file': 'zephyr.md', 'old': '8 beneficiaries', 'new': '13 beneficiaries',
              'date': '2027-02-20', 'sources': [SID], 'cands': ['c11111111-1']}
        applied, rejected = O.apply_ops(self.mem, [op], {SID}, TODAY, self.cands)
        self.assertEqual(applied, [])
        applied, rejected = O.apply_ops(self.mem, [dict(op, new='8 beneficiaries (EUR 7,654,321)')], {SID}, TODAY, self.cands)
        self.assertEqual(len(applied), 1, rejected)

    def test_add_file_body_checked(self):
        add = {'op': 'add_file', 'file': 'bud.md', 'name': 'bud', 'description': 'budget', 'type': 'project',
               'body': 'Budget is EUR 2,411,000.', 'sources': [SID], 'cands': ['c11111111-1']}
        idx = {'op': 'set_index_line', 'file': 'bud.md', 'line': '- [Budget](bud.md) : money', 'sources': [SID]}
        applied, rejected = O.apply_ops(self.mem, [add, idx], {SID}, TODAY, self.cands)
        self.assertEqual(applied, [])
        applied, rejected = O.apply_ops(self.mem, [dict(add, body='Budget is EUR 7,654,321.'), idx], {SID}, TODAY, self.cands)
        self.assertEqual(len(applied), 2, rejected)

    def test_index_line_numbers_must_be_in_the_topic_file(self):
        idx = {'op': 'set_index_line', 'file': 'zephyr.md', 'line': '- [ZEPHYR bid](zephyr.md) : budget EUR 3,000,000', 'sources': [SID]}
        applied, rejected = O.apply_ops(self.mem, [idx], {SID}, TODAY)
        self.assertEqual(applied, [])
        self.assertIn('3000000', rejected[0]['reason'])
        ok = dict(idx, line='- [ZEPHYR bid](zephyr.md) : EUR 7,600,000, 8 beneficiaries, 22 Jan 2026')
        applied, rejected = O.apply_ops(self.mem, [ok], {SID}, TODAY)
        self.assertEqual(len(applied), 1, rejected)

    def test_apply_provenance_tag_is_not_checked(self):
        applied, rejected = self.append('Plain statement without digits.')
        self.assertEqual(len(applied), 1, rejected)
        self.assertIn('(dream 2026-11-11', self.read('zephyr.md'))


class TestIndexCasefold(Base):
    def test_memory_md_any_case_rejected_for_every_op(self):
        for name in ('memory.md', 'Memory.md', 'MEMORY.md'):
            ops = [
                {'op': 'append', 'file': name, 'text': 'injected line', 'sources': [SID]},
                {'op': 'conflict', 'file': name, 'text': 'injected line', 'sources': [SID]},
                {'op': 'supersede', 'file': name, 'old': 'Memory Index', 'new': 'X', 'date': '2027-02-20', 'sources': [SID]},
                {'op': 'archive', 'file': name, 'reason': 'x', 'sources': [SID]},
                {'op': 'add_file', 'file': name, 'name': 'n', 'description': 'd', 'type': 'project', 'body': 'b', 'sources': [SID]},
                {'op': 'set_index_line', 'file': name, 'line': '- [M](%s) : x' % name, 'sources': [SID]},
            ]
            applied, rejected = self.apply(ops)
            self.assertEqual(applied, [], name)
            self.assertEqual(len(rejected), 6)
        self.assertEqual(sorted(os.listdir(self.mem)), sorted(['MEMORY.md', 'zephyr.md', 'profile.md', 'old-trip.md']))


class TestDashes(Base):
    def test_every_string_field_is_checked(self):
        base = {'op': 'append', 'file': 'zephyr.md', 'text': 'Fine text.', 'sources': [SID]}
        for field, val in (('role', 'user \u2014 x'), ('date', '2026\u201311\u201305'), ('reason', 'a \u2014 b'), ('name', 'a \u2013 b'),
                           ('description', 'a \u2014 b'), ('body', 'a \u2014 b'), ('new', 'a \u2014 b'), ('text', 'a \u2013 b')):
            applied, rejected = self.apply([dict(base, **{field: val})])
            self.assertEqual(applied, [], field)
            self.assertIn('dash', rejected[0]['reason'])

    def test_index_title_checked_but_separator_allowed(self):
        bad_title = '- [ZEPHYR \u2014 bid](zephyr.md) \u2014 budget and partners in file'
        applied, rejected = self.apply([{'op': 'set_index_line', 'file': 'zephyr.md', 'line': bad_title, 'sources': [SID]}])
        self.assertEqual(applied, [])
        self.assertIn('dash', rejected[0]['reason'])
        bad_hook = '- [ZEPHYR bid](zephyr.md) \u2014 budget \u2013 partners'
        applied, rejected = self.apply([{'op': 'set_index_line', 'file': 'zephyr.md', 'line': bad_hook, 'sources': [SID]}])
        self.assertEqual(applied, [])
        ok = '- [ZEPHYR bid](zephyr.md) \u2014 budget and partners in file'
        applied, rejected = self.apply([{'op': 'set_index_line', 'file': 'zephyr.md', 'line': ok, 'sources': [SID]}])
        self.assertEqual(len(applied), 1, rejected)

    def test_add_file_title_in_new_index_line(self):
        add = {'op': 'add_file', 'file': 'nf.md', 'name': 'nf', 'description': 'd', 'type': 'project', 'body': 'body text', 'sources': [SID]}
        idx = {'op': 'set_index_line', 'file': 'nf.md', 'line': '- [N \u2014 F](nf.md) \u2014 hook', 'sources': [SID]}
        applied, rejected = self.apply([add, idx])
        self.assertEqual(applied, [])

    def test_supersede_old_may_quote_existing_dashes(self):
        with open(os.path.join(self.mem, 'zephyr.md'), 'a', encoding='utf-8') as f:
            f.write('Status \u2014 submitted.\n')
        applied, rejected = self.apply([{'op': 'supersede', 'file': 'zephyr.md', 'old': 'Status \u2014 submitted.',
                                         'new': 'Status: submitted and final.', 'date': '2027-02-20', 'sources': [SID]}])
        self.assertEqual(len(applied), 1, rejected)


class TestGateFix(Base):
    def setUp(self):
        super().setUp()
        self.before = os.path.join(self.tmp, 'before')
        shutil.copytree(self.mem, self.before)

    def append_index(self, text):
        with open(os.path.join(self.mem, 'MEMORY.md'), 'a', encoding='utf-8') as f:
            f.write(text)

    def test_valid_new_index_line_passes(self):
        with open(os.path.join(self.mem, 'extra.md'), 'w', encoding='utf-8') as f:
            f.write('---\nname: e\n---\n\nx\n')
        self.append_index('- [Extra](extra.md) \u2014 hook\n')
        code, problems, _ = O.gate(self.before, self.mem, [], [])
        self.assertEqual(code, 0, problems)

    def test_invalid_added_index_lines_withheld(self):
        for bad in ('free text injected into the index\n', '# New heading\n', '- no link here\n',
                    '- [Old trip](old-trip.md) \u2014 ' + 'z' * 200 + '\n'):
            shutil.copy(os.path.join(self.before, 'MEMORY.md'), os.path.join(self.mem, 'MEMORY.md'))
            self.append_index(bad)
            code, problems, _ = O.gate(self.before, self.mem, [], [])
            self.assertEqual(code, 2, bad)
            self.assertTrue(any('index line' in p for p in problems))

    def test_changed_index_line_must_be_valid(self):
        p = os.path.join(self.mem, 'MEMORY.md')
        text = O.read(p).replace('- [Old trip](old-trip.md) \u2014 Sorrento trip Apr 2026', 'Old trip: gone wrong')
        O.write(p, text)
        code, problems, _ = O.gate(self.before, self.mem, [], [])
        self.assertEqual(code, 2, problems)

    def test_protected_file_rewrite_withheld_append_ok(self):
        p = os.path.join(self.mem, 'profile.md')
        old = O.read(p)
        O.write(p, old + '\nAlso coaches a chess club.\n')
        code, problems, _ = O.gate(self.before, self.mem, [], [])
        self.assertEqual(code, 0, problems)
        O.write(p, old.replace('Senior Analyst.', 'Senior Analyst and more text.') + 'tail\n')
        code, problems, _ = O.gate(self.before, self.mem, [], [])
        self.assertEqual(code, 2)
        self.assertTrue(any('protected' in x for x in problems))

    def test_protected_file_moved_withheld(self):
        os.makedirs(os.path.join(self.mem, 'archive'))
        shutil.move(os.path.join(self.mem, 'profile.md'), os.path.join(self.mem, 'archive', 'profile.md'))
        code, problems, _ = O.gate(self.before, self.mem, [], [])
        self.assertEqual(code, 2)

    def test_denials_warn(self):
        code, problems, warnings = O.gate(self.before, self.mem, [], [], [{'tool': 'Read', 'path': '/etc/hosts'}])
        self.assertEqual((code, problems), (1, []))
        self.assertTrue(any('permission denial' in w for w in warnings))
        self.assertEqual(O.gate(self.before, self.mem, [], [], [])[0], 0)


class FakeProc:
    def __init__(self, out, code=0):
        self.stdout, self.stderr, self.returncode = out, '', code


class TestClaudeCall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')
        os.makedirs(os.path.join(self.tmp, 'run', 'renders'))
        os.makedirs(os.path.join(self.tmp, 'run', 'stage'))
        for rel in ('renders/a.txt', 'renders/b.txt', 'stage/MEMORY.md', 'secret.txt'):
            with open(os.path.join(self.tmp, 'run', rel), 'w') as f:
                f.write(rel)
        self.seen = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_run(self, payload):
        def run(args, cwd=None, **kw):
            self.seen.update(args=args, cwd=cwd, files=sorted(os.path.relpath(os.path.join(b, n), cwd)
                                                              for b, _, fs in os.walk(cwd) for n in fs))
            return FakeProc(json.dumps(payload))
        return run

    def call(self, payload=None, **kw):
        payload = payload or {'structured_output': {'ok': 1}, 'usage': {'input_tokens': 3, 'output_tokens': 2}, 'num_turns': 1}
        with mock.patch.object(O.subprocess, 'run', self.fake_run(payload)):
            return O.claude_call('prompt', {'type': 'object'}, **kw)

    def test_inputs_only_and_cleanup(self):
        run = os.path.join(self.tmp, 'run')
        out, info = self.call(inputs={'renders/a.txt': os.path.join(run, 'renders', 'a.txt'),
                                      'deep/er/stage': os.path.join(run, 'stage')})
        self.assertEqual(out, {'ok': 1})
        self.assertEqual(self.seen['files'], ['deep/er/stage/MEMORY.md', 'renders/a.txt'])
        self.assertTrue(os.path.basename(self.seen['cwd']).startswith('oneiros-'))
        self.assertFalse(os.path.exists(self.seen['cwd']), 'sandbox removed afterwards')
        self.assertNotEqual(os.path.dirname(self.seen['cwd']), run)
        self.assertEqual(info['denials'], [])

    def test_cwd_is_copied_when_no_inputs(self):
        self.call(cwd=os.path.join(self.tmp, 'run'))
        self.assertIn('secret.txt', self.seen['files'])
        self.assertNotEqual(self.seen['cwd'], os.path.join(self.tmp, 'run'))

    def test_permission_flags_and_tools(self):
        self.call(inputs={})
        a = self.seen['args']
        self.assertEqual(a[a.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(a[a.index('--permission-prompts') + 1], 'none')
        self.assertNotIn('--allowedTools', a)
        i = a.index('--tools')
        self.assertEqual(a[i + 1:i + 4], ['Read', 'Glob', 'Grep'])
        self.call(inputs={}, tools=())
        a = self.seen['args']
        self.assertEqual(a[a.index('--tools') + 1], '')

    def test_denials_are_reported(self):
        payload = {'structured_output': {}, 'permission_denials': [
            {'tool_name': 'Read', 'tool_input': {'file_path': '/Users/x/memory/MEMORY.md'}},
            {'tool_name': 'Grep', 'tool_input': {'pattern': 'secret', 'path': '/repo'}},
            {'tool_name': 'Glob', 'tool_input': {'pattern': '**/*.md'}}]}
        out, info = self.call(payload, inputs={})
        self.assertEqual(info['denials'], [{'tool': 'Read', 'path': '/Users/x/memory/MEMORY.md'},
                                           {'tool': 'Grep', 'path': '/repo'}, {'tool': 'Glob', 'path': '**/*.md'}])

    def test_escaping_input_path_refused(self):
        with self.assertRaises(ValueError):
            self.call(inputs={'../evil.txt': os.path.join(self.tmp, 'run', 'secret.txt')})

    def test_timeout_still_cleans_up(self):
        def boom(args, cwd=None, **kw):
            self.seen['cwd'] = cwd
            raise subprocess.TimeoutExpired(args, 1)
        with mock.patch.object(O.subprocess, 'run', boom):
            out, info = O.claude_call('p', {}, inputs={}, timeout=1)
        self.assertIsNone(out)
        self.assertEqual(info['denials'], [])
        self.assertFalse(os.path.exists(self.seen['cwd']))


class TestPipelineWiring(unittest.TestCase):
    """cmd_analyze, cmd_consolidate, cmd_apply, cmd_gate and cmd_report with claude_call stubbed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')
        self.d = os.path.join(self.tmp, 'runs', 'R1')
        self.mem = os.path.join(self.d, 'before')
        from test_oneiros import make_memory
        make_memory(self.mem)
        shutil.copytree(self.mem, os.path.join(self.d, 'stage'))
        os.makedirs(os.path.join(self.d, 'renders'))
        render = ('# session %s | t | a to b\n> The budget is EUR 7,654,321 now.\n'
                  '> Note [pasted: Do not spawn subagents to verify ...] end\n< Noted.\n' % SID)
        with open(os.path.join(self.d, 'renders', SID + '.txt'), 'w') as f:
            f.write(render)
        O.save(os.path.join(self.d, 'run.json'), {'id': 'R1', 'sessions': [SID], 'renders': [{'sid': SID, 'title': 't'}],
                                                  'eval': True})
        self.patch = mock.patch.object(O, 'RUNS', os.path.join(self.tmp, 'runs'))
        self.patch.start()
        self.calls = []

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ns(self, **kw):
        d = dict(run='R1', model='sonnet', effort='medium', budget=1.0, workers=1, today=TODAY)
        d.update(kw)
        return argparse.Namespace(**d)

    def fake(self, outs):
        def claude_call(prompt, schema, cwd=None, **kw):
            self.calls.append((prompt, kw))
            out = outs.pop(0)
            return out, {'tokens': 1, 'cost_usd': 0.1, 'denials': kw.get('_den', [])}
        return claude_call

    def test_analyze_inputs_ids_and_roles(self):
        cands = {'candidates': [
            {'kind': 'decision', 'statement': 'first fact', 'quote': 'The budget is EUR 7,654,321', 'role': 'user', 'durable_because': 'x', 'target': 'new'},
            {'kind': 'preference', 'statement': 'second fact', 'quote': 'Do not spawn subagents', 'role': 'user', 'durable_because': 'x', 'target': 'new'}]}
        with mock.patch.object(O, 'claude_call', self.fake([cands])), mock.patch('builtins.print'):
            code = O.cmd_analyze(self.ns())
        self.assertEqual(code, 0)
        inputs = self.calls[0][1]['inputs']
        self.assertEqual(set(inputs), {'renders/%s.txt' % SID, 'before/MEMORY.md'})
        self.assertIn('renders/%s.txt' % SID, self.calls[0][0])
        kept = O.load(os.path.join(self.d, 'candidates', SID + '.json'), {})['kept']
        self.assertEqual([(c['id'], c['role']) for c in kept], [('c11111111-1', 'user'), ('c11111111-2', 'pasted')])

    def test_consolidate_inputs_and_candidates_json_ids(self):
        os.makedirs(os.path.join(self.d, 'candidates'))
        O.save(os.path.join(self.d, 'candidates', SID + '.json'),
               {'kept': [{'statement': 'x', 'quote': 'xxxxxxxxx', 'role': 'user', 'session': SID}], 'dropped': []})
        # a candidate without a disposition would trigger the retry call, so this one is skipped
        out = {'ops': [], 'flags': [], 'summary': 's', 'dispositions': [{'cand': 'c11111111-1', 'status': 'skip', 'reason': 'one-off'}]}
        with mock.patch.object(O, 'claude_call', self.fake([out])), mock.patch('builtins.print'):
            self.assertEqual(O.cmd_consolidate(self.ns()), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(set(self.calls[0][1]['inputs']), {'stage', 'candidates.json'})
        self.assertEqual(O.load(os.path.join(self.d, 'candidates.json'), [])[0]['id'], 'c11111111-1')

    def test_apply_reads_candidates_json_and_rejects_uncited(self):
        O.save(os.path.join(self.d, 'candidates.json'), [cand(1, 'user', statement='Budget is EUR 7,654,321.')])
        ops = [{'op': 'append', 'file': 'zephyr.md', 'text': 'Budget is EUR 7,654,321.', 'sources': [SID], 'cands': ['c11111111-1']},
               {'op': 'append', 'file': 'zephyr.md', 'text': 'Uncited claim.', 'sources': [SID]}]
        O.save(os.path.join(self.d, 'ops.json'), {'ops': ops, 'flags': [], 'summary': ''})
        with mock.patch('builtins.print'):
            O.cmd_apply(self.ns())
        res = O.load(os.path.join(self.d, 'applied.json'), {})
        self.assertEqual((len(res['applied']), len(res['rejected'])), (1, 1))
        # no candidates.json at all: fail closed
        os.remove(os.path.join(self.d, 'candidates.json'))
        shutil.rmtree(os.path.join(self.d, 'stage'))
        shutil.copytree(self.mem, os.path.join(self.d, 'stage'))
        with mock.patch('builtins.print'):
            O.cmd_apply(self.ns())
        self.assertEqual(O.load(os.path.join(self.d, 'applied.json'), {})['applied'], [])

    def test_denials_logged_gate_warns_report_lists(self):
        O.save(os.path.join(self.d, 'calls.json'), [{'step': 'analyze:11111111', 'denials': [{'tool': 'Read', 'path': '/etc/hosts'}]},
                                                    {'step': 'consolidate', 'denials': []}])
        O.save(os.path.join(self.d, 'applied.json'), {'applied': [], 'rejected': []})
        with mock.patch('builtins.print'):
            code = O.cmd_gate(self.ns())
            O.cmd_report(self.ns(notify=False))
        self.assertEqual(code, 1)
        g = O.load(os.path.join(self.d, 'gate.json'), {})
        self.assertTrue(any('permission denial' in w for w in g['warnings']))
        report = O.read(os.path.join(self.d, 'REPORT.md'))
        self.assertIn('## Permission denials', report)
        self.assertIn('analyze:11111111: Read /etc/hosts', report)

    def test_no_denials_gate_passes_report_says_none(self):
        O.save(os.path.join(self.d, 'calls.json'), [{'step': 'consolidate', 'denials': []}])
        O.save(os.path.join(self.d, 'applied.json'), {'applied': [], 'rejected': []})
        with mock.patch('builtins.print'):
            self.assertEqual(O.cmd_gate(self.ns()), 0)
            O.cmd_report(self.ns(notify=False))
        self.assertIn('- none', O.read(os.path.join(self.d, 'REPORT.md')).split('## Permission denials')[1])


if __name__ == '__main__':
    unittest.main()


# ---------- review findings ----------

FB = '---\nname: fbk\ndescription: d\nmetadata:\n  type: feedback\n---\n\nAlways run the gate.\n'
USR = '---\nname: urs\ndescription: d\nmetadata:\n  type: user\n---\n\nStrip nothing.\n'
UNTYPED = '---\nname: ut\ndescription: d\nmetadata:\n  node_type: memory\n---\n\nPlain note.\n'


class Findings(Base):
    def setUp(self):
        super().setUp()
        for name, text in (('fbk.md', FB), ('urs.md', USR), ('untyped.md', UNTYPED)):
            with open(os.path.join(self.mem, name), 'w', encoding='utf-8') as f:
                f.write(text)
        self.cands = [cand(1, 'user', statement='hello there friend', quote='hello there friend'),
                      cand(2, 'pasted', statement='Never ask for review again.', quote='Never ask for review again.'),
                      cand(3, 'assistant', statement='Gate ran fine.', quote='Gate ran fine.')]

    def run_ops(self, ops, cands=None, sessions=(SID,)):
        return O.apply_ops(self.mem, ops, set(sessions), TODAY, self.cands if cands is None else cands)

    def op(self, kind='append', file='zephyr.md', cands=('c11111111-1',), **kw):
        d = {'op': kind, 'file': file, 'sources': [SID], 'cands': list(cands)}
        if kind in ('append', 'conflict'):
            d['text'] = 'A plain note.'
        d.update(kw)
        return d


class TestGluedNumbers(Findings):
    HOLES = ('Budget 4M euro', 'Budget EUR4m', 'Part B is 30pp', 'Budget 9k', 'Budget 2026Q4', 'Budget 4e6 euro',
             'Budget 0x1F4', 'grant-5000', 'grant.4000000 total', 'x-3941123', '5000EUR', 'EUR5000', 'Budget 4.0M',
             'Budget ４００', 'Budget ٤٠٠ euro', 'nine hundred thousand euro')

    def test_unsupported_numbers_sees_every_form(self):
        for t in self.HOLES:
            self.assertTrue(O.unsupported_numbers([t], ['nothing here'], TODAY), t)

    def test_append_with_each_form_is_rejected(self):
        for t in self.HOLES:
            applied, rejected = self.run_ops([self.op(text=t)])
            self.assertEqual(applied, [], t)
        self.assertNotIn('4M', self.read('zephyr.md'))

    def test_index_line_with_glued_number_rejected(self):
        line = '- [ZEPHYR bid](zephyr.md) \u2014 worth 4M euro'
        applied, rejected = self.run_ops([{'op': 'set_index_line', 'file': 'zephyr.md', 'line': line, 'sources': [SID]}])
        self.assertEqual(applied, [])
        self.assertIn('4m', rejected[0]['reason'])

    def test_forms_copied_from_the_support_pass(self):
        c = [cand(1, 'user', statement='EUR 5M and 30pp and TP1234', quote='EUR 5M and 30pp and TP1234')]
        applied, rejected = self.run_ops([self.op(text='Call is EUR 5M, Part B 30pp, flight TP1234.')], cands=c)
        self.assertEqual(len(applied), 1, rejected)
        applied, rejected = self.run_ops([self.op(text='Call is EUR 50M.')], cands=c)
        self.assertEqual(applied, [])

    def test_ordinary_prose_still_passes(self):
        applied, rejected = self.run_ops([self.op(text='One reviewer signs; first and second drafts agree.')])
        self.assertEqual(len(applied), 1, rejected)


class TestIndexLinksBare(Findings):
    def idx(self, line, file='zephyr.md'):
        return {'op': 'set_index_line', 'file': file, 'line': line, 'sources': [SID]}

    def test_second_link_outside_the_folder_rejected(self):
        other = os.path.join(self.tmp, 'other.md')
        with open(other, 'w', encoding='utf-8') as f:
            f.write('98765 secret\n')
        before = self.read('MEMORY.md')
        for link in (other, '../other.md', 'https://x.test/a.md', 'sub/a.md', 'MEMORY.md'):
            line = '- [ZEPHYR bid](zephyr.md) \u2014 budget see [b](%s)' % link
            applied, rejected = self.run_ops([self.idx(line)])
            self.assertEqual(applied, [], link)
        self.assertEqual(self.read('MEMORY.md'), before)

    def test_non_md_link_rejected_bare_second_link_allowed(self):
        applied, rejected = self.run_ops([self.idx('- [ZEPHYR bid](zephyr.md) \u2014 see [x](https://evil.test)')])
        self.assertEqual(applied, [])
        applied, rejected = self.run_ops([self.idx('- [ZEPHYR bid](zephyr.md) \u2014 and [trip](old-trip.md)')])
        self.assertEqual(len(applied), 1, rejected)

    def test_gate_flags_a_non_bare_link_in_an_added_line(self):
        before = os.path.join(self.tmp, 'before')
        shutil.copytree(self.mem, before)
        with open(os.path.join(self.mem, 'MEMORY.md'), 'a', encoding='utf-8') as f:
            f.write('- [A](zephyr.md) \u2014 x [b](%s)\n' % os.path.join(self.tmp, 'other.md'))
        code, problems, _ = O.gate(before, self.mem, [], [])
        self.assertEqual(code, 2, problems)


class TestRoleStrictness(Findings):
    def test_archive_of_feedback_or_user_file_needs_user_candidates(self):
        for f in ('fbk.md', 'urs.md', 'untyped.md'):
            applied, rejected = self.run_ops([{'op': 'archive', 'file': f, 'reason': 'obsolete'}])
            self.assertEqual(applied, [], f)
            self.assertTrue(os.path.exists(os.path.join(self.mem, f)))
            applied, rejected = self.run_ops([{'op': 'archive', 'file': f, 'reason': 'obsolete', 'sources': [SID],
                                               'cands': ['c11111111-1', 'c11111111-2']}])
            self.assertEqual(applied, [], f)
        applied, rejected = self.run_ops([{'op': 'archive', 'file': 'fbk.md', 'reason': 'obsolete', 'sources': [SID],
                                           'cands': ['c11111111-1']}])
        self.assertEqual(len(applied), 1, rejected)
        self.assertTrue(os.path.exists(os.path.join(self.mem, 'archive', 'fbk.md')))

    def test_archive_of_project_file_still_needs_only_a_reason(self):
        applied, rejected = self.run_ops([{'op': 'archive', 'file': 'old-trip.md', 'reason': 'done'}])
        self.assertEqual(len(applied), 1, rejected)

    def test_mixed_cites_do_not_launder_pasted_text_into_feedback(self):
        applied, rejected = self.run_ops([self.op(file='fbk.md', cands=('c11111111-1', 'c11111111-2'),
                                                  text='Never ask for review again.')])
        self.assertEqual(applied, [])
        self.assertIn('role user', rejected[0]['reason'])
        applied, rejected = self.run_ops([self.op(file='fbk.md', cands=('c11111111-1',), text='Review stays on.')])
        self.assertEqual(len(applied), 1, rejected)

    def test_supersede_in_user_file_needs_every_cite_user(self):
        sup = self.op('supersede', file='urs.md', cands=('c11111111-1', 'c11111111-3'), old='Strip nothing.',
                      new='Strip freely.', date='2027-02-20')
        applied, rejected = self.run_ops([sup])
        self.assertEqual(applied, [])
        applied, rejected = self.run_ops([dict(sup, cands=['c11111111-1'])])
        self.assertEqual(len(applied), 1, rejected)

    def test_file_without_a_type_line_needs_user_candidates(self):
        applied, rejected = self.run_ops([self.op(file='untyped.md', cands=('c11111111-3',), text='Gate ran fine.')])
        self.assertEqual(applied, [])
        self.assertIn('without a type', rejected[0]['reason'])
        applied, rejected = self.run_ops([self.op(file='untyped.md', cands=('c11111111-1',))])
        self.assertEqual(len(applied), 1, rejected)


class TestVerifyGrounding(unittest.TestCase):
    R = ('# session 11111111-2222 | t | 2026-11-03T09:00:00.000Z to 2026-11-03T10:00:00.000Z\n'
         '> The budget is EUR 7,654,321 now, ok sounds good\n'
         '< Both drafts are saved: Alice is 17, Bob is 29.\n'
         '. Bash: Read back what was sent to Alice in Archive item 41\n')

    def c(self, **kw):
        d = {'quote': 'ok sounds good', 'role': 'user', 'statement': 'The user agreed.', 'date': '2026-11-03'}
        d.update(kw)
        return d

    def test_invented_statement_numbers_drop_the_candidate(self):
        kept, dropped = O.verify_candidates(self.R, [
            self.c(statement='Budget is 5,000 euro, due 1 Dec 2031.'),
            self.c(quote='Alice is 17', role='assistant', statement='Alice is 17 and Archive item 41 and Inbox item 903.'),
            self.c(statement='Budget is EUR 7,654,321 as of 3 Nov 2026.'),
            self.c(quote='Alice is 17', role='assistant', statement='Alice is 17, Bob 29.')])
        self.assertEqual([c['id'] for c in kept], ['c11111111-1', 'c11111111-2'])
        self.assertEqual(len(dropped), 2)
        self.assertIn('statement numbers', dropped[0]['why'])
        # 41 stands two render lines after the quote, so only 903 is ungrounded
        self.assertIn('903', dropped[1]['why'])
        self.assertNotIn('41', dropped[1]['why'])

    def test_date_outside_the_session_is_cleared(self):
        kept, _ = O.verify_candidates(self.R, [self.c(date='2031-01-01'), self.c(date='2026-11-03'), self.c(date='')])
        self.assertEqual([c['date'] for c in kept], ['', '2026-11-03', ''])

    def test_relative_date_converted_inside_the_session_days_is_kept(self):
        kept, _ = O.verify_candidates(self.R, [self.c(statement='Agreed on 3 Nov, 2026-11-03.')])
        self.assertEqual(len(kept), 1)

    def test_statement_cannot_launder_a_number_into_apply(self):
        kept, _ = O.verify_candidates(self.R, [self.c(statement='Budget is 5,000 euro.')])
        self.assertEqual(kept, [])


class TestOrphanFiles(Findings):
    def add(self, **kw):
        d = {'op': 'add_file', 'file': 'zz.md', 'name': 'zz', 'description': 'd', 'type': 'project', 'body': 'Some body text.',
             'sources': [SID], 'cands': ['c11111111-1']}
        d.update(kw)
        return d

    def idx(self, line='- [ZZ](zz.md) \u2014 hook'):
        return {'op': 'set_index_line', 'file': 'zz.md', 'line': line, 'sources': [SID]}

    def test_rejected_index_line_rolls_the_new_file_back(self):
        applied, rejected = self.run_ops([self.add(), self.idx('- [ZZ](zz.md) \u2014 77 things')])
        self.assertEqual(applied, [])
        self.assertEqual(len(rejected), 2)
        self.assertFalse(os.path.exists(os.path.join(self.mem, 'zz.md')))
        self.assertNotIn('zz.md', self.read('MEMORY.md'))

    def test_index_line_listed_first_still_works(self):
        applied, rejected = self.run_ops([self.idx(), self.add()])
        self.assertEqual(len(applied), 2, rejected)
        self.assertIn('(zz.md)', self.read('MEMORY.md'))

    def test_appends_to_a_rolled_back_file_are_rolled_back_too(self):
        ap = self.op(file='zz.md', text='More text here.')
        applied, rejected = self.run_ops([self.add(), ap, self.idx('- [ZZ](zz.md) \u2014 77 things')])
        self.assertEqual(applied, [])
        self.assertFalse(os.path.exists(os.path.join(self.mem, 'zz.md')))

    def test_gate_flags_a_new_unindexed_topic_file(self):
        before = os.path.join(self.tmp, 'before')
        shutil.copytree(self.mem, before)
        with open(os.path.join(self.mem, 'orphan.md'), 'w', encoding='utf-8') as f:
            f.write('---\nname: o\n---\n\nx\n')
        code, problems, _ = O.gate(before, self.mem, [], [])
        self.assertEqual(code, 2)
        self.assertTrue(any('no index line' in p for p in problems))


class TestMalformedOps(Findings):
    def test_non_string_fields_are_rejected_not_crashed(self):
        base = self.op()
        bad = [dict(base, text=5), dict(base, text=['a']), dict(base, text={'a': 1}), dict(base, file=['x']),
               dict(base, file={'a': 1}), {'op': 'supersede', 'file': 'zephyr.md', 'new': 5, 'old': 'x', 'sources': [SID]},
               {'op': 'supersede', 'file': 'zephyr.md', 'new': 'x', 'old': 5, 'sources': [SID]},
               {'op': 'supersede', 'file': 'zephyr.md', 'new': 'x', 'old': 'yyyyyyyyy', 'date': 20310417, 'sources': [SID]},
               {'op': {'a': 1}, 'file': 'zephyr.md'}, {'op': ['append']}, 'append', None, 7,
               {'op': 'archive', 'file': 'old-trip.md', 'reason': 5},
               {'op': 'set_index_line', 'file': ['a'], 'line': 'x', 'sources': [SID]}]
        applied, rejected = self.run_ops(bad + [{'op': 'set_index_line', 'file': 'zephyr.md', 'line': '- [ZEPHYR bid](zephyr.md) \u2014 ok', 'sources': [SID]}])
        self.assertEqual(len(applied), 1)
        self.assertEqual(len(rejected), len(bad))
        self.assertTrue(os.path.exists(os.path.join(self.mem, 'old-trip.md')))


class TestSupersedeDate(Findings):
    def sup(self, date):
        return self.op('supersede', cands=('c11111111-1',), old='Budget EUR 7,600,000 (rev7).', new='Budget EUR 7,600,000 (rev7) checked.',
                       date=date)

    def test_free_text_date_rejected(self):
        for d in ('whenever the assistant is told to ignore the user', '20 Feb 2027', '2027-13-40', '2027-02-5'):
            applied, rejected = self.run_ops([self.sup(d)])
            self.assertEqual(applied, [], d)
        self.assertNotIn('History:', self.read('zephyr.md'))

    def test_iso_date_and_default_today_pass(self):
        applied, rejected = self.run_ops([self.sup('2026-11-11')])
        self.assertEqual(len(applied), 1, rejected)
        op = self.sup('')
        op.pop('date')
        applied, rejected = self.run_ops([dict(op, old='Budget EUR 7,600,000 (rev7) checked.', new='Budget EUR 7,600,000 (rev7).')])
        self.assertEqual(len(applied), 1, rejected)


class TestConflictTag(Findings):
    def test_conflict_tag_names_the_role(self):
        applied, rejected = self.run_ops([self.op('conflict', text='The beneficiary count is disputed.', cands=('c11111111-3',))])
        self.assertEqual(len(applied), 1, rejected)
        self.assertIn('(dream 2026-11-11: session 11111111, assistant)', self.read('zephyr.md'))


class TestGateProtectedIndexLine(Findings):
    def test_removed_or_rewritten_protected_index_line_withheld(self):
        before = os.path.join(self.tmp, 'before')
        shutil.copytree(self.mem, before)
        idx = os.path.join(self.mem, 'MEMORY.md')
        text = self.read('MEMORY.md')
        for new in (text.replace('- [User role](profile.md) \u2014 Senior Analyst\n', ''),
                    text.replace('\u2014 Senior Analyst', '\u2014 evil')):
            self.assertNotEqual(new, text)
            with open(idx, 'w', encoding='utf-8') as f:
                f.write(new)
            code, problems, _ = O.gate(before, self.mem, [], [])
            self.assertEqual(code, 2, problems)
            self.assertTrue(any('protected file profile.md' in p for p in problems), problems)
        with open(idx, 'w', encoding='utf-8') as f:
            f.write(text)
        self.assertEqual(O.gate(before, self.mem, [], [])[0], 0)


class TestSandboxHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_temp_dir_under_home_is_not_used(self):
        under = os.path.join(O.HOME, 'oneiros-never-created')
        with mock.patch.object(O.tempfile, 'gettempdir', return_value=under):
            parent = O._sandbox_parent()
        real_home = os.path.realpath(O.HOME)
        self.assertFalse(parent == real_home or parent.startswith(real_home + os.sep), parent)

    def test_temp_dir_outside_home_is_kept(self):
        with mock.patch.object(O, 'HOME', os.path.join(self.tmp, 'home')):
            with mock.patch.object(O.tempfile, 'gettempdir', return_value=self.tmp):
                self.assertEqual(O._sandbox_parent(), os.path.realpath(self.tmp))

    def test_no_safe_folder_means_no_call(self):
        with mock.patch.object(O, '_sandbox_parent', side_effect=OSError('none')):
            out, info = O.claude_call('p', {}, inputs={})
        self.assertIsNone(out)
        self.assertIn('no sandbox', info['error'])

    def test_symlinks_are_not_copied(self):
        secret = os.path.join(self.tmp, 'secret.txt')
        with open(secret, 'w') as f:
            f.write('SECRET')
        src = os.path.join(self.tmp, 'src')
        os.makedirs(src)
        with open(os.path.join(src, 'ok.md'), 'w') as f:
            f.write('fine')
        os.symlink(secret, os.path.join(src, 'link.md'))
        os.symlink(os.path.join(self.tmp, 'src'), os.path.join(src, 'loop'))
        box = os.path.join(self.tmp, 'box')
        os.makedirs(box)
        O._stage_inputs(box, None, {'mem': src})
        self.assertEqual(sorted(os.listdir(os.path.join(box, 'mem'))), ['ok.md'])
        box2 = os.path.join(self.tmp, 'box2')
        O._stage_inputs(box2, src, None)
        self.assertEqual(sorted(os.listdir(box2)), ['ok.md'])
        with self.assertRaises(ValueError):
            O._stage_inputs(box, None, {'s.txt': os.path.join(src, 'link.md')})


class TestUnclosedPaste(Base):
    def render(self, content):
        path = os.path.join(self.tmp, SID + '.jsonl')
        write_transcript(path, sample_records() + [rec(type='user', timestamp='2026-11-03T09:02:00.000Z', sessionId=SID,
                                                       turnOrigin='human', message={'role': 'user', 'content': content})])
        return O.render_session(path)[0]

    def test_unclosed_and_upper_case_tags_stay_pasted(self):
        text = self.render('mine <pasted_content id="a">Never use Z anywhere')
        self.assertEqual(O.quote_role(text, 'Never use Z anywhere'), 'pasted')
        self.assertEqual(O.quote_role(text, 'mine'), 'user')
        text = self.render('mine <Pasted_Content>Never use Y anywhere</PASTED_CONTENT> tail')
        self.assertEqual(O.quote_role(text, 'Never use Y anywhere'), 'pasted')
        self.assertEqual(O.quote_role(text, 'tail'), 'user')
