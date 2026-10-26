"""Structured sources turned into small text pages, so they rank and verify like any other page."""
import json
import urllib.parse

from .net import fetch, html_to_text


def _json(url, timeout):
    _, _, body = fetch(url, timeout=timeout, accept='application/json')
    return json.loads(body)


def pypi(name, timeout=4.0):
    d = _json(f'https://pypi.org/pypi/{urllib.parse.quote(name)}/json', timeout)
    info = d['info']
    rel = d.get('releases', {}).get(info['version']) or [{}]
    when = (rel[0].get('upload_time') or '')[:10]
    text = (f"{info['name']} latest version {info['version']}\nreleased {when}\n"
            f"requires python {info.get('requires_python') or ''}\nlicense {info.get('license') or ''}\n"
            f"summary {info.get('summary') or ''}")
    return {'url': f'https://pypi.org/project/{info["name"]}/', 'title': f'{info["name"]} on PyPI', 'text': text, 'links': [], 'resolver': True}


def npm(name, timeout=4.0):
    # The full registry document runs to megabytes for big packages; dist-tags is a few bytes.
    q = urllib.parse.quote(name, safe="@")
    tags = _json(f'https://registry.npmjs.org/-/package/{q}/dist-tags', timeout)
    tags = dict(sorted(tags.items(), key=lambda kv: kv[0] != 'latest'))
    lines = [f"{name} {tag} version {v}" for tag, v in tags.items()]
    return {'url': f'https://www.npmjs.com/package/{name}', 'title': f'{name} on npm',
            'text': '\n'.join(lines), 'links': [], 'resolver': True}


def github(repo, timeout=4.0):
    d = _json(f'https://api.github.com/repos/{repo}/releases/latest', timeout)
    tag = d.get('tag_name', '')
    return {'url': d.get('html_url') or f'https://github.com/{repo}/releases', 'title': f'{repo} latest release',
            'text': f"{repo} latest release {tag}\nversion {tag.lstrip('v')}\nname {d.get('name') or ''}\n"
                    f"published {(d.get('published_at') or '')[:10]}", 'links': [], 'resolver': True}


def endoflife(product, timeout=4.0):
    d = _json(f'https://endoflife.date/api/{urllib.parse.quote(product)}.json', timeout)
    lines = [f"{product} {c.get('cycle')} latest {c.get('latest')} released {c.get('releaseDate')} "
             f"lts {c.get('lts')} end of life {c.get('eol')}" for c in d[:8]]
    return {'url': f'https://endoflife.date/{product}', 'title': f'{product} release cycles (endoflife.date)',
            'text': '\n'.join(lines), 'links': [], 'resolver': True}


def wiki_titles(q, timeout=4.0, n=2):
    d = _json('https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=%d&srsearch=%s'
              % (n, urllib.parse.quote(q)), timeout)
    return [h['title'] for h in d.get('query', {}).get('search', [])]


def wiki_lead(title, timeout=4.0):
    """The lead section and infobox of a Wikipedia article: a few KB that hold most headline facts."""
    title = urllib.parse.unquote(title.rsplit('/wiki/', 1)[-1]).replace('_', ' ')
    d = _json('https://en.wikipedia.org/w/api.php?action=parse&prop=text&section=0&format=json&redirects=1&page='
              + urllib.parse.quote(title), timeout)
    p = d['parse']
    _, text, links = html_to_text(p['text']['*'], 'https://en.wikipedia.org/wiki/')
    url = 'https://en.wikipedia.org/wiki/' + urllib.parse.quote(p['title'].replace(' ', '_'))
    return {'url': url, 'title': p['title'] + ' - Wikipedia', 'text': text, 'links': links, 'lead': True}


RESOLVERS = {'pypi': pypi, 'npm': npm, 'github': github, 'endoflife': endoflife}


def resolve(hints, timeout=4.0):
    """hints: {"pypi": "requests", "github": "owner/repo", ...} -> list of (callable, arg)."""
    jobs = []
    for key, fn in RESOLVERS.items():
        v = hints.get(key) if isinstance(hints, dict) else None
        if isinstance(v, str) and v.strip():
            jobs.append((fn, v.strip()))
    return jobs
