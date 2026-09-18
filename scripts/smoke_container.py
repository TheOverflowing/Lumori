"""Isolated deployment smoke test. No external AI calls or existing account data."""
import argparse
import json
import subprocess
import time
import urllib.request
import http.cookiejar
import uuid


def run(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='lumori:full')
    args = parser.parse_args()
    name = 'lumori-smoke-' + uuid.uuid4().hex[:10]
    volume = name + '-data'
    try:
        run('volume', 'create', volume)
        run('run', '-d', '--name', name, '-p', '127.0.0.1::8765', '-v', volume+':/srv/lumori/data', args.image)
        address = run('port', name, '8765/tcp').splitlines()[0]
        base = 'http://' + address
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def request(path, data=None, csrf=None):
            headers = {'Content-Type':'application/json', 'Origin':base}
            if csrf: headers['X-CSRF-Token'] = csrf
            req = urllib.request.Request(base+path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
            with opener.open(req, timeout=10) as response:
                return json.load(response)
        def healthy():
            for _ in range(60):
                try:
                    assert request('/healthz') == {'status':'ok'}
                    return
                except Exception:
                    time.sleep(1)
            raise RuntimeError('Container did not become healthy')
        healthy()
        request('/api/auth/register', {'email':'smoke@example.test','password':'isolated smoke test password','display_name':'Smoke'})
        session = request('/api/auth/session')
        course = request('/api/courses', {'name':'Deployment persistence'}, session['csrf_token'])
        run('restart', name)
        healthy()
        courses = request('/api/courses')
        assert any(item['id'] == course['id'] for item in courses)
        print('PASS: container startup, registration, CSRF, authenticated course write, restart persistence')
    finally:
        subprocess.run(['docker','rm','-f',name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker','volume','rm',volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
