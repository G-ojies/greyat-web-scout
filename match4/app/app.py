"""AMD AI League Match 4 entry point: one web question in, one exact value and its source out.

    python3 /app/app.py --serve                     container CMD: load the model, answer over a socket
    python3 /app/app.py --index [...] | --warmup    block until the model is loaded (startup hook)
    python3 /app/app.py --query-id q01 --query "..." [--url https://...]

Writes /app/output/<query-id>_output.json. An empty answer is written first, so a crash or a
timeout still leaves a well-formed file.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Extra wheels live in /opt/deps; numpy goes last so the base image's own copy wins when present.
if os.path.isdir('/opt/deps'):
    sys.path.insert(1, '/opt/deps')
if os.path.isdir('/opt/deps-fallback'):
    sys.path.append('/opt/deps-fallback')
from agent import server  # noqa: E402

HARD_LIMIT = float(os.environ.get('AGENT_HARD_LIMIT', '28'))
OUT_DIR = os.environ.get('AGENT_OUTPUT', '/app/output')


def write(qid, answer='', source=None, confidence=0.0):
    os.makedirs(OUT_DIR, exist_ok=True)
    doc = {'answer': answer, 'citations': [source] if source else [], 'sources': [source] if source else [],
           'source_url': source or '', 'confidence': round(float(confidence), 2)}
    path = os.path.join(OUT_DIR, f'{qid}_output.json')
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def ensure_server():
    """Start the resident server once per container (a file lock stops two from starting)."""
    if server.alive():
        return
    with open('/tmp/agent.start.lock', 'w') as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another process is starting it
        if os.path.exists('/tmp/agent.server.pid'):
            try:
                os.kill(int(open('/tmp/agent.server.pid').read()), 0)
                return
            except (OSError, ValueError):
                pass
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), '--serve'],
                                stdout=open('/tmp/agent.server.log', 'a'), stderr=subprocess.STDOUT,
                                start_new_session=True)
        open('/tmp/agent.server.pid', 'w').write(str(proc.pid))


def wait_ready(seconds):
    end = time.time() + seconds
    while time.time() < end:
        if server.alive():
            return True
        time.sleep(1)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--serve', action='store_true')
    ap.add_argument('--index', nargs='?', const='')
    ap.add_argument('--warmup', action='store_true')
    ap.add_argument('--corpus')
    ap.add_argument('--query-id', '--id', '--task-id', dest='qid')
    ap.add_argument('--query', '--question', '--task', '--prompt', dest='query')
    ap.add_argument('--url', '--start-url', '--website', '--site', dest='url')
    ap.add_argument('--output-dir', dest='out')
    ap.add_argument('rest', nargs='*')
    args, _ = ap.parse_known_args()

    global OUT_DIR
    if args.out:
        OUT_DIR = args.out

    if args.serve:
        server.serve()
        return
    if args.index is not None or args.warmup:
        ensure_server()
        print('ready' if wait_ready(float(os.environ.get('AGENT_INDEX_WAIT_S', '540'))) else 'not ready', flush=True)
        return

    query = args.query or ' '.join(args.rest) or os.environ.get('AGENT_QUERY') or os.environ.get('QUERY')
    qid = args.qid or os.environ.get('AGENT_QUERY_ID') or os.environ.get('QUERY_ID') or 'query'
    url = args.url or os.environ.get('AGENT_URL') or os.environ.get('START_URL')
    if not query:
        ap.error('no query given')

    write(qid)  # a valid empty answer exists from the first moment
    threading.Timer(HARD_LIMIT, lambda: os._exit(0)).start()
    started = time.time()
    ensure_server()
    if not wait_ready(8):
        os._exit(0)  # model still loading: the empty answer stands
    budget = max(5.0, HARD_LIMIT - 3 - (time.time() - started))
    try:
        res = server.ask({'query': query, 'url': url, 'budget': budget}, timeout=budget + 2)
    except (OSError, ValueError):
        os._exit(0)
    if res.get('answer'):
        write(qid, res['answer'], res.get('source'), res.get('confidence', 0.5))
    if os.environ.get('AGENT_DEBUG'):
        print(json.dumps(res, indent=1)[:3000], file=sys.stderr)
    os._exit(0)


if __name__ == '__main__':
    main()
