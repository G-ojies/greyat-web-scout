"""Resident process: loads the model once, then answers queries over a unix socket (one JSON line each way)."""
import json
import os
import socket
import threading
import time
import traceback

from . import llm as llm_mod
from . import pipeline

SOCK = os.environ.get('AGENT_SOCK', '/tmp/agent.sock')
READY = SOCK + '.ready'


def serve():
    model = llm_mod.load()
    lock = threading.Lock()  # one generation at a time on the GPU
    locked = type('Locked', (), {'chat': lambda self, *a, **k: _locked_chat(model, lock, *a, **k)})()
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(16)
    with open(READY, 'w') as f:
        f.write(json.dumps({'model': getattr(model, 'name', '?'), 'at': time.time()}))
    print('agent server ready', getattr(model, 'name', '?'), flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_handle, args=(conn, locked), daemon=True).start()


def _locked_chat(model, lock, *a, **k):
    with lock:
        return model.chat(*a, **k)


def _handle(conn, model):
    with conn:
        try:
            buf = b''
            while not buf.endswith(b'\n'):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            req = json.loads(buf or b'{}')
            if req.get('ping'):
                conn.sendall(b'{"ok": true}\n')
                return
            trace = {}
            ans, src, conf = pipeline.answer(req['query'], model, req.get('url'), req.get('budget', 24.0), trace)
            out = {'answer': ans, 'source': src, 'confidence': conf, 'trace': trace}
        except Exception as e:
            out = {'answer': '', 'source': None, 'confidence': 0.0, 'error': repr(e)[:300],
                   'tb': traceback.format_exc()[-800:]}
        try:
            conn.sendall((json.dumps(out) + '\n').encode())
        except OSError:
            pass


def ask(req, timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    with s:
        s.sendall((json.dumps(req) + '\n').encode())
        buf = b''
        while not buf.endswith(b'\n'):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


def alive():
    if not os.path.exists(READY):
        return False
    try:
        return bool(ask({'ping': True}, 2).get('ok'))
    except (OSError, ValueError):
        return False
