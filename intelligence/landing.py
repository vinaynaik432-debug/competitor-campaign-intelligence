"""Small public-page snapshots, without browser execution or private-network access."""
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_BYTES = 1_500_000


def is_link_gateway(url, text):
    host = (urlsplit(url).hostname or '').lower()
    linkedin = host in {'lnkd.in', 'linkedin.com'} or host.endswith('.linkedin.com')
    return linkedin and ('this link will take you to a page' in text.lower()
                         or 'unable to verify it for safety' in text.lower())


def validate_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password
            or parts.port not in {None, 80 if parts.scheme == 'http' else 443}):
        raise ValueError('Only public HTTP(S) pages on standard ports can be captured.')
    host = parts.hostname.encode('idna').decode('ascii')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('Private-network pages are not allowed.')
    port = 443 if parts.scheme == 'https' else 80
    addresses = {entry[4][0] for entry in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('Private-network pages are not allowed.')
    return parts, host, port, sorted(addresses)[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, port):
        super().__init__(host, port, timeout=12, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Pin the checked address, retaining hostname verification and TLS SNI.
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PageText(HTMLParser):
    SKIP = {'script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg', 'template'}
    VOID = {'br', 'img', 'input', 'meta', 'link', 'hr', 'source', 'area', 'base', 'embed', 'wbr', 'param', 'track', 'col'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.all_text, self.main_text, self.titles = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        attrs = dict(attrs)
        token = (attrs.get('id', '') + ' ' + attrs.get('class', '')).lower()
        skip = tag in self.SKIP or bool(re.search(r'cookie|consent|onetrust|navigation', token))
        self.stack.append((tag, skip, tag in {'main', 'article'} or attrs.get('role') == 'main'))

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        text = ' '.join(data.split())
        if not text:
            return
        if any(item[0] == 'title' for item in self.stack):
            self.titles.append(text)
        if any(item[1] for item in self.stack) or any(item[0] == 'head' for item in self.stack):
            return
        self.all_text.append(text)
        if any(item[2] for item in self.stack):
            self.main_text.append(text)


def capture_page(url):
    result = {'url': url, 'status': 'failed', 'text': '', 'title': '',
              'fetched_at': datetime.now(timezone.utc).isoformat()}
    try:
        current = url
        for attempt in range(4):
            parts, host, port, address = validate_url(current)
            connection = PinnedHTTPS(host, address, port) if parts.scheme == 'https' else http.client.HTTPConnection(address, port, timeout=12)
            path = urlunsplit(('', '', parts.path or '/', parts.query, ''))
            try:
                connection.request('GET', path, headers={'Host': host, 'User-Agent': 'CampaignRadar/0.1 (public page research)',
                                                        'Accept': 'text/html', 'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader('Location')
                    if not location or attempt == 3:
                        raise ValueError('Redirect limit reached.')
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    raise ValueError('Page returned HTTP %s.' % response.status)
                if 'text/html' not in (response.getheader('Content-Type') or '').lower():
                    raise ValueError('Destination is not an HTML page.')
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('Page exceeded the capture size limit.')
            finally:
                connection.close()
            parser = PageText()
            parser.feed(raw.decode('utf-8', errors='replace'))
            text = '\n'.join(parser.main_text or parser.all_text)[:40000]
            if is_link_gateway(current, text):
                result.update(url=current, title=' '.join(parser.titles)[:300],
                              error='LinkedIn returned a link gateway, not the destination page. Open the source link to review it manually.')
                return result
            if len(text) < 80 or re.search(r'^(just a moment|access denied|verify you are human)', text, re.I):
                raise ValueError('Page text unavailable, blocked, or requires JavaScript.')
            result.update(url=current, status='ok', text=text, title=' '.join(parser.titles)[:300],
                          evidence_hash=hashlib.sha256(text.encode()).hexdigest())
            return result
    except (ValueError, OSError, http.client.HTTPException):
        result['error'] = 'Public page could not be captured (blocked, unsupported, unsafe destination, or network error).'
    return result
