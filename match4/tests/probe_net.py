"""Search-engine and source behaviour from wherever this runs (CI = a datacenter IP like the evaluator's)."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
from agent import resolvers  # noqa: E402
from agent import search as web  # noqa: E402
from agent.pipeline import terms  # noqa: E402

QS = ['Who is the CEO of AMD?', 'What is the default port Redis listens on?', 'What is the default port for MongoDB?',
      'In what year did Nigeria gain independence?', "Which HTTP status code means I'm a teapot?",
      'What is the latest stable Linux kernel version?', 'In what year was the Python Software Foundation founded?',
      'Who created the Rust programming language?', 'What is the current stable version of Go?',
      'What is the atomic number of gold?', 'Who wrote the novel Things Fall Apart?', 'In which city is AMD headquartered?']
engines = web.ENGINES + [('wikipedia', web.wikipedia)]
tot = {n: [0, 0, 0.0] for n, _ in engines}
for q in QS:
    kw = ' '.join(terms(q))
    row = []
    for name, fn in engines:
        t = time.time()
        try:
            got = fn(kw, 6.0)
            n = len(got)
            top = got[0][0][:50] if got else ''
        except Exception as e:
            n, top = -1, str(e)[:40]
        dt = time.time() - t
        tot[name][0] += n > 0
        tot[name][1] += 1
        tot[name][2] += dt
        row.append(f'{name}={n}/{dt:.1f}s {top}')
    print(kw, '\n   ' + '\n   '.join(row), flush=True)
for name, (ok, n, dt) in tot.items():
    print(f'{name:11} useful {ok}/{n}  avg {dt / n:.2f}s')
t = time.time()
errs = 0
for q in QS * 2:
    try:
        for title in resolvers.wiki_titles(' '.join(terms(q)), n=2):
            resolvers.wiki_lead(title)
    except Exception as e:
        errs += 1
        print('wiki error', q, e)
print(f'{len(QS) * 2 * 3} Wikipedia calls in {time.time() - t:.1f}s, {errs} errors')
t = time.time()
print('wiki_lead', resolvers.wiki_lead('Advanced Micro Devices')['text'][:200].replace('\n', ' | '), f'{time.time() - t:.2f}s')
