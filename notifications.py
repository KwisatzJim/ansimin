"""Durable, asynchronous ntfy alerts with deliberately minimal public messages."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Notifications:
    def __init__(self, state, sender=None, clock=time.time):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state / 'notifications.sqlite3'
        self.config_path = self.state / 'notifications.json'
        self.sender = sender or self.publish
        self.clock = clock
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.thread = None
        self.storage_error = False
        with self.database() as db:
            os.chmod(self.path, 0o600)
            db.execute('CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, title TEXT NOT NULL, '
                       'message TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, '
                       'next_attempt REAL NOT NULL, created REAL NOT NULL, sent REAL, error TEXT)')
            db.execute("UPDATE alerts SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END, "
                       "error='Delivery interrupted; provider acceptance is unknown' WHERE status='sending'")

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def config(self):
        if not self.config_path.exists():
            return {'server':'https://ntfy.sh', 'topic':'', 'enabled':False}
        config = json.loads(self.config_path.read_text())
        if (config.get('server') != 'https://ntfy.sh' or type(config.get('enabled')) is not bool or
            not isinstance(config.get('topic'), str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', config['topic'])):
            raise ValueError('Invalid ntfy configuration')
        return {key:config[key] for key in ('server','topic','enabled')}

    def configure(self, topic, enabled=True):
        if not isinstance(topic, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', topic) or type(enabled) is not bool:
            raise ValueError('Invalid ntfy topic or enabled setting')
        with self.lock:
            config = {'server':'https://ntfy.sh', 'topic':topic, 'enabled':enabled}
            # Reconfiguration should not redirect old queued events to a different topic.
            with self.database() as db:
                if not enabled or self.config().get('topic') != topic:
                    db.execute("UPDATE alerts SET status='suppressed' WHERE status='pending'")
            temporary = self.state / ('.notifications-' + secrets.token_hex(8))
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as file:
                    json.dump(config, file)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.config_path)
                descriptor = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temporary.unlink(missing_ok=True)
            return self.status()

    def enabled(self, value):
        config = self.config()
        if not config['topic']:
            raise ValueError('Configure an ntfy topic on the server first')
        return self.configure(config['topic'], value)

    def status(self):
        config = self.config()
        with self.database() as db:
            counts = dict(db.execute('SELECT status,count(*) FROM alerts GROUP BY status').fetchall())
            last_sent = db.execute('SELECT max(sent) FROM alerts').fetchone()[0]
            failure = db.execute("SELECT error FROM alerts WHERE error IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
        return {**config, 'pending':counts.get('pending',0)+counts.get('sending',0),
                'failed':counts.get('failed',0), 'last_sent':last_sent,
                'last_error':failure[0] if failure else None, 'storage_error':self.storage_error}

    def enqueue(self, identity, title, message):
        try:
            with self.lock:
                config = self.config()
                if not config['enabled'] or not config['topic']:
                    return False
                with self.database() as db:
                    db.execute("INSERT OR IGNORE INTO alerts(id,title,message,status,attempts,next_attempt,created) "
                               "VALUES(?,?,?,'pending',0,?,?)",
                               (identity, title, message, self.clock(), self.clock()))
            self.storage_error = False
            return True
        except (OSError, ValueError, sqlite3.Error):
            self.storage_error = True
            return False

    def run(self, job):
        if job.get('status') not in ('failed','interrupted'):
            return
        identity = job.get('id','')
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{24}', identity):
            return
        status = job['status']
        kind = 'scheduled Ansible run' if job.get('schedule_id') else 'Ansible run'
        message = f'{"A" if job.get("schedule_id") else "An"} {kind} {"was interrupted by a restart" if status == "interrupted" else "failed"}. '
        message += f'Reference: {identity[:12]}. Open ansimin for details.'
        self.enqueue(f'run:{identity}:{status}', 'Ansimin: run ' + status, message)

    def schedule(self, identity, status, attempt):
        descriptions = {'skipped_busy':'was skipped because another job was running',
                        'failed_to_start':'could not start; check its target, playbook and credential',
                        'missed_offline':'missed its scheduled time',
                        'interrupted_dispatch':'was interrupted before its start could be confirmed'}
        if status not in descriptions or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{24}', identity):
            return
        reference = identity[:12]
        self.enqueue(f'schedule:{identity}:{attempt}:{status}', 'Ansimin: schedule needs attention',
                     f'An ansimin schedule {descriptions[status]}. Reference: {reference}. Open ansimin for details.')

    def worker_error(self):
        self.enqueue('scheduler-error:' + str(int(self.clock())), 'Ansimin: scheduler error',
                     'The ansimin scheduler encountered an error. Open ansimin and check the controller service.')

    def test(self):
        identity = 'test:' + secrets.token_hex(12)
        if not self.enqueue(identity, 'Ansimin: test notification', 'Ansimin notification delivery test. No Ansible jobs were run.'):
            raise ValueError('Notifications are disabled or could not be queued')
        return {'queued':True, 'id':identity}

    @staticmethod
    def publish(config, alert):
        request = urllib.request.Request(config['server'] + '/' + config['topic'],
            data=alert['message'].encode(), method='POST',
            headers={'Content-Type':'text/plain; charset=utf-8', 'Title':alert['title'],
                     'Priority':'4' if not alert['id'].startswith('test:') else '3',
                     'Tags':'warning' if not alert['id'].startswith('test:') else 'test_tube',
                     'User-Agent':'ansimin'})
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=10) as response:
            data = json.loads(response.read(8192))
            if not 200 <= response.status < 300 or data.get('event') != 'message' or not isinstance(data.get('id'), str):
                raise ValueError('ntfy did not confirm acceptance')

    def deliver_one(self):
        with self.lock:
            config = self.config()
            if not config['enabled']:
                return False
            with self.database() as db:
                row = db.execute("SELECT * FROM alerts WHERE status='pending' AND next_attempt<=? ORDER BY rowid LIMIT 1", (self.clock(),)).fetchone()
                if row is None:
                    return False
                alert = dict(row)
                db.execute("UPDATE alerts SET status='sending',attempts=attempts+1 WHERE id=?", (alert['id'],))
        error = None
        retryable = True
        try:
            self.sender(config, alert)
        except urllib.error.HTTPError as failure:
            error = f'ntfy returned HTTP {failure.code}'
            retryable = failure.code == 429 or failure.code >= 500
            failure.close()
        except Exception:
            error = 'ntfy delivery was not confirmed (network, TLS or response error)'
        attempts = alert['attempts'] + 1
        with self.lock, self.database() as db:
            if error is None:
                db.execute("UPDATE alerts SET status='sent',sent=?,error=NULL WHERE id=?", (self.clock(),alert['id']))
                self.storage_error = False
            else:
                current_config = self.config()
                retry = retryable and attempts < 3 and current_config['enabled'] and current_config['topic'] == config['topic']
                db.execute('UPDATE alerts SET status=?,error=?,next_attempt=? WHERE id=?',
                           ('pending' if retry else 'failed',error,self.clock()+(60 if attempts==1 else 300),alert['id']))
        return True

    def start(self):
        if self.thread is not None:
            return
        def loop():
            while not self.event.is_set():
                try:
                    if self.deliver_one():
                        continue
                except Exception:
                    self.storage_error = True
                self.event.wait(5)
        self.thread = threading.Thread(target=loop, name='ansimin-notifications', daemon=True)
        self.thread.start()

    def stop(self):
        self.event.set()
        if self.thread:
            self.thread.join(timeout=2)
