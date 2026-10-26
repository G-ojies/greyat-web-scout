"""HTTP fetching and HTML-to-text, standard library only."""
import gzip
import html
import re
import socket
import time
import urllib.parse
import urllib.error
import urllib.request
import zlib
from html.parser import HTMLParser

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/126.0 Safari/537.36')
# Wikimedia throttles (HTTP 429) browser-looking clients from cloud IPs; its policy asks bots to name
# themselves with a contact URL, which keeps the API answering through a full evaluation run.
WIKI_UA = 'Match4WebAgent/1.0 (https://lablab.ai/ai-hackathons/amd-lablab-ai-academy-challenge) python-urllib'
WIKI_HOSTS = ('wikipedia.org', 'wikimedia.org', 'wikidata.org')
MAX_BYTES = 1_500_000
SKIP_TAGS = {'script', 'style', 'noscript', 'svg', 'template', 'iframe', 'head'}
BLOCK_TAGS = {'p', 'div', 'br', 'li', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
              'section', 'article', 'table', 'ul', 'ol', 'dt', 'dd', 'pre', 'blockquote',
              'header', 'footer', 'nav', 'main', 'aside', 'figcaption', 'caption', 'hr'}


class Stale(OSError):
    """The question this fetch belonged to has finished."""


def fetch(url, timeout=6.0, accept='text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8', stop=None):
    """Return (final_url, content_type, text) or raise. Setting the `stop` event abandons the download."""
    host = urllib.parse.urlsplit(url).hostname or ''
    wiki = host.endswith(WIKI_HOSTS)
    req = urllib.request.Request(url, headers={
        'User-Agent': WIKI_UA if wiki else UA, 'Accept': accept, 'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate'})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if not (wiki and e.code == 429):
            raise
        time.sleep(0.6)
        r = urllib.request.urlopen(req, timeout=timeout)
    with r:
        chunks, size = [], 0
        while size < MAX_BYTES:
            if stop is not None and stop.is_set():
                raise Stale(url)
            b = r.read(min(65536, MAX_BYTES - size))
            if not b:
                break
            chunks.append(b)
            size += len(b)
        raw = b''.join(chunks)
        enc = (r.headers.get('Content-Encoding') or '').lower()
        if enc == 'gzip':
            raw = gzip.decompress(raw)
        elif enc == 'deflate':
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        ctype = r.headers.get('Content-Type', '')
        charset = re.search(r'charset=([\w-]+)', ctype)
        text = raw.decode(charset.group(1) if charset else 'utf-8', errors='replace')
        return r.geturl(), ctype, text


class _TextParser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts, self.links, self.title = [], [], ''
        self._skip = 0
        self._in_title = False
        self._href = None
        self._anchor = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
            if tag == 'head':
                self._skip -= 1  # keep <title> inside head
        if tag == 'title':
            self._in_title = True
        if tag in BLOCK_TAGS:
            self.parts.append('\n')
        if tag == 'a':
            href = dict(attrs).get('href')
            if href and not href.startswith(('javascript:', '#')):
                self._href = urllib.parse.urljoin(self.base, href)
                self._anchor = []
                if href.startswith(('tel:', 'mailto:')):
                    self.parts.append(' ' + urllib.parse.unquote(href.split(':', 1)[1]) + ' ')

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and tag != 'head':
            self._skip = max(0, self._skip - 1)
        if tag == 'title':
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append('\n')
        if tag == 'a' and self._href:
            self.links.append((self._href, ' '.join(self._anchor).strip()))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data.strip())


def html_to_text(doc, base='', stop=None):
    """Return (title, text, links) for an HTML document."""
    p = _TextParser(base)
    try:
        for i in range(0, len(doc), 65536):
            if stop is not None and stop.is_set():
                raise Stale(base)
            p.feed(doc[i:i + 65536])
    except Stale:
        raise
    except Exception:
        pass
    text = ''.join(p.parts)
    text = re.sub(r'[ \t\r\f\v\xa0]+', ' ', text)
    text = re.sub(r' *\n[ \n]*', '\n', text).strip()
    return html.unescape(p.title.strip()), text, p.links


def page(url, timeout=6.0, stop=None):
    """Fetch a URL and return a dict with url, title, text, links (empty text on failure)."""
    try:
        final, ctype, body = fetch(url, timeout=timeout, stop=stop)
        if 'html' in ctype or body.lstrip()[:15].lower().startswith(('<!doctype', '<html')):
            title, text, links = html_to_text(body, final, stop)
        else:
            title, text, links = '', body, []
    except (OSError, ValueError, socket.timeout, zlib.error, EOFError) as e:
        return {'url': url, 'title': '', 'text': '', 'links': [], 'error': str(e)[:120]}
    return {'url': final, 'title': title, 'text': text[:400_000], 'links': links}
