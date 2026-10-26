"""Key-free web search with fallbacks: DuckDuckGo HTML, Bing, Mojeek, and Wikipedia."""
import base64
import html
import json
import re
import urllib.parse

from .net import fetch, html_to_text

BAD_HOSTS = ('duckduckgo.com', 'bing.com', 'microsoft.com/en-us/bing', 'mojeek.com', 'go.microsoft.com',
             'chromewebstore.google.com')


def _clean(results):
    seen, out = set(), []
    for url, title in results:
        if not url.startswith('http') or any(h in url for h in BAD_HOSTS):
            continue
        key = url.split('#')[0].rstrip('/')
        if key in seen:
            continue
        seen.add(key)
        out.append({'url': url, 'title': title})
    return out


def duckduckgo(q, timeout=5.0):
    _, _, body = fetch('https://html.duckduckgo.com/html/?q=' + urllib.parse.quote(q), timeout=timeout)
    res = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        href = m.group(1)
        if 'uddg=' in href:
            href = urllib.parse.unquote(re.search(r'uddg=([^&]+)', href).group(1))
        res.append((href, re.sub('<[^>]+>', '', m.group(2))))
    return res


def _unbing(href):
    """Bing wraps results as bing.com/ck/a?...&u=a1<base64url>; return the real URL."""
    href = href.replace('&amp;', '&')
    m = re.search(r'[?&]u=a1([^&]+)', href)
    if not m:
        return href
    b = m.group(1)
    try:
        return base64.urlsafe_b64decode(b + '=' * (-len(b) % 4)).decode('utf-8', 'replace')
    except ValueError:
        return href


def bing(q, timeout=5.0):
    _, _, body = fetch('https://www.bing.com/search?setlang=en&cc=US&q=' + urllib.parse.quote(q), timeout=timeout)
    res = []
    for blk in body.split('class="b_algo"')[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if m:
            res.append((_unbing(m.group(1)), re.sub('<[^>]+>', '', m.group(2))))
    return res


def bing_rss(q, timeout=5.0):
    _, _, body = fetch('https://www.bing.com/search?format=rss&setlang=en&cc=US&q=' + urllib.parse.quote(q),
                       timeout=timeout, accept='application/rss+xml,text/xml')
    return [(html.unescape(l), html.unescape(t)) for t, l in
            re.findall(r'<item><title>(.*?)</title><link>(.*?)</link>', body, re.S)]


def mwmbl(q, timeout=5.0):
    _, _, body = fetch('https://api.mwmbl.org/search?s=' + urllib.parse.quote(q), timeout=timeout,
                       accept='application/json')
    return [(r.get('url', ''), ''.join(x.get('value', '') for x in r.get('title', [])) if isinstance(r.get('title'), list) else '')
            for r in json.loads(body)[:6]]


def mojeek(q, timeout=5.0):
    _, _, body = fetch('https://www.mojeek.com/search?q=' + urllib.parse.quote(q), timeout=timeout)
    res = []
    for m in re.finditer(r'<a class="ob" href="(https?://[^"]+)"', body):
        res.append((m.group(1), ''))
    return res


def wikipedia(q, timeout=5.0):
    url = ('https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=3&srsearch='
           + urllib.parse.quote(q))
    _, _, body = fetch(url, timeout=timeout, accept='application/json')
    hits = json.loads(body).get('query', {}).get('search', [])
    return [('https://en.wikipedia.org/wiki/' + urllib.parse.quote(h['title'].replace(' ', '_')), h['title'])
            for h in hits]


ENGINES = [('duckduckgo', duckduckgo), ('bing', bing), ('bing_rss', bing_rss), ('mojeek', mojeek), ('mwmbl', mwmbl)]


def search(q, want=10, timeout=5.0, log=None):
    """Query every engine in parallel and merge results round-robin (best of each engine first)."""
    from concurrent.futures import ThreadPoolExecutor, wait
    engines = ENGINES + [('wikipedia', wikipedia)]
    ex = ThreadPoolExecutor(len(engines))
    futs = {ex.submit(fn, q, timeout): name for name, fn in engines}
    done, _ = wait(futs, timeout=timeout)
    ex.shutdown(wait=False, cancel_futures=True)
    lists = []
    for f, name in futs.items():
        try:
            if f not in done:
                if log is not None:
                    log.append(f'{name}:slow')
                continue
            got = f.result(timeout=0)
        except Exception as e:
            got = []
            if log is not None:
                log.append(f'{name}:err {str(e)[:40]}')
            continue
        if log is not None:
            log.append(f'{name}:{len(got)}')
        lists.append(got)
    merged = []
    for i in range(max((len(x) for x in lists), default=0)):
        merged += [x[i] for x in lists if i < len(x)]
    return _clean(merged)[:want]
