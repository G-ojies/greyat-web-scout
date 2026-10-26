"""Question -> (answer, source URL). Every answer must appear word for word in a fetched page."""
import math
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait

from . import resolvers
from . import search as web
from .llm import parse_json
from .net import page

STOP = set('''a an the of in on at to for from by with and or is are was were be been being what which who whom
whose when where why how does do did doing has have had this that these those it its as into than then there their
they them he she his her you your i me my we our us please give tell find show list current currently latest
official exact value number name'''.split())

# Words a question uses that the answering page often phrases differently (infobox labels especially).
SYN = {
    'founded': ['formation', 'formed', 'established', 'inception'],
    'founder': ['founded by', 'founders', 'created by'],
    'founded by': ['founder'],
    'created': ['designed by', 'developer', 'creator', 'designer', 'invented'],
    'creator': ['designed by', 'created by', 'developer'],
    'invented': ['inventor', 'designed by'],
    'ceo': ['chief executive', 'key people'],
    'headquartered': ['headquarters'],
    'released': ['initial release', 'first appeared', 'release date'],
    'wrote': ['author', 'written by'],
    'born': ['birth'],
    'died': ['death'],
    'population': ['inhabitants'],
}

PLAN_SYS = ('You plan web lookups. Reply with JSON only: {"queries": [two short web search queries], '
            '"urls": [up to 3 full URLs of official or authoritative pages that most likely state the answer]}. '
            'Prefer primary sources: the vendor or project site, its docs, release notes, GitHub releases, PyPI, '
            'Wikipedia. When they apply, also add "pypi": "<package>", "npm": "<package>", '
            '"github": "<owner/repo>", "endoflife": "<product slug on endoflife.date, e.g. python, nodejs, '
            'ubuntu>", "wikipedia": ["<exact English Wikipedia article title of the subject>"]. '
            'Do not answer the question.')

EXTRACT_SYS = ('You extract one exact value from numbered web passages. Reply with JSON only: '
               '{"answer": "...", "passage": n}. The answer must be copied exactly as it appears in passage n: '
               'just the value (a number, version, date, name, price, code or short phrase), never a sentence, and '
               'without repeating the subject the question names (for "latest version of Python" answer "3.15.0", '
               'not "Python 3.15.0"). Keep a qualifier only when it is part of the value itself (a currency '
               'symbol, a unit, a prefix like REV-C2 or Q3 FY27). If the question asks for the '
               'latest or current value, prefer the newest one the passages state, and trust an official release '
               'page, a package registry or the site the question names over an encyclopedia. If no passage states the '
               'answer, reply {"answer": "", "passage": 0}. Never answer from memory.')


def safe_chat(llm, system, user, max_new_tokens):
    try:
        return llm.chat(system, user, max_new_tokens=max_new_tokens)
    except Exception:
        return ''


def norm(s):
    """The grader's normalisation: uppercase, no whitespace, no - . · _"""
    return re.sub(r'[\s\-.·_]', '', (s or '').upper())


def terms(q):
    return [t for t in re.findall(r'[a-z0-9][a-z0-9.+#-]*', q.lower()) if t not in STOP and len(t) > 1]


GENERIC = {'I', 'CEO', 'CTO', 'CFO', 'COO', 'GPU', 'CPU', 'API', 'URL', 'ISO', 'RFC', 'LTS', 'USD', 'NPM', 'PYPI'}


def _forms(t):
    """A term, a crude stem of it, and its synonyms: 'headquartered' also matches 'headquarters'."""
    out = [t] + SYN.get(t, [])
    if len(t) >= 6:
        out.append(t[:max(4, len(t) - 3)])
    return out


def entities(q):
    """Capitalised names in the question, likely Wikipedia titles: 'Python Software Foundation'."""
    words = q.rstrip('?.!').split()
    out, run = [], []
    for i, w in enumerate(words):
        w = w.strip(',;:"()\'')
        if w[:1].isupper() and not (i == 0 and w.lower() in STOP) and w.upper() not in GENERIC:
            run.append(w.removesuffix("'s"))
        else:
            if run:
                out.append(' '.join(run))
            run = []
    if run:
        out.append(' '.join(run))
    return list(dict.fromkeys(out))[:3]


LATEST = re.compile(r'\b(latest|current|currently|newest|most recent|now|today|stable)\b', re.I)


