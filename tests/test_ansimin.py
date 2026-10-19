import json
from pathlib import Path
import tempfile
import os
import sys
import time
import unittest
from unittest.mock import patch
import ansimin


class WorkspaceTests(unittest.TestCase):
    def test_hosts_without_explicit_variables_are_included(self):
        raw = {'_meta': {'hostvars': {}}, 'ungrouped': {'hosts': ['standalone']},
               'pis': {'hosts': ['pi', 'pi2']}, 'other': {'hosts': ['pi']}}
        data = ansimin.inventory_summary(raw)
        self.assertEqual([host['name'] for host in data['hosts']], ['pi', 'pi2', 'standalone'])
        self.assertTrue(all(host['connection'] == 'ssh' for host in data['hosts']))

    def test_preview_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('../secret.yml', '/tmp/secret.yml', 'inventory.ini', 'nested/file.yml'):
                with self.assertRaises(ValueError):
                    ansimin.playbook_path(root, name)
            (root / 'link.yml').symlink_to('/tmp/secret.yml')
            with self.assertRaises(ValueError):
                ansimin.playbook_path(root, 'link.yml')
            self.assertEqual(ansimin.playbook_path(root, 'update.yml'), root / 'update.yml')

    def test_inventory_uses_ansible_and_filters_variables(self):
        raw = {'_meta': {'hostvars': {'pi': {'ansible_user': 'jim', 'secret': 'hidden'}}},
               'all': {'children': ['pis']}, 'pis': {'hosts': ['pi']}}
        with patch('ansimin.subprocess.run') as run:
            run.return_value.stdout = json.dumps(raw)
            data = ansimin.inventory(Path('/workspace'), '/bin/ansible-inventory')
            self.assertEqual(data['groups'], [{'name': 'pis', 'hosts': ['pi'], 'children': []}])
            self.assertNotIn('secret', data['hosts'][0])
            self.assertEqual(run.call_args.args[0], ['/bin/ansible-inventory', '-i', '/workspace/inventory.ini', '--list'])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'safe.yml').write_text('---\n- hosts: all\n  tasks: []\n')
        self.executable = self.root / 'ansible-playbook'
        self.executable.write_text(f'#!{sys.executable}\nimport sys,time\nprint("started", flush=True)\ntime.sleep(1)\nprint("finished", flush=True)\n')
        self.executable.chmod(0o700)
        self.runs = ansimin.Runs(self.root, str(self.executable))
        self.inventory = patch('ansimin.inventory', return_value={'hosts': [{'name': 'pi'}], 'groups': []})
        self.inventory.start()
        self.addCleanup(self.inventory.stop)

    def start(self, **changes):
        payload = {'playbook': 'safe.yml', 'target': 'pi', 'mode': 'syntax'}
        payload.update(changes)
        return self.runs.start(payload)

    def wait(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.runs.snapshot()
            if job['status'] not in ('running', 'cancelling'):
                return job
            time.sleep(.02)
        self.runs.cancel(job['id'])
        self.fail('Run did not finish')

    def test_single_run_and_live_output(self):
        self.start()
        with self.assertRaises(RuntimeError):
            self.start()
        deadline = time.monotonic() + 3
        while 'started' not in self.runs.snapshot()['output'] and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIn('started', self.runs.snapshot()['output'])
        self.assertEqual(self.runs.snapshot()['status'], 'running')
        job = self.wait()
        self.assertEqual(job['status'], 'succeeded')
        self.assertEqual(job['exit_code'], 0)
        self.assertIn('finished', job['output'])

    def test_cancellation(self):
        self.executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(30)\n')
        job = self.start()
        self.runs.cancel(job['id'])
        self.assertEqual(self.wait()['status'], 'cancelled')

    def test_rejects_invalid_run_inputs(self):
        for changes in ({'target': 'pi; touch /tmp/oops'}, {'target': ''}, {'playbook': '../safe.yml'},
                        {'mode': 'anything'}, {'sudo_password': 'one\ntwo'}):
            with self.assertRaises(ValueError):
                self.start(**changes)

    def test_adhoc_arguments_stay_one_literal_argument(self):
        executable = self.root / 'ansible'
        executable.write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        executable.chmod(0o700)
        arguments = 'echo "hello"; $(touch /tmp/ansimin-unwanted) | cat'
        self.start(kind='adhoc', module='ansible.builtin.command', arguments=arguments,
                   mode='check', become=True)
        job = self.wait()
        self.assertEqual(job['status'], 'succeeded')
        argv = json.loads(job['output'])
        self.assertIn('--args=' + arguments, argv)
        self.assertIn('--check', argv)
        self.assertIn('--become', argv)
        self.assertEqual(job['kind'], 'adhoc')
        self.assertEqual(job['module'], 'ansible.builtin.command')

    def test_rejects_invalid_adhoc_input(self):
        base = {'kind': 'adhoc', 'module': 'ansible.builtin.ping', 'mode': 'run'}
        for changes in ({'module': '--extra-vars'}, {'module': 'ping;id'}, {'module': '../ping'},
                        {'arguments': ['id']}, {'arguments': 'x' * 2049}, {'become': 'false'},
                        {'mode': 'syntax'}, {'kind': 'other'}):
            with self.assertRaises(ValueError):
                self.start(**dict(base, **changes))

    def test_adhoc_and_playbook_cannot_overlap(self):
        executable = self.root / 'ansible'
        executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(1)\n')
        executable.chmod(0o700)
        job = self.start(kind='adhoc', module='ansible.builtin.ping', mode='run')
        with self.assertRaises(RuntimeError):
            self.start()
        self.runs.cancel(job['id'])
        self.assertEqual(self.wait()['status'], 'cancelled')

    @unittest.skipUnless(hasattr(os, 'memfd_create'), 'Anonymous credential files require Linux')
    def test_password_in_memory_only_and_output_redacted(self):
        self.runs.history = ansimin.History(self.root / 'state')
        self.executable.write_text(f'''#!{sys.executable}
import os,subprocess,sys,time
helper = sys.argv[sys.argv.index('--become-password-file')+1]
assert helper.endswith('/become_password.py')
password = subprocess.check_output([helper], text=True).strip()
assert password not in ' '.join(sys.argv)
assert password not in str(dict(os.environ))
sys.stdout.write(password[:4]); sys.stdout.flush(); time.sleep(.1)
sys.stdout.write(password[4:] + '\\n'); sys.stdout.flush()
''')
        self.start(sudo_password='dummy-secret-for-test')
        job = self.wait()
        self.assertEqual(job['status'], 'succeeded')
        self.assertNotIn('dummy-secret-for-test', str(job))
        self.assertIn('[REDACTED]', job['output'])
        saved = self.runs.history.get(job['id'])
        self.assertEqual(saved['output'], job['output'])
        self.assertNotIn('dummy-secret-for-test', self.runs.history.path.read_bytes().decode(errors='replace'))

    def test_finished_run_survives_new_runner_instance(self):
        self.runs.history = ansimin.History(self.root / 'state')
        self.start()
        job = self.wait()
        restored = ansimin.Runs(self.root, str(self.executable), ansimin.History(self.root / 'state'))
        self.assertEqual(restored.snapshot(), job)

    def test_run_does_not_launch_if_history_storage_fails(self):
        self.runs.history = ansimin.History(self.root / 'state')
        import sqlite3
        with patch.object(self.runs.history, 'record', side_effect=sqlite3.OperationalError('disk full')):
            with patch('ansimin.subprocess.Popen') as launch:
                with self.assertRaises(sqlite3.OperationalError):
                    self.start()
                launch.assert_not_called()
        self.assertIsNone(self.runs.snapshot())

    def test_storage_failure_during_run_preserves_execution_and_warns(self):
        self.runs.history = ansimin.History(self.root / 'state')
        original_record = self.runs.history.record
        import sqlite3
        calls = []
        def fail_after_launch(job):
            calls.append(job['id'])
            if len(calls) > 1:
                raise sqlite3.OperationalError('disk full')
            original_record(job)
        with patch.object(self.runs.history, 'record', side_effect=fail_after_launch):
            self.start()
            job = self.wait()
        self.assertEqual(job['status'], 'succeeded')
        self.assertTrue(job['history_error'])
        self.assertIn('finished', job['output'])


class InventoryEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.runs = ansimin.Runs(self.root, '/bin/ansible-playbook')
        self.editor = ansimin.InventoryEditor(self.root, self.state, self.runs)
        self.original = '# My hosts\n[pis]\npi ansible_connection=local\n'
        (self.root / 'inventory.ini').write_text(self.original)
        self.payload = {'content': self.original + '# Edited\n', 'revision': ansimin.revision(self.original.encode())}
        self.checker = patch('ansimin.subprocess.run')
        self.run = self.checker.start()
        self.addCleanup(self.checker.stop)
        self.run.return_value.returncode = 0
        self.run.return_value.stdout = json.dumps({'_meta': {'hostvars': {'pi': {}}}, 'pis': {'hosts': ['pi']}})
        self.run.return_value.stderr = ''

    def test_validate_is_read_only_and_uses_strict_ini_parser(self):
        result = self.editor.update(self.payload)
        self.assertTrue(result['valid'])
        self.assertEqual(result['hosts'][0]['name'], 'pi')
        self.assertEqual((self.root / 'inventory.ini').read_text(), self.original)
        self.assertFalse(list(self.root.glob('.ansimin-inventory-*')))
        env = self.run.call_args.kwargs['env']
        self.assertEqual(env['ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED'], 'True')
        self.assertEqual(env['ANSIBLE_INVENTORY_ENABLED'], 'ini')

    def test_save_preserves_comments_and_exact_backup(self):
        result = self.editor.update(self.payload, save=True)
        self.assertEqual((self.root / 'inventory.ini').read_text(), self.payload['content'])
        backup = self.state / 'backups' / result['backup']
        self.assertEqual(backup.read_text(), self.original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['revision'], ansimin.revision(self.payload['content'].encode()))
        with self.assertRaises(RuntimeError):
            self.editor.update(self.payload, save=True)

    def test_parser_failure_preserves_original(self):
        self.run.return_value.returncode = 1
        self.run.return_value.stderr = 'Failed to parse inventory'
        with self.assertRaises(ansimin.SyntaxProblem):
            self.editor.update(self.payload, save=True)
        self.assertEqual((self.root / 'inventory.ini').read_text(), self.original)
        self.assertFalse((self.state / 'backups').exists())
        self.assertFalse(list(self.root.glob('.ansimin-inventory-*')))

    def test_empty_parsed_inventory_is_rejected(self):
        self.run.return_value.stdout = json.dumps({'_meta': {'hostvars': {}}})
        with self.assertRaises(ansimin.SyntaxProblem):
            self.editor.update(self.payload, save=True)
        self.assertEqual((self.root / 'inventory.ini').read_text(), self.original)

    def test_external_edit_during_validation_is_preserved(self):
        from types import SimpleNamespace
        def change_file(*args, **kwargs):
            (self.root / 'inventory.ini').write_text('# terminal changed this file\n')
            return SimpleNamespace(returncode=0, stdout=json.dumps({'_meta': {'hostvars': {'pi': {}}}}), stderr='')
        self.run.side_effect = change_file
        with self.assertRaises(RuntimeError):
            self.editor.update(self.payload, save=True)
        self.assertEqual((self.root / 'inventory.ini').read_text(), '# terminal changed this file\n')

    def test_inventory_edit_is_blocked_during_jobs(self):
        self.runs.job = {'status': 'running'}
        with self.assertRaises(RuntimeError):
            self.editor.update(self.payload, save=True)
        self.run.assert_not_called()

    def test_inventory_symlink_and_missing_revision_are_rejected(self):
        with self.assertRaises(ValueError):
            self.editor.update({'content': self.original}, save=True)
        (self.root / 'inventory.ini').unlink()
        (self.root / 'elsewhere.ini').write_text(self.original)
        (self.root / 'inventory.ini').symlink_to(self.root / 'elsewhere.ini')
        with self.assertRaises(ValueError):
            self.editor.read()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.history = ansimin.History(self.state)

    def job(self, number, status='succeeded'):
        return {'id': f'{number:024x}', 'kind': 'adhoc', 'module': 'ansible.builtin.ping',
                'playbook': None, 'target': 'pi', 'mode': 'run', 'status': status,
                'output': 'pong', 'truncated': False, 'started': number,
                'finished': number + 1 if status == 'succeeded' else None, 'exit_code': 0}

    def test_saved_fields_exclude_credential_and_command_data(self):
        job = self.job(1)
        job.update(sudo_password='private-secret', arguments='other-sensitive-argument', command=['secret'])
        self.history.record(job)
        saved = self.history.get(job['id'])
        self.assertNotIn('sudo_password', saved)
        self.assertNotIn('arguments', saved)
        self.assertNotIn('command', saved)
        self.assertNotIn(b'private-secret', self.history.path.read_bytes())
        self.assertEqual(self.history.path.stat().st_mode & 0o777, 0o600)

    def test_restarts_mark_unfinished_jobs_interrupted_and_keep_finished_jobs(self):
        self.history.record(self.job(1))
        self.history.record(self.job(2, 'running'))
        self.history.record(self.job(3, 'cancelling'))
        restored = ansimin.History(self.state)
        self.assertEqual(restored.get(f'{1:024x}')['status'], 'succeeded')
        for number in (2, 3):
            job = restored.get(f'{number:024x}')
            self.assertEqual(job['status'], 'interrupted')
            self.assertIsNotNone(job['finished'])
            self.assertIn('check the targets before retrying', job['output'])

    def test_history_pagination_has_no_duplicates_and_omits_output(self):
        for number in range(1, 31):
            self.history.record(self.job(number))
        page = self.history.page()
        self.assertEqual(len(page['runs']), 25)
        self.assertNotIn('output', page['runs'][0])
        # A newer run should not shift the next page of older entries.
        self.history.record(self.job(31))
        older = self.history.page(page['cursor'])
        ids = [job['id'] for job in page['runs'] + older['runs']]
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(set(ids)), 30)
        self.assertIsNone(older['cursor'])

    def test_invalid_history_queries_are_rejected(self):
        for cursor in ('-1', 'abc', '1 OR 1=1', '9' * 40):
            with self.assertRaises(ValueError):
                self.history.page(cursor)
        with self.assertRaises(ValueError):
            self.history.get('../credential.json')


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.runs = ansimin.Runs(self.root, '/bin/ansible-playbook')
        self.editor = ansimin.Playbooks(self.root, self.state, self.runs)
        self.checker = patch('ansimin.subprocess.run')
        self.run = self.checker.start()
        self.addCleanup(self.checker.stop)
        self.run.return_value.returncode = 0
        self.run.return_value.stdout = 'Syntax passed'
        self.run.return_value.stderr = ''
        self.payload = {'name': 'test.yml', 'content': '---\n- hosts: all\n  tasks: []\n', 'revision': None}

    def test_validation_does_not_create_playbook(self):
        self.assertTrue(self.editor.update(self.payload)['valid'])
        self.assertFalse((self.root / 'test.yml').exists())
        self.assertFalse(list(self.root.glob('.ansimin-*')))

    def test_save_backs_up_exact_previous_bytes_and_rejects_stale_revision(self):
        created = self.editor.update(self.payload, save=True)
        changed = dict(self.payload, revision=created['revision'], content='---\n- hosts: pi\n  tasks: []\n')
        saved = self.editor.update(changed, save=True)
        self.assertEqual((self.state / 'backups' / saved['backup']).read_text(), self.payload['content'])
        self.assertEqual((self.root / 'test.yml').read_text(), changed['content'])
        with self.assertRaises(RuntimeError):
            self.editor.update(changed, save=True)
        self.assertEqual(len(list((self.state / 'backups').iterdir())), 1)

    def test_invalid_syntax_preserves_original_without_backup(self):
        created = self.editor.update(self.payload, save=True)
        self.run.return_value.returncode = 4
        self.run.return_value.stderr = 'YAML syntax error'
        with self.assertRaises(ansimin.SyntaxProblem):
            self.editor.update(dict(self.payload, revision=created['revision'], content='[bad'), save=True)
        self.assertEqual((self.root / 'test.yml').read_text(), self.payload['content'])
        self.assertFalse((self.state / 'backups').exists())
        self.assertFalse(list(self.root.glob('.ansimin-*')))

    def test_external_edit_during_validation_is_preserved(self):
        created = self.editor.update(self.payload, save=True)
        def external_edit(*args, **kwargs):
            (self.root / 'test.yml').write_text('External terminal change')
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        self.run.side_effect = external_edit
        with self.assertRaises(RuntimeError):
            self.editor.update(dict(self.payload, revision=created['revision']), save=True)
        self.assertEqual((self.root / 'test.yml').read_text(), 'External terminal change')

    def test_creation_never_overwrites_existing_file(self):
        (self.root / 'test.yml').write_text('Existing')
        with self.assertRaises(RuntimeError):
            self.editor.update(self.payload, save=True)
        self.assertEqual((self.root / 'test.yml').read_text(), 'Existing')

    def test_rejects_active_job_and_unsafe_filename(self):
        with self.assertRaises(ValueError):
            self.editor.update(dict(self.payload, name='../escape.yml'), save=True)
        self.runs.job = {'status': 'running'}
        with self.assertRaises(RuntimeError):
            self.editor.update(self.payload, save=True)


if __name__ == '__main__':
    unittest.main()
