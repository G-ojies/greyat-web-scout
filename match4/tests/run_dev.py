"""Run a question file through app.py the way the harness would, one process per question."""
import json
import os
import subprocess
import sys
import time

APP = os.path.join(os.path.dirname(__file__), '..', 'app', 'app.py')
OUT = os.environ.get('AGENT_OUTPUT', 'out')
qs = json.load(open(sys.argv[1]))


def live_expect(how):
    """Today's true value for questions whose answer changes over time."""
    import urllib.request
    get = lambda u: json.load(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'dev'}), timeout=15))
    if 'pypi' in how:
        return [get(f"https://pypi.org/pypi/{how['pypi']}/json")['info']['version']]
    if 'npm' in how:
        return [get(f"https://registry.npmjs.org/{how['npm']}")['dist-tags']['latest']]
    if 'github' in how:
        t = get(f"https://api.github.com/repos/{how['github']}/releases/latest")['tag_name']
        return [t, t.lstrip('v')]
    if 'go' in how:
        v = urllib.request.urlopen('https://go.dev/VERSION?m=text', timeout=15).read().decode().split()[0]
        return [v, v[2:]]
    if 'kernel' in how:
        return [get('https://www.kernel.org/releases.json')['latest_stable']['version']]
    if 'endoflife' in how:
        return [get(f"https://endoflife.date/api/{how['endoflife']}.json")[0]['latest']]


for q in qs:
    if 'live' in q:
        try:
            q['expect'] = live_expect(q['live'])
        except Exception as e:
            print('no live value for', q['id'], e)
os.makedirs(OUT, exist_ok=True)
hits = 0
only = set(sys.argv[2:])
for q in qs:
    if only and q['id'] not in only:
        continue
    time.sleep(float(os.environ.get('DEV_GAP_S', '0')))  # the free dev LLM allows ~8k tokens a minute
    t = time.time()
    cmd = [sys.executable, APP, '--query-id', q['id'], '--query', q['query']] + (['--url', q['url']] if q.get('url') else [])
    env = dict(os.environ, AGENT_DEBUG='1')
    with open(os.path.join(OUT, f"{q['id']}_trace.json"), 'w') as tf:
        subprocess.run(cmd, timeout=200, env=env, stderr=tf)
    o = json.load(open(os.path.join(OUT, f"{q['id']}_output.json")))
    ok = ''
    if 'expect' in q:
        norm = lambda s: ''.join(ch for ch in (s or '').upper() if ch not in ' \t\n-.·_')
        ok = 'OK ' if norm(o['answer']) in {norm(e) for e in q['expect']} else 'XX '
        hits += ok == 'OK '
    print(f"{ok}{time.time() - t:5.1f}s {q['id']} {o['answer']!r:22} {o['source_url'][:70]}" + ('' if ok != 'XX ' else f"  want {q['expect'][0]!r}"), flush=True)
if any('expect' in q for q in qs):
    print(f'{hits}/{sum("expect" in q and (not only or q["id"] in only) for q in qs)} correct')
