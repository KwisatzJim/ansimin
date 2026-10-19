"""Read-only production schedule checks; execute the smoke playbook only in isolation."""
import http.client
import json
from pathlib import Path
import ssl
import time


def check_schedules(host='127.0.0.1', port=8443, state=(Path.home() / '.local/state/ansimin'), scheduler=None):
    conn = http.client.HTTPSConnection(host, port,
        context=ssl.create_default_context(cafile=str(state / 'cert.pem')), timeout=45)
    cookie = None
    def request(method, path, body=None, origin=True):
        headers = {'Content-Type':'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        if origin:
            headers['Origin'] = f'https://{host}:{port}'
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    def wait(job_id):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status, _, data = request('GET', '/api/history/' + job_id)
            assert status == 200
            job = data['job']
            if job['status'] not in ('running','cancelling'):
                assert job['status'] == 'succeeded', job
                assert 'changed=0' in job['output']
                return job
            time.sleep(.2)
        raise AssertionError('Scheduled smoke test did not finish')

    assert request('GET', '/api/schedules')[0] == 401
    for action in ('save','toggle','remove','run'):
        assert request('POST', '/api/schedules/' + action, {})[0] == 401
    status, headers, _ = request('POST', '/api/login', {'password':(state/'initial-password').read_text().strip()})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    status, _, initial = request('GET', '/api/schedules')
    assert status == 200 and 'schedules' in initial
    assert request('POST', '/api/schedules/save', {}, origin=False)[0] == 403
    if scheduler is not None:
        # This branch is called only for the temporary isolated server.
        secret = 'dummy-secret-for-scheduled-tests'
        assert request('POST', '/api/scheduler-credential/save', {'password':secret,'confirm_password':secret})[0] == 200
        spec = dict(name='Scheduled smoke test', playbook='smoke-test.yml', target='pi', cadence='daily',
                    time='23:59', timezone='America/Chicago', weekday=0, mode='run', use_credential=True)
        status, _, saved = request('POST', '/api/schedules/save', spec)
        assert status == 200 and not saved['enabled'], saved
        status, _, data = request('POST', '/api/schedules/run', saved)
        assert status == 202, data
        job = wait(data['job']['id'])
        assert job['schedule_id'] == saved['id'] and job['schedule_name'] == spec['name']
        assert secret not in json.dumps(job)
        _, _, current = request('GET', '/api/schedules')
        assert not current['schedules'][0]['enabled']
        status, _, enabled = request('POST', '/api/schedules/toggle', dict(saved, enabled=True))
        assert status == 200, enabled
        assert request('POST', '/api/schedules/toggle', dict(saved, enabled=False))[0] == 409
        # Advance only the isolated scheduler clock, then exercise its actual background loop.
        scheduler.clock = lambda: enabled['next_run']
        scheduler.start()
        deadline = time.monotonic() + 45
        timed_id = None
        while time.monotonic() < deadline:
            _, _, current = request('GET', '/api/schedules')
            timed_id = current['schedules'][0]['last_run_id']
            if timed_id and timed_id != job['id']:
                break
            time.sleep(.2)
        assert timed_id and timed_id != job['id']
        timed_job = wait(timed_id)
        assert timed_job['schedule_id'] == saved['id'] and secret not in json.dumps(timed_job)
        scheduler.stop()
        assert request('POST', '/api/schedules/toggle', dict(enabled, enabled=False))[0] == 200
        _, _, current = request('GET', '/api/schedules')
        assert request('POST', '/api/schedules/remove', current['schedules'][0])[0] == 200
        assert request('GET', '/api/history/' + timed_id)[0] == 200
        assert secret.encode() not in scheduler.path.read_bytes()
        request('POST', '/api/scheduler-credential/remove', {})
        print('Isolated schedules passed: paused creation, stored-credential run-once, real timed dispatch, history tags, stale edits, pause, and deletion.')
    request('POST', '/api/logout', {})
    print('Schedule HTTPS checks passed: authenticated list/actions and origin protection.')
    conn.close()


if __name__ == '__main__':
    check_schedules()
