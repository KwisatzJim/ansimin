import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import ansimin
from notifications import Notifications, NoRedirect
from schedules import Scheduler
from test_schedules import FakeRuns, FakeCredential


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.now = 1000
        self.sender = Mock()
        self.notes = Notifications(self.state, sender=self.sender, clock=lambda:self.now)
        self.notes.configure('isolated-test-topic')

    def job(self, status='failed'):
        return {'id':'a'*24, 'status':status, 'schedule_id':None,
                'playbook':'private-playbook.yml', 'target':'private-host',
                'output':'private-output-and-password', 'sudo_password':'private-password'}

    def test_failure_is_queued_without_network_and_minimizes_public_data(self):
        self.notes.run(self.job())
        self.sender.assert_not_called()
        self.assertEqual(self.notes.status()['pending'], 1)
        self.notes.deliver_one()
        config, alert = self.sender.call_args.args
        self.assertEqual(config['server'], 'https://ntfy.sh')
        self.assertEqual(config['topic'], 'isolated-test-topic')
        self.assertIn('Reference: aaaaaaaaaaaa', alert['message'])
        for private in ('private-playbook','private-host','private-output','private-password'):
            self.assertNotIn(private, json.dumps(alert))
            self.assertNotIn(private.encode(), self.notes.path.read_bytes())
        self.assertEqual(self.notes.status()['pending'], 0)
        self.assertEqual(self.notes.status()['last_sent'], self.now)

    def test_deduplication_survives_restart_and_success_and_cancel_are_quiet(self):
        for status in ('succeeded','cancelled'):
            self.notes.run(self.job(status))
        self.assertEqual(self.notes.status()['pending'], 0)
        self.notes.run(self.job())
        self.notes.run(self.job())
        self.notes.deliver_one()
        restored = Notifications(self.state, sender=self.sender, clock=lambda:self.now)
        restored.run(self.job())
        self.assertFalse(restored.deliver_one())
        self.sender.assert_called_once()

    def test_retries_are_bounded_and_exception_text_is_not_saved(self):
        self.sender.side_effect = urllib.error.URLError('private-password in an unsafe exception')
        self.notes.run(self.job())
        self.assertTrue(self.notes.deliver_one())
        self.assertFalse(self.notes.deliver_one())
        self.now += 60
        self.assertTrue(self.notes.deliver_one())
        self.now += 300
        self.assertTrue(self.notes.deliver_one())
        self.now += 10000
        self.assertFalse(self.notes.deliver_one())
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(self.notes.status()['failed'], 1)
        self.assertNotIn('private-password', json.dumps(self.notes.status()))
        self.assertNotIn(b'private-password', self.notes.path.read_bytes())

    def test_disabled_notifications_suppress_pending_alerts(self):
        self.notes.run(self.job())
        self.notes.enabled(False)
        self.assertEqual(self.notes.status()['pending'], 0)
        self.assertFalse(self.notes.deliver_one())
        self.notes.run(dict(self.job(), id='b'*24))
        self.notes.enabled(True)
        self.assertFalse(self.notes.deliver_one())
        self.notes.run(dict(self.job(), id='c'*24))
        self.assertTrue(self.notes.deliver_one())
        self.sender.assert_called_once()

    def test_permanent_provider_rejection_is_not_retried(self):
        self.sender.side_effect = urllib.error.HTTPError('https://ntfy.sh/isolated-test-topic',403,'Forbidden',{},None)
        self.notes.run(self.job())
        self.notes.deliver_one()
        self.assertEqual(self.notes.status()['failed'],1)
        self.assertEqual(self.notes.status()['last_error'],'ntfy returned HTTP 403')
        self.now += 10000
        self.assertFalse(self.notes.deliver_one())
        self.sender.assert_called_once()

    def test_restart_recovers_inflight_attempts_without_exceeding_limit(self):
        self.notes.run(self.job())
        with self.notes.database() as db:
            db.execute("UPDATE alerts SET status='sending',attempts=3")
        restored = Notifications(self.state, sender=self.sender, clock=lambda:self.now)
        self.assertFalse(restored.deliver_one())
        self.assertEqual(restored.status()['failed'], 1)

    def test_interrupted_history_recovery_emits_once(self):
        history = ansimin.History(self.state)
        job = dict(self.job('running'), output='private-output', truncated=False)
        history.record(job)
        ansimin.History(self.state, self.notes)
        ansimin.History(self.state, self.notes)
        self.assertEqual(self.notes.status()['pending'], 1)
        self.notes.deliver_one()
        self.assertIn('interrupted by a restart', self.sender.call_args.args[1]['message'])

    def test_schedule_failures_missed_times_and_busy_skips_emit_without_secrets(self):
        runs, credential = FakeRuns(), FakeCredential()
        scheduler = Scheduler(self.state, runs, credential, clock=lambda:self.now, notifications=self.notes)
        spec = dict(name='Private schedule name',playbook='safe.yml',target='pi',mode='run',
                    cadence='daily',time='09:00',timezone='UTC',weekday=0,use_credential=True,enabled=True)
        saved = scheduler.save(spec)
        runs.busy = True
        self.now = saved['next_run']
        scheduler.tick()
        self.notes.deliver_one()
        self.assertIn('another job was running', self.sender.call_args.args[1]['message'])
        runs.busy = False
        credential.configured = False
        self.now = scheduler.list()['schedules'][0]['next_run']
        scheduler.tick()
        self.notes.deliver_one()
        self.assertIn('could not start', self.sender.call_args.args[1]['message'])
        self.now = scheduler.list()['schedules'][0]['next_run'] + 10
        Scheduler(self.state,runs,credential,clock=lambda:self.now,notifications=self.notes)
        self.notes.deliver_one()
        self.assertIn('missed its scheduled time',self.sender.call_args.args[1]['message'])
        self.assertNotIn(b'Private schedule name',self.notes.path.read_bytes())
        self.assertNotIn(b'dummy-scheduled-password',self.notes.path.read_bytes())

    def test_invalid_topics_and_non_boolean_settings_are_rejected(self):
        for topic in ('','bad/topic','bad?token','a'*65):
            with self.assertRaises(ValueError):
                self.notes.configure(topic)
        with self.assertRaises(ValueError):
            self.notes.enabled('true')
        self.assertEqual(self.notes.path.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.notes.config_path.stat().st_mode & 0o777,0o600)

    def test_publish_uses_documented_ntfy_headers_timeout_and_no_redirects(self):
        response = Mock(status=200)
        response.read.return_value = b'{"event":"message","id":"accepted-test"}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        alert = {'id':'run:example:failed','title':'Ansimin: run failed','message':'Generic failure'}
        with patch('notifications.urllib.request.build_opener',return_value=opener):
            Notifications.publish(self.notes.config(),alert)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://ntfy.sh/isolated-test-topic')
        self.assertEqual(request.get_header('Title'),alert['title'])
        self.assertEqual(request.get_header('Priority'),'4')
        self.assertEqual(request.data,b'Generic failure')
        self.assertEqual(opener.open.call_args.kwargs['timeout'],10)
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',None,'https://other.example'))


if __name__ == '__main__':
    unittest.main()
