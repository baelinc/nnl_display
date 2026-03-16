#!/usr/bin/env python3
"""
web_server.py  —  NNL FPP Plugin Settings UI  v1.0.0
======================================================
Serves the settings web UI at http://<Pi-IP>:8180

Endpoints:
  GET  /                        → settings UI (web/index.html)
  GET  /nnl/api/settings        → current settings.json
  POST /nnl/api/settings        → save settings.json
  GET  /nnl/api/status          → live display status + counts
  POST /nnl/api/restart         → restart display process
  GET  /nnl/api/test-connection → test WordPress API
  POST /nnl/api/presets         → save presets
  GET  /nnl/preview.jpg         → latest rendered frame as JPEG (for live preview)
  GET  /nnl/api/check-update    → check GitHub for newer version
"""

import http.server
import json
import os
import sys
import signal
import subprocess
import threading
import time
import urllib.request
import urllib.error
import logging

log = logging.getLogger('nnl.web')

PLUGIN_DIR    = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PLUGIN_DIR, 'settings.json')
WEB_DIR       = os.path.join(PLUGIN_DIR, 'web')
PREVIEW_FILE  = '/tmp/nnl_preview.jpg'
STATUS_FILE   = '/tmp/nnl_status.json'
PORT          = 8180

CURRENT_VERSION = '1.0.0'


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    data.pop('_comment', None)
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(data, f, indent=4)


def get_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {'running': False}


def restart_display():
    pid_file = '/tmp/nnl_display.pid'
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
    except Exception:
        pass

    script = os.path.join(PLUGIN_DIR, 'nnl_display.py')
    log_f  = open('/tmp/nnl_display.log', 'a')
    proc   = subprocess.Popen(
        ['sudo', sys.executable, script],
        stdout=log_f, stderr=log_f,
        start_new_session=True,
    )
    with open(pid_file, 'w') as f:
        f.write(str(proc.pid))
    return proc.pid


def test_connection(settings):
    api  = settings.get('api', {})
    url  = api.get('url', '')
    key  = api.get('key', '')
    if not url:
        return {'ok': False, 'error': 'No API URL configured'}
    try:
        req = urllib.request.Request(url + '?limit=1')
        if key:
            req.add_header('X-NNL-Key', key)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        c = data.get('counts', {})
        return {'ok': True, 'naughty': c.get('naughty', 0), 'nice': c.get('nice', 0)}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'error': f'HTTP {e.code}: {e.reason}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def check_github_update(settings):
    """Check GitHub releases for a newer version."""
    gh     = settings.get('github', {})
    user   = gh.get('user', '')
    repo   = gh.get('repo', '')
    if not user or not repo or user == 'YOUR-USERNAME':
        return {'available': False, 'current': CURRENT_VERSION, 'error': 'GitHub not configured'}

    url = f'https://api.github.com/repos/{user}/{repo}/releases/latest'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'NNL-FPP-Updater'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        remote = data.get('tag_name', '').lstrip('v')
        if not remote:
            return {'available': False, 'current': CURRENT_VERSION}

        def ver_tuple(v):
            try: return tuple(int(x) for x in v.split('.'))
            except: return (0,)

        available = ver_tuple(remote) > ver_tuple(CURRENT_VERSION)
        return {
            'available':  available,
            'current':    CURRENT_VERSION,
            'latest':     remote,
            'changelog':  data.get('body', ''),
            'release_url':data.get('html_url', ''),
        }
    except Exception as e:
        return {'available': False, 'current': CURRENT_VERSION, 'error': str(e)}


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, mime='text/html; charset=utf-8'):
        try:
            with open(path, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', len(body))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        p = self.path.split('?')[0]

        if p in ('/', '/index.html'):
            self._file(os.path.join(WEB_DIR, 'index.html'))

        elif p == '/nnl/api/settings':
            self._json(load_settings())

        elif p == '/nnl/api/status':
            status = get_status()
            # Attach update info if cached
            upd = load_settings().get('_update_cache')
            if upd:
                status['update'] = upd
            self._json(status)

        elif p == '/nnl/api/check-update':
            result = check_github_update(load_settings())
            # Cache the result in settings
            s = load_settings()
            s['_update_cache'] = result
            try: save_settings(s)
            except Exception: pass
            self._json(result)

        elif p == '/nnl/preview.jpg':
            # Grab the current frame from the shared canvas — rendered on demand,
            # no background timer, zero overhead when the page is not open.
            try:
                from nnl_display import get_preview_canvas
                from io import BytesIO
                img = get_preview_canvas()
                if img is None:
                    self.send_error(503, 'Display not running')
                    return
                # Scale 4× with NEAREST so LED pixels are crisp
                preview = img.resize(
                    (img.width * 4, img.height * 4), resample=0
                )
                buf = BytesIO()
                preview.save(buf, format='JPEG', quality=85)
                body = buf.getvalue()
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', len(body))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
            except ImportError:
                self.send_error(503, 'Display module not loaded')
            except Exception as e:
                self.send_error(500, str(e))

        else:
            local = os.path.join(WEB_DIR, p.lstrip('/'))
            if os.path.isfile(local):
                self._file(local)
            else:
                self.send_error(404)

    def do_POST(self):
        p      = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length else b''

        if p == '/nnl/api/settings':
            try:
                data = json.loads(body)
                save_settings(data)
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 400)

        elif p == '/nnl/api/restart':
            try:
                pid = restart_display()
                self._json({'ok': True, 'pid': pid})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)

        elif p == '/nnl/api/presets':
            try:
                presets = json.loads(body)
                s = load_settings()
                s['presets'] = presets
                save_settings(s)
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 400)

        else:
            self.send_error(404)


def run(port=PORT):
    server = http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler)
    log.info('NNL Settings UI: http://0.0.0.0:%d', port)
    server.serve_forever()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [NNL.web] %(message)s')
    run()
