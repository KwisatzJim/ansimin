"""Read-only on production; use a captured ntfy transport in the isolated server."""
import http.client
import json
from pathlib import Path
import ssl
import time


def check_notifications(host='127.0.0.1',port=8443,state=(Path.home() / '.local/state/ansimin'),notifier=None,captured=None):
    conn = http.client.HTTPSConnection(host,port,context=ssl.create_default_context(cafile=str(state/'cert.pem')),timeout=45)
    cookie = None
    def request(method,path,body=None,origin=True):
        headers={'Content-Type':'application/json'}
        if cookie: headers['Cookie']=cookie
        if origin: headers['Origin']=f'https://{host}:{port}'
        conn.request(method,path,json.dumps(body) if body is not None else None,headers)
        response=conn.getresponse()
        return response.status,dict(response.getheaders()),json.loads(response.read())
    assert request('GET','/api/notifications')[0]==401
    assert request('POST','/api/notifications/test',{})[0]==401
    assert request('POST','/api/notifications/settings',{'enabled':True})[0]==401
    status,headers,_=request('POST','/api/login',{'password':(state/'initial-password').read_text().strip()})
    assert status==200
    cookie=headers['Set-Cookie'].split(';')[0]
    assert request('GET','/api/notifications')[0]==200
    assert request('POST','/api/notifications/test',{},origin=False)[0]==403
    if notifier is not None:
        notifier.configure('isolated-test-topic')
        assert request('POST','/api/notifications/settings',{'enabled':'yes'})[0]==400
        status,_,data=request('POST','/api/notifications/test',{})
        assert status==202 and data['queued']
        assert notifier.deliver_one()
        assert captured[-1]['title']=='Ansimin: test notification'
        payload={'kind':'adhoc','module':'ansible.builtin.command','arguments':'/usr/bin/false','target':'pi','mode':'run'}
        status,_,data=request('POST','/api/run',payload)
        assert status==202,data
        identity=data['job']['id']
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            _,_,data=request('GET','/api/run')
            if data['job']['status']=='failed': break
            time.sleep(.2)
        assert data['job']['status']=='failed',data
        # The worker finishes enqueueing just after it commits the completed run.
        while time.monotonic()<deadline and notifier.status()['pending']==0:
            time.sleep(.05)
        assert notifier.deliver_one()
        assert captured[-1]['title']=='Ansimin: run failed'
        assert identity[:12] in captured[-1]['message']
        assert '/usr/bin/false' not in captured[-1]['message']
        assert 'pi' not in captured[-1]['message']
        status,_,data=request('POST','/api/notifications/settings',{'enabled':False})
        assert status==200 and not data['enabled']
        assert request('POST','/api/notifications/test',{})[0]==400
        print('Isolated notifications passed: real failed Ansible job, captured generic alert, test API, and enable/disable.')
    request('POST','/api/logout',{})
    print('Notification HTTPS checks passed: authentication and origin protection.')
    conn.close()


if __name__=='__main__':
    check_notifications()
