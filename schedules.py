"""Persistent daily/weekly playbook schedules. Skip missed runs and never queue overlaps."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def next_occurrence(spec, after):
    zone = ZoneInfo(spec['timezone'])
    date = datetime.fromtimestamp(after, zone).date()
    hour, minute = map(int, spec['time'].split(':'))
    for days in range(16):
        day = date + timedelta(days=days)
        if spec['cadence'] == 'weekly' and day.weekday() != spec['weekday']:
            continue
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        timestamp = local.timestamp()
        # Skip nonexistent spring-forward times. During fall-back use only the first occurrence.
        roundtrip = datetime.fromtimestamp(timestamp, zone)
        if roundtrip.replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if timestamp > after:
            return timestamp
    raise ValueError('Unable to calculate the next scheduled time')


class Scheduler:
    def __init__(self, state, runs, credential, clock=time.time, notifications=None):
        self.path = Path(state) / 'schedules.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs, self.credential, self.clock = runs, credential, clock
        self.notifications = notifications
        recovered = []
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.thread = None
        self.error = False
        with self.database() as db:
            os.chmod(self.path, 0o600)
            db.execute('CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, config TEXT NOT NULL, '
                       'version INTEGER NOT NULL, enabled INTEGER NOT NULL, next_run REAL NOT NULL, '
                       'last_status TEXT, last_run_id TEXT, last_attempt REAL)')
            now = self.clock()
            for row in db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run<=?', (now,)).fetchall():
                spec = json.loads(row['config'])
                db.execute("UPDATE schedules SET next_run=?,last_status='missed_offline',last_attempt=?,last_run_id=NULL WHERE id=?",
                           (next_occurrence(spec, now), row['next_run'], row['id']))
                recovered.append((row['id'], 'missed_offline', row['next_run']))
            for row in db.execute("SELECT id,last_attempt FROM schedules WHERE last_status='dispatching'").fetchall():
                recovered.append((row['id'], 'interrupted_dispatch', row['last_attempt']))
            db.execute("UPDATE schedules SET last_status='interrupted_dispatch' WHERE last_status='dispatching'")
        if self.notifications:
            for event in recovered:
                self.notifications.schedule(*event)

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def validate(self, payload):
        name = payload.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError('Enter a schedule name of at most 80 characters')
        cadence, clock, zone = payload.get('cadence'), payload.get('time'), payload.get('timezone')
        if cadence not in ('daily', 'weekly') or not isinstance(clock, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', clock):
            raise ValueError('Choose daily or weekly and a valid time')
        if not isinstance(zone, str) or len(zone) > 64:
            raise ValueError('Enter a timezone such as America/Chicago')
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Enter a recognized timezone such as America/Chicago') from None
        weekday = payload.get('weekday', 0)
        if type(weekday) is not int or not 0 <= weekday <= 6:
            raise ValueError('Choose a valid weekday')
        use_credential, enabled = payload.get('use_credential', True), payload.get('enabled', False)
        if type(use_credential) is not bool or type(enabled) is not bool or payload.get('mode') not in ('run', 'check'):
            raise ValueError('Invalid schedule options')
        spec = {'name': name.strip(), 'playbook': payload.get('playbook'), 'target': payload.get('target'),
                'mode': payload['mode'], 'cadence': cadence, 'time': clock, 'timezone': zone,
                'weekday': weekday, 'use_credential': use_credential}
        # Reuse the exact path, target and mode validation used by manual playbook runs.
        self.runs.prepare({'playbook': spec['playbook'], 'target': spec['target'], 'mode': spec['mode']})
        if enabled and use_credential and not self.credential.status()['configured']:
            raise ValueError('Configure the stored sudo credential before enabling this schedule')
        return spec, enabled

    @staticmethod
    def row_for(db, payload):
        identity, version = payload.get('id'), payload.get('version')
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{24}', identity):
            raise ValueError('Schedule not found')
        row = db.execute('SELECT * FROM schedules WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('Schedule not found')
        if type(version) is not int or row['version'] != version:
            raise RuntimeError('This schedule changed elsewhere. Refresh schedules before trying again.')
        return row

    def public(self, row):
        result = dict(json.loads(row['config']), id=row['id'], version=row['version'], enabled=bool(row['enabled']),
                      next_run=row['next_run'] if row['enabled'] else None, last_status=row['last_status'],
                      last_run_id=row['last_run_id'], last_attempt=row['last_attempt'])
        if row['last_run_id'] and self.runs.history:
            try:
                result['last_status'] = self.runs.history.get(row['last_run_id'])['status']
            except ValueError:
                pass
        return result

    def list(self):
        with self.lock, self.database() as db:
            return {'schedules': [self.public(row) for row in db.execute('SELECT * FROM schedules ORDER BY rowid DESC')],
                    'timezone': 'America/Chicago', 'scheduler_error': self.error}

    def save(self, payload):
        spec, enabled = self.validate(payload)
        now = self.clock()
        with self.lock, self.database() as db:
            if payload.get('id') is None:
                if db.execute('SELECT count(*) FROM schedules').fetchone()[0] >= 50:
                    raise ValueError('This version supports up to 50 schedules')
                identity = secrets.token_hex(12)
                db.execute('INSERT INTO schedules(id,config,version,enabled,next_run) VALUES(?,?,1,?,?)',
                           (identity, json.dumps(spec), enabled, next_occurrence(spec, now)))
            else:
                row = self.row_for(db, payload)
                identity = row['id']
                db.execute('UPDATE schedules SET config=?,version=version+1,enabled=?,next_run=? WHERE id=?',
                           (json.dumps(spec), enabled, next_occurrence(spec, now), identity))
            return self.public(db.execute('SELECT * FROM schedules WHERE id=?', (identity,)).fetchone())

    def toggle(self, payload):
        enabled = payload.get('enabled')
        if type(enabled) is not bool:
            raise ValueError('Invalid enabled setting')
        with self.lock, self.database() as db:
            row = self.row_for(db, payload)
            spec = json.loads(row['config'])
            if enabled:
                self.runs.prepare({'playbook': spec['playbook'], 'target': spec['target'], 'mode': spec['mode']})
                if spec['use_credential'] and not self.credential.status()['configured']:
                    raise ValueError('Configure the stored sudo credential before enabling this schedule')
            db.execute('UPDATE schedules SET enabled=?,version=version+1,next_run=? WHERE id=?',
                       (enabled, next_occurrence(spec, self.clock()), row['id']))
            return self.public(db.execute('SELECT * FROM schedules WHERE id=?', (row['id'],)).fetchone())

    def remove(self, payload):
        with self.lock, self.database() as db:
            row = self.row_for(db, payload)
            db.execute('DELETE FROM schedules WHERE id=?', (row['id'],))

    def dispatch(self, row, timed):
        spec = json.loads(row['config'])
        payload = {'playbook': spec['playbook'], 'target': spec['target'], 'mode': spec['mode']}
        try:
            current = self.runs.snapshot()
            if current and current['status'] in ('running', 'cancelling'):
                raise RuntimeError('busy')
            if spec['use_credential']:
                payload['sudo_password'] = self.credential.load()
            job = self.runs.start(payload, schedule={'id': row['id'], 'name': spec['name']})
        except RuntimeError:
            status = 'skipped_busy'
            job = None
        except Exception:
            # Never save exception text that may mention credentials or raw subprocess data.
            status = 'failed_to_start'
            job = None
        else:
            status = 'started'
        finally:
            payload.clear()
        with self.database() as db:
            db.execute('UPDATE schedules SET last_status=?,last_run_id=? WHERE id=?',
                       (status, job['id'] if job else None, row['id']))
            attempt = db.execute('SELECT last_attempt FROM schedules WHERE id=?', (row['id'],)).fetchone()
        if self.notifications and job is None:
            self.notifications.schedule(row['id'], status, attempt['last_attempt'] if attempt else None)
        if not timed and job is None:
            if status == 'skipped_busy':
                raise RuntimeError('Another job is running. Wait or cancel it before trying again.')
            raise ValueError('Unable to start schedule. Check its playbook, target and stored credential.')
        return job

    def run_now(self, payload):
        with self.lock:
            with self.database() as db:
                row = self.row_for(db, payload)
                db.execute("UPDATE schedules SET last_status='dispatching',last_attempt=?,last_run_id=NULL WHERE id=?", (self.clock(), row['id']))
            # Running once does not enable a paused schedule or move its next scheduled time.
            return self.dispatch(row, timed=False)

    def tick(self):
        now = self.clock()
        with self.lock:
            with self.database() as db:
                due = db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run,rowid', (now,)).fetchall()
            for row in due:
                now = self.clock()
                spec = json.loads(row['config'])
                missed = now - row['next_run'] > 90
                # Commit the next occurrence before launching: a crash must never replay a claimed run.
                with self.database() as db:
                    db.execute('UPDATE schedules SET next_run=?,last_status=?,last_attempt=?,last_run_id=NULL WHERE id=?',
                               (next_occurrence(spec, now), 'missed_offline' if missed else 'dispatching', now, row['id']))
                if not missed:
                    self.dispatch(row, timed=True)
                elif self.notifications:
                    self.notifications.schedule(row['id'], 'missed_offline', now)
        self.error = False

    def start(self):
        if self.thread is not None:
            return
        def loop():
            while not self.event.is_set():
                try:
                    self.tick()
                except Exception:
                    if self.notifications and not self.error:
                        self.notifications.worker_error()
                    self.error = True
                self.event.wait(10)
        self.thread = threading.Thread(target=loop, name='ansimin-scheduler', daemon=True)
        self.thread.start()

    def stop(self):
        self.event.set()
        if self.thread:
            self.thread.join(timeout=2)
