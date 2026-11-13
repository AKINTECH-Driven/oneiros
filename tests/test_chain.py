"""Chained dreams (start --after), swap in and out of order, LIFO undo and a failed swap. These run the real commands on a
temporary live memory folder."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
os.environ.setdefault('ONEIROS_CONFIG', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'no-such-config.json'))  # tests never read a local config
os.environ.setdefault('ONEIROS_PROTECTED', 'profile.md,notes.md')  # the fixtures' append-only files
import oneiros as O  # noqa: E402

SID = 'aaaa1111-0000-4000-8000-000000000001'
TOPIC = '---\nname: pilot\ndescription: pilot facts\nmetadata:\n  type: project\n---\n\nThe pilot runs in Riverton.\n'
INDEX = '# Memory Index\n\n- [Pilot](pilot.md) — pilot facts\n'


class ChainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')
        self.project = os.path.join(self.tmp, 'project')
        self.live = os.path.join(self.tmp, 'memory')
        os.makedirs(self.project)
        os.makedirs(self.live)
        O.write(os.path.join(self.live, 'MEMORY.md'), INDEX)
        O.write(os.path.join(self.live, 'pilot.md'), TOPIC)
        rec = {'type': 'user', 'timestamp': '2026-11-12T10:00:00Z', 'turnOrigin': 'human',
               'message': {'role': 'user', 'content': 'the pilot partner is Lakeside and the demo is in Hillview'}}
        with open(os.path.join(self.project, SID + '.jsonl'), 'w') as f:
            f.write(json.dumps(rec, separators=(',', ':')) + '\n')
        self.patches = [mock.patch.object(O, 'STATE', os.path.join(self.tmp, 'state.json')),
                        mock.patch.object(O, 'RUNS', os.path.join(self.tmp, 'runs')),
                        mock.patch.object(O, 'LIVE_MEMORY', self.live),
                        mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': '', 'CLAUDE_SESSION_ID': ''})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cmd(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = O.main(list(argv))
        return code, json.loads(out.getvalue())

    def dream(self, rid, text, after=None, gate=True):
        """One dream with a single append op that a real candidate supports, through start, apply, gate and report."""
        argv = ['start', '--id', rid, '--since', '2026-11-12', '--project', self.project] + (['--after', after] if after else [])
        code, out = self.cmd(*argv)
        self.assertEqual(code, 0, out)
        d = O.run_dir(rid)
        cand = {'id': 'c%s-1' % SID[:8], 'session': SID, 'role': 'user', 'statement': text, 'quote': 'the pilot partner is Lakeside',
                'date': '2026-11-12', 'target': 'pilot.md', 'kind': 'fact', 'durable_because': 'project state'}
        O.save(os.path.join(d, 'candidates.json'), [cand])
        O.save(os.path.join(d, 'ops.json'), {'ops': [{'op': 'append', 'file': 'pilot.md', 'text': text,
                                                       'sources': [SID[:8]], 'cands': [cand['id']]}], 'flags': [], 'summary': 's'})
        code, out = self.cmd('apply', rid)
        self.assertEqual(out['applied'], 1, out)
        if gate:
            self.assertEqual(self.cmd('gate', rid)[0], 0)
            self.assertEqual(self.cmd('report', rid)[0], 0)
        return d

    def text(self, folder):
        return O.read(os.path.join(folder, 'pilot.md'))


class TestChainStart(ChainCase):
    def test_snapshot_is_previous_stage_and_target_stays_live(self):
        d1 = self.dream('R1', 'The pilot partner is Lakeside.')
        d2 = self.dream('R2', 'The demo is in Hillview.', after='R1')
        run = O.load(os.path.join(d2, 'run.json'), {})
        self.assertEqual(run['after'], 'R1')
        self.assertEqual(os.path.realpath(run['memory_source']), os.path.realpath(self.live))
        self.assertEqual(run['before_manifest'], O.manifest(os.path.join(d1, 'stage')))
        self.assertIn('Lakeside', self.text(os.path.join(d2, 'stage')))
        self.assertIn('Hillview', self.text(os.path.join(d2, 'stage')))
        self.assertNotIn('Lakeside', self.text(self.live), 'a dream never touches live memory before a swap')
        self.assertIn('Chained after R1', O.read(os.path.join(d2, 'REPORT.md')))

    def test_after_refusals(self):
        self.dream('R1', 'The pilot partner is Lakeside.', gate=False)
        base = ['start', '--since', '2026-11-12', '--project', self.project]
        code, out = self.cmd(*base + ['--after', 'R1'])
        self.assertEqual(code, 2)
        self.assertIn('no passing gate', out['error'])
        O.save(os.path.join(O.run_dir('R1'), 'gate.json'), {'code': 2})
        self.assertIn('no passing gate', self.cmd(*base + ['--after', 'R1'])[1]['error'])
        O.save(os.path.join(O.run_dir('R1'), 'gate.json'), {'code': 1})
        self.assertIn('exclude each other', self.cmd(*base + ['--after', 'R1', '--memory', self.live])[1]['error'])
        self.assertIn('mix live and evaluation', self.cmd(*base + ['--after', 'R1', '--eval'])[1]['error'])
        self.assertIn('no run R9', self.cmd(*base + ['--after', 'R9'])[1]['error'])
        self.assertEqual(sorted(os.listdir(O.RUNS)), ['R1'], 'a refused start makes no run folder')


class TestSwapAndUndo(ChainCase):
    def test_swap_in_order_is_an_exact_copy(self):
        d1 = self.dream('R1', 'The pilot partner is Lakeside.')
        d2 = self.dream('R2', 'The demo is in Hillview.', after='R1')
        code, out = self.cmd('swap', 'R1')
        self.assertEqual((code, out['mode']), (0, 'copy'), out)
        self.assertEqual(O.manifest(self.live), O.manifest(os.path.join(d1, 'stage')))
        code, out = self.cmd('swap', 'R2')
        self.assertEqual((code, out['mode']), (0, 'copy'), out)
        self.assertEqual(O.manifest(self.live), O.manifest(os.path.join(d2, 'stage')))
        state = O.load(O.STATE, {})
        self.assertEqual([s['run'] for s in state['swaps']], ['R1', 'R2'])
        self.assertNotIn('pending_review', state)

    def test_swap_out_of_order_replays(self):
        self.dream('R1', 'The pilot partner is Lakeside.')
        self.dream('R2', 'The demo is in Hillview.', after='R1')
        code, out = self.cmd('swap', 'R2')
        self.assertEqual((code, out['mode'], out['applied']), (0, 'replay', 1), out)
        live = self.text(self.live)
        self.assertIn('Hillview', live)
        self.assertNotIn('Lakeside', live, 'replay applies only the swapped run\'s own operations')

    def test_undo_is_last_in_first_out(self):
        self.dream('R1', 'The pilot partner is Lakeside.')
        self.dream('R2', 'The demo is in Hillview.', after='R1')
        original = O.manifest(self.live)
        self.cmd('swap', 'R1')
        after_r1 = O.manifest(self.live)
        self.cmd('swap', 'R2')
        code, out = self.cmd('undo', 'R1')
        self.assertEqual(code, 2)
        self.assertIn('undo R2 first', out['error'])
        self.assertIn('Hillview', self.text(self.live), 'a refused undo changes nothing')
        self.assertEqual(self.cmd('undo', 'R2')[0], 0)
        self.assertEqual(O.manifest(self.live), after_r1)
        self.assertEqual(self.cmd('undo', 'R1')[0], 0)
        self.assertEqual(O.manifest(self.live), original)

    def test_undo_allowed_once_the_later_swap_is_undone_and_reswap_works(self):
        self.dream('R1', 'The pilot partner is Lakeside.')
        self.dream('R2', 'The demo is in Hillview.', after='R1')
        self.cmd('swap', 'R1')
        self.cmd('swap', 'R2')
        self.cmd('undo', 'R2')
        self.assertEqual(self.cmd('undo', 'R1')[0], 0)
        code, out = self.cmd('swap', 'R1')
        self.assertEqual((code, out['mode']), (0, 'copy'), out)
        self.assertEqual(self.cmd('undo', 'R1')[0], 0)

    def test_failed_post_swap_gate_restores_live_and_allows_a_retry(self):
        d1 = self.dream('R1', 'The pilot partner is Lakeside.')
        original = O.manifest(self.live)
        O.write(os.path.join(d1, 'stage', 'pilot.md'), '---\nshrunk\n')  # stage tampered after its gate passed
        code, out = self.cmd('swap', 'R1')
        self.assertEqual(code, 2)
        self.assertIn('restored', out['error'])
        self.assertEqual(O.manifest(self.live), original)
        self.assertFalse(os.path.exists(os.path.join(d1, 'live-backup')))
        self.assertTrue(os.path.isdir(out['backup']))
        self.assertNotIn('swaps', O.load(O.STATE, {}))
        rec = O.load(os.path.join(d1, 'swap.json'), {})
        self.assertEqual(rec['status'], 'restored')
        self.assertTrue(any('shrank' in p for p in rec['problems']), rec)
        snaps = [n for n in os.listdir(d1) if n.startswith('live-failed-swap-')]
        self.assertEqual(len(snaps), 1)
        self.assertIn('shrunk', O.read(os.path.join(d1, snaps[0], 'pilot.md')), 'the snapshot holds what the failed swap left')
        self.assertIn('restored', self.cmd('swap', 'R1')[1]['error'], 'a retry runs the gate again instead of refusing')



class TestReviewFixes(ChainCase):
    """Edge cases of the chain code: gate results, crashed copies, replay records and undo snapshots."""

    def test_swap_needs_a_passing_gate_result(self):
        d1 = self.dream('R1', 'The pilot partner is Lakeside.', gate=False)
        code, out = self.cmd('swap', 'R1')
        self.assertEqual(code, 2)
        self.assertIn('no gate result', out['error'])
        self.assertFalse(os.path.exists(os.path.join(d1, 'live-backup')))

    def test_crashed_backup_copy_is_never_taken_for_a_backup(self):
        d1 = self.dream('R1', 'The pilot partner is Lakeside.')
        original = O.manifest(self.live)
        real = shutil.copytree

        def half_copy(src, dst, *args, **kw):
            os.makedirs(dst)
            shutil.copy2(os.path.join(src, 'MEMORY.md'), dst)
            raise OSError(28, 'No space left on device')
        with mock.patch.object(O.shutil, 'copytree', side_effect=half_copy):
            with self.assertRaises(OSError):
                O.main(['swap', 'R1'])
        self.assertFalse(os.path.exists(os.path.join(d1, 'live-backup')))
        self.assertEqual(O.manifest(self.live), original, 'live is untouched until the backup is complete')
        self.assertIn('no live-backup', self.cmd('undo', 'R1')[1]['error'])
        with mock.patch.object(O.shutil, 'copytree', side_effect=real):
            code, out = self.cmd('swap', 'R1')
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(os.path.join(d1, 'live-backup.tmp')))

    def test_replay_that_applies_nothing_is_not_recorded(self):
        self.dream('R1', 'The pilot partner is Lakeside.')
        d2 = self.dream('R2', 'The demo is in Hillview.', after='R1')
        ops = O.load(os.path.join(d2, 'applied.json'), {})
        dep = {'op': 'supersede', 'file': 'pilot.md', 'old': 'The pilot partner is Lakeside.', 'new': 'The pilot partner is Hillview.',
               'date': '2026-11-12', 'sources': [SID[:8]], 'cands': ['c%s-1' % SID[:8]]}
        O.save(os.path.join(d2, 'applied.json'), {'applied': [dep], 'rejected': ops.get('rejected', [])})
        code, out = self.cmd('swap', 'R2')
        self.assertEqual(code, 2, out)
        self.assertIn('applied 0 of 1', out['error'])
        self.assertFalse(os.path.exists(os.path.join(d2, 'live-backup')))
        self.assertNotIn('swaps', O.load(O.STATE, {}))
        self.assertEqual(O.load(os.path.join(d2, 'swap.json'), {})['status'], 'refused')
        self.assertEqual(self.cmd('swap', 'R1')[0], 0, 'the earlier run still swaps')
        code, out = self.cmd('swap', 'R2')
        self.assertEqual((code, out['applied']), (0, 1), out)

    def test_replay_records_rejected_operations(self):
        self.dream('R1', 'The pilot partner is Lakeside.')
        d2 = self.dream('R2', 'The demo is in Hillview.', after='R1')
        res = O.load(os.path.join(d2, 'applied.json'), {})
        dep = {'op': 'supersede', 'file': 'pilot.md', 'old': 'The pilot partner is Lakeside.', 'new': 'The pilot partner is Hillview.',
               'date': '2026-11-12', 'sources': [SID[:8]], 'cands': ['c%s-1' % SID[:8]]}
        O.save(os.path.join(d2, 'applied.json'), {'applied': res['applied'] + [dep], 'rejected': []})
        code, out = self.cmd('swap', 'R2')
        self.assertEqual((code, out['mode'], out['applied'], out['rejected']), (0, 'replay', 1, 1), out)
        rec = O.load(os.path.join(d2, 'swap.json'), {})
        self.assertEqual(rec['status'], 'swapped-partial')
        self.assertIn('1 of the reviewed operations were not applied', out['note'])
        self.assertEqual(rec['note'], out['note'])
        self.assertEqual(len(rec['rejected']), 1)
        self.assertIn('exactly once', rec['rejected'][0]['reason'])
        self.assertEqual(rec['after_manifest'], O.manifest(self.live))

    def test_undo_snapshots_live_and_names_later_edits(self):
        d1 = self.dream('R1', 'The pilot partner is Lakeside.')
        original = O.manifest(self.live)
        self.cmd('swap', 'R1')
        O.write(os.path.join(self.live, 'other.md'), '---\nname: other\n---\nwritten by another session\n')
        code, out = self.cmd('undo', 'R1')
        self.assertEqual(code, 0, out)
        self.assertEqual(out['changed_since_swap'], ['other.md'])
        self.assertEqual(O.load(os.path.join(d1, 'swap.json'), {})['status'], 'undone')
        self.assertTrue(os.path.isfile(os.path.join(out['live_before_undo'], 'other.md')))
        self.assertEqual(O.manifest(self.live), original)

    def test_undo_guard_ignores_swaps_into_another_memory_folder(self):
        self.dream('R1', 'The pilot partner is Lakeside.')
        self.cmd('swap', 'R1')
        other = os.path.join(self.tmp, 'other-memory')
        shutil.copytree(self.live, other)
        code, out = self.cmd('start', '--id', 'X1', '--since', '2026-11-12', '--project', self.project, '--memory', other)
        self.assertEqual(code, 0, out)
        O.save(os.path.join(O.run_dir('X1'), 'ops.json'), {'ops': []})
        O.save(os.path.join(O.run_dir('X1'), 'candidates.json'), [])
        self.cmd('apply', 'X1')
        self.cmd('gate', 'X1')
        self.assertEqual(self.cmd('swap', 'X1')[0], 0)
        self.assertEqual(self.cmd('undo', 'R1')[0], 0, 'a later swap into a different folder does not block undo')

    def test_chain_base_needs_a_stage_folder(self):
        d1 = self.dream('R1', 'The pilot partner is Lakeside.')
        shutil.rmtree(os.path.join(d1, 'stage'))
        self.assertIn('no run R1', self.cmd('start', '--since', '2026-11-12', '--project', self.project, '--after', 'R1')[1]['error'])

    def test_failed_swap_restores_even_when_its_snapshot_copy_fails(self):
        d1 = self.dream('R1', 'The pilot partner is Lakeside.')
        original = O.manifest(self.live)
        O.write(os.path.join(d1, 'stage', 'pilot.md'), '---\nshrunk\n')
        real = shutil.copytree

        def no_space_for_snapshot(src, dst, *args, **kw):
            if 'live-failed-swap-' in dst:
                raise OSError(28, 'No space left on device')
            return real(src, dst, *args, **kw)
        with mock.patch.object(O.shutil, 'copytree', side_effect=no_space_for_snapshot):
            code, out = self.cmd('swap', 'R1')
        self.assertEqual(code, 2, out)
        self.assertEqual(O.manifest(self.live), original, 'live is restored although the snapshot failed')
        self.assertTrue(any('snapshot of the failed swap not kept' in p for p in out['problems']), out)
        self.assertFalse(os.path.exists(os.path.join(d1, 'live-backup')))

    def test_start_keeps_a_swap_recorded_while_it_rendered(self):
        real = O.render_session

        def render_and_swap_elsewhere(path, cap=O.RENDER_CAP):
            st = O.load(O.STATE, {})
            st.setdefault('swaps', []).append({'run': 'Z9', 'mode': 'copy', 't': 'x'})
            O.save(O.STATE, st)
            return real(path, cap)
        with mock.patch.object(O, 'render_session', side_effect=render_and_swap_elsewhere):
            code, out = self.cmd('start', '--id', 'R1', '--since', '2026-11-12', '--project', self.project)
        self.assertEqual(code, 0, out)
        st = O.load(O.STATE, {})
        self.assertEqual([s['run'] for s in st['swaps']], ['Z9'])
        self.assertEqual(st['lock']['run'], 'R1')



class TestSwapSkip(ChainCase):
    """swap --skip: the person leaves out operations they choose."""

    def two_op_run(self, rid):
        argv = ['start', '--id', rid, '--since', '2026-11-12', '--project', self.project]
        self.assertEqual(self.cmd(*argv)[0], 0)
        d = O.run_dir(rid)
        cand = {'id': 'c%s-1' % SID[:8], 'session': SID, 'role': 'user', 'statement': 'The pilot partner is Lakeside and the demo is in Hillview.',
                'quote': 'the pilot partner is Lakeside', 'date': '2026-11-12', 'target': 'pilot.md'}
        O.save(os.path.join(d, 'candidates.json'), [cand])
        ops = [{'op': 'append', 'file': 'pilot.md', 'text': t, 'sources': [SID[:8]], 'cands': [cand['id']]}
               for t in ('The pilot partner is Lakeside.', 'The demo is in Hillview.')]
        O.save(os.path.join(d, 'ops.json'), {'ops': ops})
        self.assertEqual(self.cmd('apply', rid)[1]['applied'], 2)
        self.assertEqual(self.cmd('gate', rid)[0], 0)
        return d

    def test_skip_leaves_the_named_operation_out(self):
        d = self.two_op_run('R1')
        code, out = self.cmd('swap', 'R1', '--skip', '1')
        self.assertEqual((code, out['mode'], out['applied'], out['skipped']), (0, 'replay', 1, [1]), out)
        live = self.text(self.live)
        self.assertIn('Hillview', live)
        self.assertNotIn('Lakeside', live)
        rec = O.load(os.path.join(d, 'swap.json'), {})
        self.assertEqual(rec['status'], 'swapped')
        self.assertEqual([s['n'] for s in rec['skipped']], [1])
        self.assertIn('Lakeside', rec['skipped'][0]['op']['text'])
        self.assertEqual(self.cmd('undo', 'R1')[0], 0)
        self.assertNotIn('Hillview', self.text(self.live))

    def test_report_numbers_the_operations_skip_takes(self):
        d = self.two_op_run('R1')
        self.assertEqual(self.cmd('report', 'R1')[0], 0)
        rep = O.read(os.path.join(d, 'REPORT.md'))
        self.assertIn('1. append pilot.md: The pilot partner is Lakeside.', rep)
        self.assertIn('2. append pilot.md: The demo is in Hillview.', rep)

    def test_skip_refusals_make_no_backup(self):
        d = self.two_op_run('R1')
        for bad, msg in (('3', 'no operation 3'), ('0', 'no operation 0'), ('1,2', 'every operation skipped'), ('x', 'operation numbers'),
                         ('', 'operation numbers'), (',', 'operation numbers'), (' ', 'operation numbers'), ('+2', 'operation numbers'),
                         ('\u0662', 'operation numbers'), ('-1', 'operation numbers')):
            code, out = self.cmd('swap', 'R1', '--skip', bad)
            self.assertEqual(code, 2, bad)
            self.assertIn(msg, out['error'])
        self.assertFalse(os.path.exists(os.path.join(d, 'live-backup')))
        self.assertFalse(os.path.exists(os.path.join(d, 'swap.json')))


if __name__ == '__main__':
    unittest.main()