def passages(pg, qterms, width=6, latest=False):
    """Split a page into overlapping windows of lines and score each against the query terms."""
    lines = [l for l in pg['text'].split('\n') if l.strip()]
    forms = [_forms(t) for t in set(qterms)]
    lead = pg.get('lead') or pg.get('start')
    # For "latest/current" questions a registry, release feed or the given start page beats an
    # encyclopedia article body, which is often a version or two behind.
    bias = 0.0
    if latest:
        if pg.get('resolver') or pg.get('start') or pg.get('same_site'):
            bias = 1.0
        elif 'wikipedia.org' in pg['url'] and not pg.get('lead'):
            bias = -1.0
    out = []
    for i in range(0, len(lines), max(1, width // 2)):
        chunk = '\n'.join(lines[i:i + width])[:900]
        low = chunk.lower()
        hit = sum(1 for f in forms if any(x in low for x in f))
        if hit or (lead and i < width * 2):
            # A Wikipedia lead/infobox opening is kept even without a word match: it holds the headline facts.
            out.append({'url': pg['url'], 'title': pg['title'], 'text': chunk,
                        'score': hit + 0.3 * any(c.isdigit() for c in chunk) + 0.2 * (i == 0)
                        + (1.5 if lead and i < width * 2 else 0) + bias})
    return out


def relevant(results, qterms):
    """Drop search hits that share no real word with the question. Engines that detect a bot answer
    with pages about the query's first word only (a dictionary entry for "default", chicago.gov for
    "city"), so a match on that word alone does not count when the question has more to it."""
    uniq = list(dict.fromkeys(qterms))
    out = []
    for r in results:
        hay = (r.get('title', '') + ' ' + r['url']).lower()
        hit = [t for t in uniq if any(x in hay for x in _forms(t))]
        if hit and (hit != uniq[:1] or len(uniq) < 3):
            out.append(r)
    return out


def follow(pg, qterms, n=3):
    """Pick the page's links whose anchor text or URL best matches the query."""
    scored = []
    for href, anchor in pg.get('links', []):
        if not href.startswith('http') or href.split('#')[0] == pg['url'].split('#')[0]:
            continue
        hay = (anchor + ' ' + href).lower()
        s = sum(1 for t in qterms if t in hay)
        if s:
            scored.append((s, href))
    seen, out = set(), []
    for s, href in sorted(scored, key=lambda x: -x[0]):
        if href not in seen:
            seen.add(href)
            out.append(href)
        if len(out) >= n:
            break
    return out


def fetch_all(urls, deadline, timeout=5.0, stop=None):
    urls = list(dict.fromkeys(u for u in urls if u and u.startswith('http')))
    if not urls:
        return []
    budget = max(0.5, min(timeout, deadline - time.time()))
    ex = ThreadPoolExecutor(min(16, len(urls)))
    futs = [ex.submit(page, u, budget, stop) for u in urls]
    done, _ = wait(futs, timeout=budget)
    ex.shutdown(wait=False, cancel_futures=True)  # never wait on a slow site
    return [f.result() for f in futs if f in done and f.result()['text']]


def _wiki_guess(query, kw):
    """Lead sections for the names in the question, else for Wikipedia's own top hit."""
    names = entities(query)
    pages = []
    for title in names[:2] or resolvers.wiki_titles(kw, n=1):
        try:
            pages.append(resolvers.wiki_lead(title))
        except Exception:
            pass
    return pages


def answer(query, llm, start_url=None, budget=24.0, trace=None):
    """Answer one question. Fetches still running when it returns are told to stop, so they do not
    eat bandwidth and CPU that the next question needs."""
    stop = threading.Event()
    try:
        return _answer(query, llm, start_url, budget, trace, stop)
    finally:
        stop.set()


def _answer(query, llm, start_url, budget, trace, stop):
    t0 = time.time()
    deadline = t0 + budget
    trace = trace if trace is not None else {}
    qterms = terms(query)

    # 1. Plan (model), search (engines, keyword form) and Wikipedia guesses all start at once.
    kw = ' '.join(terms(query)) or query
    ex = ThreadPoolExecutor(5)
    log = []
    plan_f = ex.submit(lambda: parse_json(safe_chat(llm, PLAN_SYS, query, 220)))
    search_f = ex.submit(web.search, kw, 8, 4.5, log)
    start_f = ex.submit(page, start_url, 5.0, stop) if start_url else None
    wiki_f = ex.submit(_wiki_guess, query, kw)
    ex.shutdown(wait=False)
    plan, results, start_pg, leads = {}, [], None, []
    try:
        plan = plan_f.result(timeout=max(1.0, min(10.0, deadline - time.time() - 12)))
    except Exception:
        pass
    if not isinstance(plan, dict):
        plan = {}
    try:
        results = search_f.result(timeout=max(0.5, 5.0 - (time.time() - t0)))
    except Exception:
        pass
    if start_f:
        try:
            start_pg = start_f.result(timeout=max(0.5, 6.0 - (time.time() - t0)))
        except Exception:
            pass
    trace['search'] = log
    trace['t_plan_search'] = round(time.time() - t0, 1)
    trace['plan'] = plan

    if start_pg and start_pg['text']:
        start_pg['start'] = True
    pages = [start_pg] if start_pg and start_pg['text'] else []
    urls = []
    if start_pg and start_pg['text']:
        urls += follow(start_pg, qterms, 4)
    elif start_url:
        urls.append(start_url)  # first try failed or was slow: one more go with the main batch
    planned = [u for u in (plan.get('urls') or [])[:3] if isinstance(u, str)]
    urls += planned
    urls += [r['url'] for r in relevant(results, qterms)[:6]]
    titles = plan.get('wikipedia') or []
    titles = [titles] if isinstance(titles, str) else [t for t in titles if isinstance(t, str)]
    titles += [u for u in planned if '.wikipedia.org/wiki/' in u]
    jobs = resolvers.resolve(plan) + [(resolvers.wiki_lead, t) for t in titles[:2]]
    ex = ThreadPoolExecutor(6)
    res_f = [ex.submit(fn, arg) for fn, arg in jobs]
    extra = [q for q in (plan.get('queries') or [])[:1] if isinstance(q, str) and q.strip()]
    extra_f = ex.submit(web.search, extra[0], 4, 3.0) if extra and time.time() < deadline - 14 else None
    ex.shutdown(wait=False)
    if extra_f:
        try:
            urls += [r['url'] for r in relevant(extra_f.result(timeout=3.5), qterms)]
        except Exception:
            pass
    trace['t_extra_search'] = round(time.time() - t0, 1)
    pages += fetch_all(urls, deadline - 9, stop=stop)
    for f in res_f + [wiki_f]:
        try:
            got = f.result(timeout=max(0.1, min(2.0, deadline - 8 - time.time())))
        except Exception:
            continue
        for pg in got if isinstance(got, list) else [got]:
            if pg and pg.get('text'):
                pages.insert(0, pg)
    if start_url:
        host = urllib.parse.urlsplit(start_url).hostname
        for pg in pages:
            if pg['url'].rstrip('/') == start_url.rstrip('/'):
                pg['start'] = True
            if urllib.parse.urlsplit(pg['url']).hostname == host:
                pg['same_site'] = True
    trace['t_fetch'] = round(time.time() - t0, 1)
    trace['n_urls'] = len(urls)

    # 2. One hop from the best pages if nothing scores well yet.
    latest = bool(LATEST.search(query))
    cands = [p for pg in pages for p in passages(pg, qterms, latest=latest)]
    if (not cands or max(c['score'] for c in cands) < max(2, len(set(qterms)) * 0.6)) and time.time() < deadline - 10:
        hop = [h for pg in pages[:4] for h in follow(pg, qterms, 2)]
        more = fetch_all(hop, deadline - 8, stop=stop)
        pages += more
        cands += [p for pg in more for p in passages(pg, qterms, latest=latest)]
    trace['pages'] = [pg['url'] for pg in pages]
    if not cands:
        return '', None, 0.0

    # 3. Best passages, at most 3 per page, then let the model pick one value.
    cands.sort(key=lambda c: -c['score'])
    picked, per = [], {}
    for c in cands:
        if per.get(c['url'], 0) < 3:
            picked.append(c)
            per[c['url']] = per.get(c['url'], 0) + 1
        if len(picked) >= 8:
            break
    site = ''
    if start_url:
        host = urllib.parse.urlsplit(start_url).hostname or ''
        site = (f'The question was asked about {start_url}: when its site ({host}) states the value, '
                f'answer exactly as that site writes it.\n\n')
    prompt = f'Question: {query}\n\n' + site + '\n\n'.join(
        f'[{i + 1}] {c["title"]} ({c["url"]})\n{c["text"]}' for i, c in enumerate(picked))
    if time.time() > deadline - 2:
        return '', None, 0.0
    trace['t_before_extract'] = round(time.time() - t0, 1)
    reply = parse_json(safe_chat(llm, EXTRACT_SYS, prompt, 80))
    trace['t_extract'] = round(time.time() - t0, 1)
    ans = str(reply.get('answer') or '').strip().strip('"').strip()
    m = re.fullmatch(r'v?(\d+(?:\.\d+){1,3})\s*\(?(?:LTS|stable|latest|release)?\)?', ans, re.I)
    if m:
        ans = m.group(1)  # "v24.22.1 LTS" -> "24.22.1"
    if re.fullmatch(r'\d{1,3}(,\d{3})+(\.\d+)?', ans):
        ans = ans.replace(',', '')  # the grader does not strip thousands separators
    trace['reply'] = reply
    if not ans or not norm(ans):
        return '', None, 0.0

    # 4. Verify: the value must appear verbatim (after normalisation) in a passage; cite that page.
    try:
        n = int(reply.get('passage') or 0)
    except (TypeError, ValueError):
        n = 0
    chosen = picked[n - 1] if 1 <= n <= len(picked) else None
    if chosen is None and latest:
        trace['rejected'] = ans  # no passage named: a value from memory, stale for "latest" questions
        return '', None, 0.0

    def has(c):
        t = norm(c['text'])
        return norm(ans) in t or norm(ans) in t.replace(',', '')

    holding = [c for c in picked if has(c)]
    if holding:
        # Cite the passage that best matches the question; the model's own pick wins ties.
        def rank(c):
            title = c['title'].lower() + ' ' + c['url'].lower()
            return c['score'] + (1.0 if c is chosen else 0) + 0.5 * sum(1 for t in set(qterms) if t in title)
        best = max(holding, key=rank)
        return ans, best['url'], 0.9 if best is chosen else 0.7
    for pg in pages:
        if norm(ans) in norm(pg['text']):
            return ans, pg['url'], 0.5
    trace['rejected'] = ans
    return '', None, 0.0
