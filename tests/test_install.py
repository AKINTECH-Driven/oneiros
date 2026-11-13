"""Install-time guards: the current session is excluded under either env name, and a live start with no
finished dream and no --since is refused instead of reading the whole session history."""
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


def transcript(path, start, human=True):
    rec = {'type': 'user', 'timestamp': start, 'message': {'role': 'user', 'content': 'remember the budget is EUR 5'}}
    if human:
        rec['turnOrigin'] = 'human'
    with open(path, 'w') as f:
        f.write(json.dumps(rec, separators=(',', ':')) + '\n')  # real transcripts are compact JSON


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='oneiros-test-')
        self.project = os.path.join(self.tmp, 'project')
        self.mem = os.path.join(self.tmp, 'memory')
        os.makedirs(self.project)
        os.makedirs(self.mem)
        with open(os.path.join(self.mem, 'MEMORY.md'), 'w') as f:
            f.write('# Memory Index\n')
        for i, sid in enumerate(('aaaa1111', 'bbbb2222', 'cccc3333', 'dddd4444')):
            transcript(os.path.join(self.project, sid + '.jsonl'), '2026-11-1%dT10:00:00Z' % (2 + i % 2))
        self.state = os.path.join(self.tmp, 'state.json')
        self.patches = [mock.patch.object(O, 'STATE', self.state), mock.patch.object(O, 'RUNS', os.path.join(self.tmp, 'runs'))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, argv, env=None):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), redirect_stdout(out):
            for k in ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_SESSION_ID'):
                if env is None or k not in env:
                    os.environ.pop(k, None)
            code = O.main(argv)
        return code, json.loads(out.getvalue())


class TestFirstRunGuard(InstallCase):
    def test_live_start_without_history_is_refused(self):
        code, out = self.run_cmd(['start', '--memory', self.mem, '--project', self.project])
        self.assertEqual(code, 2)
        self.assertIn('--since', out['error'])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'runs')), 'no run folder may be made')
        self.assertFalse(os.path.exists(self.state), 'no lock may be taken')

    def test_live_start_with_junk_history_is_refused(self):
        for junk in (' ', '(none)', '-', 123, None, 'never'):
            O.save(self.state, {'last_finished': junk})
            code, out = self.run_cmd(['start', '--memory', self.mem, '--project', self.project])
            self.assertEqual(code, 2, (junk, out))
            self.assertIn('--since', out['error'])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'runs')))

    def test_live_start_with_since_runs(self):
        code, out = self.run_cmd(['start', '--memory', self.mem, '--project', self.project, '--since', '2026-11-13'])
        self.assertEqual(code, 0, out)
        self.assertEqual(out['sessions'], 2)

    def test_live_start_with_seeded_state_runs(self):
        O.save(self.state, {'last_finished': '2026-11-13T00:00:00+00:00'})
        code, out = self.run_cmd(['start', '--memory', self.mem, '--project', self.project])
        self.assertEqual(code, 0, out)
        self.assertEqual(out['sessions'], 2)

    def test_eval_start_without_history_still_runs(self):
        code, out = self.run_cmd(['start', '--eval', '--memory', self.mem, '--project', self.project])
        self.assertEqual(code, 0, out)
        self.assertEqual(out['sessions'], 4)


class TestCurrentSession(InstallCase):
    def test_both_env_names(self):
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'x1', 'CLAUDE_SESSION_ID': 'x2'}):
            self.assertEqual(O.current_session_ids(), {'x1', 'x2'})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(O.current_session_ids(), set())

    def test_start_excludes_the_running_session(self):
        O.save(self.state, {'last_finished': '2026-11-10T00:00:00+00:00'})
        code, out = self.run_cmd(['start', '--memory', self.mem, '--project', self.project],
                                 env={'CLAUDE_CODE_SESSION_ID': 'aaaa1111'})
        self.assertEqual(code, 0, out)
        self.assertEqual(out['sessions'], 3)
        run = O.load(os.path.join(self.tmp, 'runs', out['run'], 'run.json'), {})
        self.assertNotIn('aaaa1111', run['sessions'])

    def test_due_excludes_the_running_session(self):
        O.save(self.state, {'last_finished': '2026-11-10T00:00:00+00:00'})
        code, out = self.run_cmd(['due', '--project', self.project], env={'CLAUDE_CODE_SESSION_ID': 'aaaa1111'})
        self.assertEqual(code, 0, out)
        self.assertIn('3 sessions', out['why'])
        code, out = self.run_cmd(['due', '--project', self.project],
                                 env={'CLAUDE_CODE_SESSION_ID': 'aaaa1111', 'CLAUDE_SESSION_ID': 'bbbb2222'})
        self.assertEqual(code, 3, out)


if __name__ == '__main__':
    unittest.main()
