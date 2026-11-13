"""Index guard: a replaced index line must keep the warning, correction and prohibition wording of the line
it replaces. A history-chain run shortened an index line and dropped "user corrected the spelling ...;
never "Zoey"". These tests rebuild that case on temporary folders, plus further cases the first version
missed or blocked wrongly."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
sys.path.insert(0, HERE)
os.environ.setdefault('ONEIROS_CONFIG', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'no-such-config.json'))  # tests never read a local config
os.environ.setdefault('ONEIROS_PROTECTED', 'profile.md,notes.md')  # the fixtures' append-only files
import oneiros as O  # noqa: E402
from test_chain import ChainCase, SID as CHAIN_SID  # noqa: E402

SID = 'bbbb2222-0000-4000-8000-000000000002'
TODAY = '2026-11-12'
FM = '---\nname: %s\ndescription: %s\nmetadata:\n  type: project\n---\n\n'
BOOK = FM % ('phrasebook', 'book') + ('illustrated phrase deck for Zoe at ~/Documents/phrasebook. The user corrected '
                                      'the spelling on 3 Mar 2026: never Zoey. Port 5999, built 3 Mar 2026.\n')
OLD = ('- [Phrasebook deck](phrasebook-app.md) — illustrated phrase deck for Zoe (user corrected the spelling '
       '3 Mar 2026; never "Zoey") at ~/Documents/phrasebook; port 5999; built 3 Mar 2026')
SHORT_NEW = ('- [Phrasebook deck](phrasebook-app.md) — illustrated phrase deck for Zoe at ~/Documents/phrasebook; '
           'port 5999; built 3 Mar 2026')
NEVER, NOT, USER, WARN = O.NEVER, O.NOT, 'user said or decided', '⚠ or warning'
# one phrase per guard label: dropping it must be caught, keeping it must not
SAMPLES = {NEVER: 'never cc the team', NOT: 'the stable call, not the beta one', 'must': 'must cc the team', 'always': 'always cc the team',
           'corrected': 'name corrected', 'wrong': 'the wrong repo', 'beware': 'beware of rate limits',
           'avoid': 'avoid the API key', 'confidential': 'confidential filing', 'rule': 'standing rule',
           WARN: '⚠️ flaky', USER: 'user decided to wait'}


def L(text, file='a.md', title='A'):
    return '- [%s](%s) — %s' % (title, file, text)


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.mem = tempfile.mkdtemp(prefix='oneiros-guard-')
        O.write(os.path.join(self.mem, 'phrasebook-app.md'), BOOK)

    def tearDown(self):
        shutil.rmtree(self.mem, ignore_errors=True)

    def index(self, *lines):
        O.write(os.path.join(self.mem, 'MEMORY.md'), '# Memory Index\n\n' + '\n'.join(lines) + '\n')

    def topic(self, name, body):
        O.write(os.path.join(self.mem, name), FM % (name[:-3], 'x') + body + '\n')

    def set_line(self, file, line):
        op = {'op': 'set_index_line', 'file': file, 'line': line, 'sources': [SID[:8]]}
        return O.apply_ops(self.mem, [op], [SID], TODAY)

    def idx(self):
        return O.read(os.path.join(self.mem, 'MEMORY.md'))

    def lost(self, old, new):
        return O.lost_guard_terms(L(old), L(new))


class TestApplyRule(GuardCase):
    def test_a_shortened_phrasebook_line_is_rejected(self):
        self.index(OLD)
        applied, rejected = self.set_line('phrasebook-app.md', SHORT_NEW)
        self.assertEqual(applied, [])
        reason = rejected[0]['reason']
        self.assertIn('drops warning or correction wording', reason)
        for term in (NEVER, 'corrected', USER, '"Zoey"'):
            self.assertIn(term, reason)
        self.assertIn(OLD, self.idx(), 'the live line stays as it was')

    def test_a_shortening_that_keeps_the_correction_lands(self):
        self.index(OLD)
        new = ('- [Phrasebook deck](phrasebook-app.md) — deck for Zoe (user corrected 3 Mar 2026; never '
               'Zoey), port 5999')
        applied, rejected = self.set_line('phrasebook-app.md', new)
        self.assertEqual((len(applied), rejected), (1, []))
        self.assertIn(new, self.idx())

    def test_a_dropped_contraction_is_rejected_end_to_end(self):
        self.topic('drafts.md', 'Drafts.')
        self.index(L('we don’t send drafts', 'drafts.md'))
        applied, rejected = self.set_line('drafts.md', L('we send drafts', 'drafts.md'))
        self.assertEqual(applied, [])
        self.assertIn(NEVER, rejected[0]['reason'])

    def test_a_masking_link_target_is_rejected_end_to_end(self):
        self.topic('rules.md', 'Rules.')
        self.topic('never-notes.md', 'Notes.')
        self.index(L('never send unapproved', 'rules.md'))
        applied, rejected = self.set_line('rules.md', L('send, see [N](never-notes.md)', 'rules.md'))
        self.assertEqual(applied, [])
        self.assertIn(NEVER, rejected[0]['reason'])

    def test_a_new_index_line_is_not_checked(self):
        self.index(L('never this', 'other.md', 'Other'))
        self.topic('other.md', 'Other.')
        applied, rejected = self.set_line('phrasebook-app.md', '- [Phrasebook deck](phrasebook-app.md) — deck for Zoe')
        self.assertEqual((len(applied), rejected), (1, []))

    def test_a_status_update_lands(self):
        self.topic('drafts.md', 'Builds 5 and 6 NOT shipped. Build 5 check RED.')  # NOT here: the facts rule keeps upper-case words
        self.index(L('builds 5 and 6 NOT shipped (5 check RED)', 'drafts.md'))
        applied, rejected = self.set_line('drafts.md', L('builds 5 and 6 shipped (5 check RED)', 'drafts.md'))
        self.assertEqual((len(applied), rejected), (1, []))


class TestGuardTerms(unittest.TestCase):
    def lost(self, old, new):
        return O.lost_guard_terms(L(old), L(new))

    def test_every_label_has_a_sample(self):
        self.assertEqual({label for label, _ in O.GUARD_PATTERNS}, set(SAMPLES))

    def test_dropping_each_label_is_caught_and_keeping_it_is_not(self):
        for label, phrase in SAMPLES.items():
            with self.subTest(label=label):
                self.assertEqual(O.guard_terms(L(phrase))[0], {label: 1})
                self.assertEqual(self.lost(phrase + '; plan', 'plan'), [label])
                self.assertEqual(self.lost(phrase + '; plan', phrase), [])

    def test_the_helper_on_the_shortened_line_case(self):
        self.assertEqual(O.lost_guard_terms(OLD, OLD), [])
        self.assertEqual(O.lost_guard_terms(OLD, SHORT_NEW), ['corrected', NEVER, USER, '"Zoey"'])

    def test_each_occurrence_counts(self):
        self.assertEqual(self.lost('never cc the team; never send unapproved', 'never send unapproved'), [NEVER])
        self.assertEqual(self.lost('must cc the team; must sign', 'must sign'), ['must'])
        self.assertEqual(self.lost('size, not line count; the stable call, not beta', 'size, not line count'), [NOT])

    def test_case_is_folded_both_ways(self):
        self.assertEqual(self.lost('NEVER send', 'send'), [NEVER])
        self.assertEqual(self.lost('never send', 'NEVER send'), [])

    def test_negation_forms_are_one_class(self):
        self.assertEqual(self.lost("we don't send", 'we send'), [NEVER])
        self.assertEqual(self.lost('we can’t send', 'we send'), [NEVER], 'curly apostrophe')
        for variant in ('don‘t', 'donʼt', 'don`t'):
            with self.subTest(variant=variant):
                self.assertEqual(self.lost(variant + ' send', 'do not send'), [])
        self.assertEqual(self.lost('cannot send', "can't send"), [])
        self.assertEqual(self.lost('never send', 'do not send'), [])

    def test_prohibitions_and_contrasts_count_but_status_does_not(self):
        self.assertEqual(self.lost('agents must not edit templates', 'agents edit templates'), [NEVER])
        self.assertEqual(self.lost('agents must not edit templates', 'agents never edit templates'), [], "'must not' is one prohibition")
        self.assertEqual(self.lost('measure file size, not line count', 'measure file size'), [NOT])
        self.assertEqual(self.lost('NOT the beta tier', 'the stable tier'), [NOT])
        self.assertEqual(self.lost('real data, not seed data', 'real data'), [NOT], "'seed' ends in -ed but is no participle")
        self.assertEqual(self.lost("this isn't the beta tier", 'the stable tier'), [NOT], 'a status contraction in a contrast')
        for rule in ('sharing is not allowed', 'not permitted until QA signs', 'not authorised outside Acme',
                     'not authorized outside Acme', 'not recommended for production', 'not supported on 3.9',
                     'not required', 'not approved'):
            with self.subTest(rule=rule):
                self.assertEqual(self.lost(rule, 'x'), [NOT])
        updates = [('NOT sent', 'sent'), ('not released or announced', 'released 17 Jun'), ('not posted', 'posted'),
                   ('not submitted', 'SUBMITTED 17 Jun'), ('not run', 'run'), ('not yet sent', 'sent 17 Jun'),
                   ('has not been merged', 'merged'), ('not fully built', 'built'), ("isn't sent", 'sent'),
                   ("wasn't submitted", 'submitted'), ("hasn't been merged", 'merged'), ('not ready', 'ready'),
                   ('not live', 'live'), ('not complete', 'complete'), ('not available', 'available'),
                   ('not working', 'working'), ('not yet approved', 'approved'), ('terms not yet agreed', 'terms agreed'),
                   ('not currently supported', 'supported'), ('is not being sent', 'sent'), ('not officially sent', 'sent')]
        for old, new in updates:
            with self.subTest(status=old):
                self.assertEqual(self.lost('drafts %s' % old, 'drafts %s' % new), [])

    def test_a_dropped_status_clause_is_caught_by_its_word(self):
        self.assertEqual(self.lost('team brief not shared outside Acme', 'team brief'), ['not shared'])
        self.assertEqual(self.lost('in review, not disclosed to partners', 'in review'), ['not disclosed'])
        self.assertEqual(self.lost('embargoed, not published', 'embargoed'), ['not published'])
        self.assertEqual(self.lost('not sent; not released', 'sent'), ['not released'])
        self.assertEqual(self.lost('not sent; not sent again', 'sent'), ['not sent'], 'each occurrence counts')
        self.assertEqual(self.lost('not yet sent', 'delivered'), ['not sent'], 'fail closed when the status word changes')

    def test_a_quoted_must_not_counts_once(self):
        self.assertEqual(O.guard_terms(L("agents `must not` quit"))[0], {NEVER: 1})
        self.assertEqual(self.lost('agents `must not` edit templates', 'agents never edit templates'), [])

    def test_a_new_status_not_cannot_stand_in_for_a_lost_never(self):
        self.assertEqual(self.lost('pilot; never cc the team; draft saved', 'pilot; draft saved, not yet reviewed'), [NEVER])
        self.assertEqual(self.lost('never publish before sign-off; built', 'built; not ready for release'), [NEVER])
        self.assertEqual(self.lost('do not edit by hand; v2', 'v2; has not been tested'), [NEVER])
        self.assertEqual(self.lost('size, not line count; x', 'x; not ready'), [NOT])

    def test_user_counts_only_where_it_marks_what_the_user_said_or_decided(self):
        self.assertEqual(self.lost("finished (user's call 3 Mar)", 'finished 22 Apr'), [USER])
        self.assertEqual(self.lost('user 3 Mar: use the CLI', 'use the CLI'), [USER])
        self.assertEqual(self.lost('the user states they differ', 'they differ'), [USER])
        for plain in ("inquiry via the user's mail app", 'installed user-wide', 'wait for the user', 'users told'):
            with self.subTest(plain=plain):
                self.assertEqual(self.lost(plain, 'x'), [])

    def test_words_inside_paths_hyphens_and_other_words_do_not_count(self):
        self.assertEqual(O.guard_terms(L('never-ending logs, ~/never/notes, knotty rules, nothing, avoidance'))[0], {})
        self.assertEqual(self.lost('never touch the live DB', 'touch the live DB; never-ending logs'), [NEVER])

    def test_link_targets_are_blanked_and_titles_count(self):
        self.assertEqual(O.lost_guard_terms(L('never send'), L('send [N](never.md)')), [NEVER])
        self.assertEqual(O.lost_guard_terms(L('see [B](rule.md)'), L('see B')), [])
        self.assertEqual(O.guard_terms(L('see [docs](https://a.test/x?q=never)'))[0], {})
        self.assertEqual(O.lost_guard_terms('- [Do not share pair](pair.md) — facts', '- [Pair](pair.md) — facts'), [NEVER])

    def test_the_warning_sign_counts_with_or_without_its_selector(self):
        self.assertEqual(self.lost('⚠️ main branch is v2', 'main branch is v2'), [WARN])
        self.assertEqual(self.lost('⚠️ main branch is v2', '⚠ main branch: v2'), [])
        self.assertEqual(self.lost('⚠️ main branch', 'WARNING: main branch'), [])

    def test_quoted_spans_survive_by_whole_words(self):
        self.assertEqual(self.lost('never “Zoey”', 'never mind'), ['"Zoey"'])
        self.assertEqual(self.lost('never "Zo"', 'never Zoe'), ['"Zo"'], 'a span inside a longer word does not count')
        self.assertEqual(self.lost('"blue ribbon" = prizes', '"Blue ribbon" means prizes'), [])
        self.assertEqual(self.lost('"Zed · Zero Error Daemon"', 'Zed (Zero Error Daemon)'), [])
        self.assertEqual(self.lost('x "q" y "q"', 'x'), ['"q"'])

    def test_an_unbalanced_or_mixed_quote_cannot_hide_a_span(self):
        self.assertEqual(self.lost('a big" screen; never "Zoey" spelling', 'a big" screen; never mind spelling'),
                         ['"Zoey"'])
        self.assertEqual(self.lost('never “Zoey" spelling', 'never again spelling'), ['"Zoey"'])

    def test_a_counting_tripwire_not_a_reading_of_meaning(self):
        """Known limit, kept on purpose: counts can stay equal while the meaning flips."""
        self.assertEqual(self.lost('never delete it', 'delete it, never later'), [])
        self.assertEqual(self.lost('not shared outside Acme', 'shared outside Acme'), [])
        self.assertEqual(self.lost('you must not delete it', 'you must delete it, not later'), [NEVER])


class TestGateRule(GuardCase):
    def setUp(self):
        super().setUp()
        self.index(OLD, L('pilot facts; never share the list', 'pilot.md', 'Pilot'))
        self.topic('pilot.md', 'The pilot runs in Riverton.')
        self.topic('other.md', 'Other.')
        self.stage = os.path.join(tempfile.mkdtemp(prefix='oneiros-guard-stage-'), 's')
        shutil.copytree(self.mem, self.stage)

    def tearDown(self):
        shutil.rmtree(os.path.dirname(self.stage), ignore_errors=True)
        super().tearDown()

    def before_index(self, *lines):
        self.index(*lines)

    def stage_index(self, *lines):
        O.write(os.path.join(self.stage, 'MEMORY.md'), '# Memory Index\n\n' + '\n'.join(lines) + '\n')

    def gate(self):
        return O.gate(self.mem, self.stage, [], [])

    def test_unchanged_index_passes(self):
        self.assertEqual(self.gate()[0], 0)

    def test_a_line_that_lost_its_correction_withholds(self):
        self.stage_index(SHORT_NEW, L('pilot facts; never share the list', 'pilot.md', 'Pilot'))
        code, problems, _ = self.gate()
        self.assertEqual(code, 2)
        self.assertTrue(any('index line of phrasebook-app.md dropped warning or correction wording' in p
                            and '"Zoey"' in p for p in problems), problems)

    def test_a_rewrite_that_keeps_the_wording_passes(self):
        self.stage_index(OLD, L('never share the pilot list', 'pilot.md', 'Pilot'))
        self.assertEqual(self.gate()[0], 0)

    def test_an_archived_files_removed_line_warns(self):
        os.makedirs(os.path.join(self.stage, 'archive'))
        shutil.move(os.path.join(self.stage, 'pilot.md'), os.path.join(self.stage, 'archive', 'pilot.md'))
        self.stage_index(OLD)
        code, problems, warnings = self.gate()
        self.assertEqual((code, problems), (1, []))
        self.assertTrue(any('pilot.md removed (file archived)' in w and NEVER in w for w in warnings), warnings)

    def test_a_removed_prose_line_with_a_link_warns(self):
        self.before_index(OLD, '⚠️ never edit [Pilot](pilot.md) by hand')
        os.makedirs(os.path.join(self.stage, 'archive'))
        shutil.move(os.path.join(self.stage, 'pilot.md'), os.path.join(self.stage, 'archive', 'pilot.md'))
        self.stage_index(OLD)
        code, _, warnings = self.gate()
        self.assertEqual(code, 1)
        self.assertTrue(any('pilot.md removed' in w for w in warnings), warnings)

    def test_an_earlier_line_that_mentions_the_file_is_not_its_successor(self):
        mention = L('see also [Pilot](pilot.md)', 'other.md', 'Other')
        self.before_index(mention, OLD, L('pilot facts; never share the list', 'pilot.md', 'Pilot'))
        self.stage_index(mention, OLD, L('pilot facts, never share the list', 'pilot.md', 'Pilot'))
        self.assertEqual(self.gate()[0], 0, 'the edited Pilot line kept never')
        loud = L('never see [Pilot](pilot.md)', 'other.md', 'Other')
        self.before_index(loud, OLD, L('pilot facts; never share the list', 'pilot.md', 'Pilot'))
        self.stage_index(loud, OLD, L('pilot facts', 'pilot.md', 'Pilot'))
        self.assertEqual(self.gate()[0], 2, 'the Pilot line lost never; the earlier line keeping one does not cover it')
        self.before_index(OLD, L('pilot facts; never share the list', 'pilot.md', 'Pilot'), loud)
        self.stage_index(OLD, L('never see [Pilot](pilot.md) now', 'other.md', 'Other'), L('pilot facts', 'pilot.md', 'Pilot'))
        self.assertEqual(self.gate()[0], 2, 'both lines changed in one block: the mention is still not the successor')

    def test_two_lines_with_one_first_link_are_paired_in_order(self):
        self.before_index(OLD, L('never A', 'pilot.md', 'Pilot'), L('never B', 'pilot.md', 'Pilot'))
        self.stage_index(OLD, L('never A', 'pilot.md', 'Pilot'), L('plain B', 'pilot.md', 'Pilot'))
        self.assertEqual(self.gate()[0], 2)
        self.stage_index(OLD, L('never A', 'pilot.md', 'Pilot'))
        code, problems, warnings = self.gate()
        self.assertEqual((code, problems), (1, []), 'a deleted second line is reported as removed')
        self.assertTrue(any('pilot.md removed' in w for w in warnings), warnings)

    def test_a_moved_unchanged_line_is_not_reported(self):
        self.stage_index(L('pilot facts; never share the list', 'pilot.md', 'Pilot'), OLD)
        self.assertEqual(self.gate(), (0, [], []))


class TestSwapReplay(ChainCase):
    def test_replay_cannot_bring_in_a_line_that_drops_a_warning(self):
        O.write(os.path.join(self.live, 'MEMORY.md'), '# Memory Index\n\n- [Pilot](pilot.md) — pilot facts; never share the list\n')
        d1 = self.dream('R1', 'The pilot partner is Lakeside.')
        res = O.load(os.path.join(d1, 'applied.json'), {})
        drop = {'op': 'set_index_line', 'file': 'pilot.md', 'line': '- [Pilot](pilot.md) — pilot facts',
                'sources': [CHAIN_SID[:8]]}
        O.save(os.path.join(d1, 'applied.json'), {'applied': res['applied'] + [drop], 'rejected': []})
        O.write(os.path.join(self.live, 'other.md'), FM % ('other', 'other') + 'Other facts.\n')  # live drifted: replay
        code, out = self.cmd('swap', 'R1')
        self.assertEqual((code, out['mode'], out['applied'], out['rejected']), (0, 'replay', 1, 1), out)
        self.assertIn('drops warning or correction wording', out['rejected_reasons'][0])
        self.assertIn('never share the list', O.read(os.path.join(self.live, 'MEMORY.md')))
        self.assertEqual(O.load(os.path.join(d1, 'swap.json'), {})['status'], 'swapped-partial')


if __name__ == '__main__':
    unittest.main()
