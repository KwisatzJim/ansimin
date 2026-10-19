from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from schedules import Scheduler, next_occurrence


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


class FakeCredential:
    configured = True
    def status(self):
        return {'configured': self.configured}
    def load(self):
        if not self.configured:
            raise ValueError('credential unavailable')
        return 'dummy-scheduled-password'


class FakeRuns:
    history = None
    busy = False
    def __init__(self):
        self.launched = []
    def prepare(self, payload):
        if payload.get('playbook') != 'safe.yml' or payload.get('target') not in ('pi', 'all'):
            raise ValueError('invalid playbook or target')
    def snapshot(self):
        return {'status':'running'} if self.busy else None
    def start(self, payload, schedule=None):
        self.prepare(payload)
        if self.busy:
            raise RuntimeError('busy')
        self.launched.append((dict(payload), dict(schedule)))
        return {'id': f'{len(self.launched):024x}'}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = timestamp('2026-10-16T08:00:00-05:00')
        self.runs, self.credential = FakeRuns(), FakeCredential()
        self.scheduler = Scheduler(Path(self.temp.name), self.runs, self.credential, clock=lambda: self.now)
        self.spec = dict(name='Safe schedule', playbook='safe.yml', target='pi', mode='run',
                         cadence='daily', time='09:00', timezone='America/Chicago', weekday=0, use_credential=True)

    def test_schedules_default_to_paused_and_do_not_run(self):
        saved = self.scheduler.save(self.spec)
        self.assertFalse(saved['enabled'])
        self.assertIsNone(saved['next_run'])
        self.now += 86400
        self.scheduler.tick()
        self.assertFalse(self.runs.launched)

    def test_due_run_uses_credential_exactly_once_and_advances_next_time(self):
        saved = self.scheduler.save(dict(self.spec, enabled=True))
        self.now = saved['next_run']
        self.scheduler.tick()
        self.scheduler.tick()
        self.assertEqual(len(self.runs.launched), 1)
        payload, source = self.runs.launched[0]
        self.assertEqual(payload['sudo_password'], 'dummy-scheduled-password')
        self.assertEqual(source['id'], saved['id'])
        current = self.scheduler.list()['schedules'][0]
        self.assertGreater(current['next_run'], self.now)
        self.assertEqual(current['last_status'], 'started')
        self.assertNotIn(b'dummy-scheduled-password', self.scheduler.path.read_bytes())

    def test_busy_job_skips_without_queue_or_retry(self):
        saved = self.scheduler.save(dict(self.spec, enabled=True))
        self.runs.busy = True
        self.now = saved['next_run']
        with patch.object(self.credential, 'load', side_effect=AssertionError('Do not decrypt for a busy job slot')):
            self.scheduler.tick()
        self.runs.busy = False
        self.scheduler.tick()
        self.assertFalse(self.runs.launched)
        self.assertEqual(self.scheduler.list()['schedules'][0]['last_status'], 'skipped_busy')

    def test_restart_skips_overdue_time_and_preserves_schedule(self):
        saved = self.scheduler.save(dict(self.spec, enabled=True))
        self.now = saved['next_run'] + 10
        restored = Scheduler(Path(self.temp.name), self.runs, self.credential, clock=lambda: self.now)
        restored.tick()
        self.assertFalse(self.runs.launched)
        current = restored.list()['schedules'][0]
        self.assertEqual(current['id'], saved['id'])
        self.assertEqual(current['last_status'], 'missed_offline')
        self.assertGreater(current['next_run'], self.now)

    def test_late_tick_and_missing_credential_do_not_launch_or_retry(self):
        saved = self.scheduler.save(dict(self.spec, enabled=True))
        self.now = saved['next_run'] + 91
        self.scheduler.tick()
        self.assertEqual(self.scheduler.list()['schedules'][0]['last_status'], 'missed_offline')
        self.now = self.scheduler.list()['schedules'][0]['next_run']
        self.credential.configured = False
        self.scheduler.tick()
        self.assertEqual(self.scheduler.list()['schedules'][0]['last_status'], 'failed_to_start')
        self.assertFalse(self.runs.launched)

    def test_run_once_does_not_enable_schedule(self):
        saved = self.scheduler.save(self.spec)
        job = self.scheduler.run_now(saved)
        self.assertTrue(job['id'])
        self.assertFalse(self.scheduler.list()['schedules'][0]['enabled'])
        self.assertEqual(len(self.runs.launched), 1)

    def test_pause_edit_and_delete_reject_stale_versions(self):
        saved = self.scheduler.save(self.spec)
        enabled = self.scheduler.toggle(dict(saved, enabled=True))
        self.assertTrue(enabled['enabled'])
        with self.assertRaises(RuntimeError):
            self.scheduler.toggle(dict(saved, enabled=False))
        edited = self.scheduler.save(dict(enabled, name='Renamed', time='10:30'))
        paused = self.scheduler.toggle(dict(edited, enabled=False))
        self.scheduler.remove(paused)
        self.assertEqual(self.scheduler.list()['schedules'], [])

    def test_schedule_can_omit_credential_and_cannot_enable_missing_credential(self):
        self.credential.configured = False
        with self.assertRaises(ValueError):
            self.scheduler.save(dict(self.spec, enabled=True))
        saved = self.scheduler.save(dict(self.spec, enabled=True, use_credential=False))
        self.now = saved['next_run']
        self.scheduler.tick()
        self.assertNotIn('sudo_password', self.runs.launched[0][0])

    def test_invalid_configuration_is_rejected(self):
        for change in ({'time':'24:00'}, {'cadence':'hourly'}, {'timezone':'invalid/timezone'},
                       {'playbook':'../secret.yml'}, {'target':'pi;id'}, {'mode':'syntax'},
                       {'weekday':True}, {'enabled':'yes'}, {'name':''}):
            with self.assertRaises(ValueError):
                self.scheduler.save(dict(self.spec, **change))
        self.assertEqual(self.scheduler.list()['schedules'], [])


class ScheduleTimeTests(unittest.TestCase):
    def test_weekly_local_time(self):
        spec = dict(cadence='weekly', weekday=0, time='09:00', timezone='America/Chicago')
        self.assertEqual(next_occurrence(spec, timestamp('2026-10-16T08:00:00-05:00')),
                         timestamp('2026-10-19T09:00:00-05:00'))

    def test_spring_forward_nonexistent_time_is_skipped(self):
        spec = dict(cadence='daily', weekday=0, time='02:30', timezone='America/Chicago')
        self.assertEqual(next_occurrence(spec, timestamp('2026-03-07T03:00:00-06:00')),
                         timestamp('2026-03-09T02:30:00-05:00'))

    def test_fall_back_time_runs_only_once(self):
        spec = dict(cadence='daily', weekday=0, time='01:30', timezone='America/Chicago')
        first = next_occurrence(spec, timestamp('2026-11-01T00:00:00-05:00'))
        self.assertEqual(first, timestamp('2026-11-01T01:30:00-05:00'))
        self.assertEqual(next_occurrence(spec, first), timestamp('2026-11-02T01:30:00-06:00'))


if __name__ == '__main__':
    unittest.main()
