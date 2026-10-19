"""Run only against a dedicated test installation; executes only the no-change smoke playbook."""
import http.client
import json
from pathlib import Path
import ssl
import time


def check(host='127.0.0.1', port=8443, state=(Path.home() / '.local/state/ansimin')):
    context = ssl.create_default_context(cafile=str(state / 'cert.pem'))
    conn = http.client.HTTPSConnection(host, port, context=context, timeout=40)
    cookie = None

    def request(method, path, body=None, origin=True, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = f'https://{host}:{port}'
        if authenticated and cookie:
            headers['Cookie'] = cookie
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())

    payload = {'playbook': 'smoke-test.yml', 'target': 'pi', 'mode': 'run'}
    assert request('POST', '/api/run', payload)[0] == 401
    assert request('GET', '/api/run')[0] == 401
    assert request('GET', '/api/history')[0] == 401
    assert request('GET', '/api/history/' + 'a' * 24)[0] == 401
    status, headers, _ = request('POST', '/api/login', {
        'password': (state / 'initial-password').read_text().strip()})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';')[0]
    assert request('POST', '/api/run', payload, origin=False)[0] == 403
    assert request('POST', '/api/run', dict(payload, target='pi; id'))[0] == 400
    assert request('POST', '/api/run', dict(payload, playbook='../outside.yml'))[0] == 400
    # A dummy secret proves the real Ansible credential channel is accepted.
    # The debug-only smoke playbook never needs sudo and never changes a host.
    secret = 'dummy-secret-not-an-admin-password'
    status, _, data = request('POST', '/api/run', dict(payload, sudo_password=secret))
    assert status == 202, (status, data)
    job_id = data['job']['id']
    smoke_job_id = job_id
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        _, _, data = request('GET', '/api/run')
        job = data['job']
        assert job['id'] == job_id
        assert secret not in json.dumps(job)
        if job['status'] not in ('running', 'cancelling'):
            break
        time.sleep(.2)
    assert job['status'] == 'succeeded', job
    assert job['exit_code'] == 0
    assert 'No system changes were requested.' in job['output']
    assert 'changed=0' in job['output']
    payload = {'kind': 'adhoc', 'module': 'ansible.builtin.ping', 'target': 'pi', 'mode': 'run'}
    assert request('POST', '/api/run', dict(payload, module='ping;id'))[0] == 400
    for module, arguments, expected in [('ansible.builtin.ping', '', 'pong'),
                                         ('ansible.builtin.command', '/usr/bin/id -u', 'rc=0')]:
        status, _, data = request('POST', '/api/run', dict(payload, module=module, arguments=arguments))
        assert status == 202, (status, data)
        job_id = data['job']['id']
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            _, _, data = request('GET', '/api/run')
            job = data['job']
            assert job['id'] == job_id
            if job['status'] not in ('running', 'cancelling'):
                break
            time.sleep(.2)
        assert job['status'] == 'succeeded' and job['exit_code'] == 0, job
        assert expected in job['output'], job
        assert job['kind'] == 'adhoc' and job['module'] == module
    status, _, page = request('GET', '/api/history')
    assert status == 200 and len(page['runs']) >= 3
    assert all('output' not in entry for entry in page['runs'])
    status, _, historical = request('GET', '/api/history/' + smoke_job_id)
    assert status == 200 and historical['job']['status'] == 'succeeded'
    assert 'No system changes were requested.' in historical['job']['output']
    assert secret not in json.dumps(historical)
    assert request('GET', '/api/history/not-a-run')[0] == 404
    assert request('POST', '/api/logout', {})[0] == 200
    assert request('GET', '/api/run')[0] == 401
    assert request('GET', '/api/history')[0] == 401
    print('Live HTTPS checks passed: protected run APIs, origin checks, input validation,')
    print('real Ansible execution, anonymous password file, no secret in output, and changed=0.')
    print('Ad-hoc checks passed: real Ansible ping/pong and read-only id command.')
    print('History checks passed: authenticated list and details, preserved output, and secret exclusion.')


if __name__ == '__main__':
    check()
