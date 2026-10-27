"""Campaign Radar local dashboard. Run: python3 app.py"""
import argparse
import csv
import io
import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from intelligence.apify import Apify, WATCHLIST, collect
from intelligence.store import Store

ROOT = Path(__file__).resolve().parent
METHODOLOGY = ('Observations from public LinkedIn ads, not spend or campaign performance. First observed does not mean newly launched. '
               'Audience phrases describe the message, not confirmed targeting. Missing ads in incomplete samples never mean a campaign stopped. '
               'The first live collection is a baseline. Rule-based comparisons do not use an LLM.')


def csv_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    value = str(value if value is not None else '')
    # Quoting alone does not prevent spreadsheet formula execution.
    if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        value = "'" + value
    return value


def csv_bytes(rows, columns):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(columns)
    writer.writerows([[csv_cell(row.get(key, '')) for key in columns] for row in rows])
    return ('\ufeff' + out.getvalue()).encode('utf-8')


class Application:
    def __init__(self, data_dir):
        self.store = Store(Path(data_dir) / 'campaign-radar.sqlite3')
        self.data_dir = Path(data_dir)
        self.csrf = secrets.token_urlsafe(32)
        self.token = ''
        self.job = None
        self.lock = threading.Lock()

    def all_snapshots(self, mode):
        if mode == 'live':
            return self.store.snapshots()
        if mode == 'demo':
            path = ROOT / 'examples/demo.json'
            return json.loads(path.read_text(encoding='utf-8'))['snapshots'] if path.exists() else []
        raise ValueError('Choose sample or live data.')

    def snapshot_list(self):
        result = []
        for mode in ('live', 'demo'):
            for snapshot in self.all_snapshots(mode):
                statuses = {c['status'] for c in snapshot['companies']}
                status = 'complete' if statuses == {'complete'} else ('failed' if statuses == {'failed'} else 'partial')
                result.append({'id': snapshot['id'], 'collected_at': snapshot['collected_at'], 'mode': mode,
                               'ad_count': sum(len(c['ads']) for c in snapshot['companies']), 'status': status})
        return sorted(result, key=lambda s: s['collected_at'], reverse=True)

    def report(self, mode, snapshot_id=''):
        from intelligence.model import compare_snapshots, classify_ad
        snapshots = self.all_snapshots(mode)
        if snapshot_id and snapshot_id not in {s['id'] for s in snapshots}:
            raise ValueError('Snapshot not found in the selected dataset.')
        snapshot = next((s for s in snapshots if s['id'] == snapshot_id), snapshots[-1] if snapshots else None)
        previous = None
        if snapshot:
            index = snapshots.index(snapshot)
            # Use the most recent comparable snapshot, rather than crossing collection scope.
            previous = next((s for s in reversed(snapshots[:index]) if s['scope'] == snapshot['scope']), None)
        events = compare_snapshots(previous, snapshot) if snapshot else []
        companies = snapshot['companies'] if snapshot else []
        for company in companies:
            for ad in company['ads']:
                ad['classification'] = classify_ad(ad)
        company_summary = [{'key': c['key'], 'name': c['name'], 'ad_count': len(c['ads']),
                            'status': c['status'], 'notes': c.get('notes', [])} for c in companies]
        statuses = {c['status'] for c in companies}
        status = 'empty' if not companies else ('complete' if statuses == {'complete'} else ('failed' if statuses == {'failed'} else 'partial'))
        return {'mode': mode, 'snapshot': snapshot, 'previous': {'id': previous['id'], 'collected_at': previous['collected_at']} if previous else None,
                'events': events, 'feedback': self.store.feedback(snapshot['id']) if snapshot else {},
                'summary': {'companies': len(companies), 'ads': sum(len(c['ads']) for c in companies),
                            'changes': len(events), 'collection_status': status, 'baseline': previous is None},
                'company_summary': company_summary, 'methodology': METHODOLOGY}

    def bootstrap(self):
        return {'csrf': self.csrf, 'connected': bool(self.token), 'watchlist': WATCHLIST,
                'budget': self.store.budget(), 'snapshots': self.snapshot_list(), 'job': self.job}

    def start_scan(self, data):
        max_ads = data.get('max_ads_per_company', 10)
        pages = data.get('capture_pages', True)
        if isinstance(max_ads, bool) or max_ads not in {5, 10, 20} or not isinstance(pages, bool):
            raise ValueError('Choose 5, 10 or 20 ads per company and a valid page capture setting.')
        if not self.token:
            raise ValueError('Connect Apify first using the private token field.')
        if self.store.budget()['remaining_usd'] < .5:
            raise ValueError('Insufficient unallocated project allowance for five runs.')
        if not self.lock.acquire(blocking=False):
            raise ValueError('A collection is already running.')
        token = self.token
        self.job = {'id': secrets.token_hex(6), 'status': 'running', 'message': 'Checking provider pricing…'}

        def worker():
            try:
                snapshot = collect(token, self.store, self.data_dir / 'raw', max_ads, pages,
                                   lambda message: self.job.update(message=message))
                failed = sum(c['status'] == 'failed' for c in snapshot['companies'])
                self.job.update(status='done', snapshot_id=snapshot['id'],
                                message='Snapshot saved. %d of 5 companies need attention.' % failed if failed else 'Snapshot saved. Ad-library samples are ready to review.')
            except Exception as error:
                self.job.update(status='error', message=str(error).replace(token, '[redacted]'))
            finally:
                self.lock.release()
        threading.Thread(target=worker, daemon=True).start()
        return dict(self.job)


class Handler(BaseHTTPRequestHandler):
    server_version = 'CampaignRadar'

    def log_message(self, *args):
        pass  # Request bodies, tokens and URLs are never logged.

    def send(self, body, status=200, content_type='application/json', filename=None):
        if isinstance(body, (dict, list)) or body is None:
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type + ('; charset=utf-8' if content_type.startswith('text/') else ''))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host') in {'127.0.0.1:%d' % self.server.server_port, 'localhost:%d' % self.server.server_port}

    def do_GET(self):
        if not self.valid_host():
            return self.send({'error': 'Invalid host.'}, 403)
        app = self.server.application
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == '/api/bootstrap':
                return self.send(app.bootstrap())
            if url.path == '/api/job':
                return self.send(app.job)
            if url.path in {'/api/report', '/api/export', '/api/events/export'}:
                report = app.report(query.get('mode', ['demo'])[0], query.get('snapshot_id', [''])[0])
                if url.path == '/api/report':
                    return self.send(report)
                if url.path == '/api/events/export':
                    rows = [dict(e, dataset=report['mode'], feedback=report['feedback'].get(e['id'], '')) for e in report['events']]
                    columns = ['dataset', 'company_name', 'type', 'title', 'before', 'after', 'before_at', 'after_at',
                               'before_url', 'after_url', 'explanation', 'suggested_action', 'confidence', 'feedback']
                else:
                    rows = []
                    for company in (report['snapshot'] or {}).get('companies', []):
                        for ad in company['ads']:
                            rows.append(dict(ad, dataset=report['mode'], company=company['name'],
                                             observed_at=report['snapshot']['collected_at'], collection_status=company['status']))
                    columns = ['dataset', 'company', 'observed_at', 'collection_status', 'id', 'text', 'headline', 'cta',
                               'format', 'library_url', 'landing_url', 'landing_url_source', 'start_date', 'end_date', 'targeting']
                return self.send(csv_bytes(rows, columns), content_type='text/csv', filename='campaign-radar-%s.csv' % ('changes' if '/events/' in url.path else 'ads'))
            files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
            if url.path in files:
                path = ROOT / 'static' / files[url.path]
                if path.exists():
                    return self.send(path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                return self.send('Dashboard setup is finishing. Refresh shortly.', content_type='text/plain')
            return self.send({'error': 'Not found.'}, 404)
        except ValueError as error:
            return self.send({'error': str(error)}, 400)
        except Exception:
            return self.send({'error': 'Could not load local data. Check the project files.'}, 500)

    def do_POST(self):
        app = self.server.application
        if not self.valid_host() or not secrets.compare_digest(self.headers.get('X-Session-Token', ''), app.csrf):
            return self.send({'error': 'Reload the dashboard to reconnect.'}, 403)
        origins = {'http://127.0.0.1:%d' % self.server.server_port, 'http://localhost:%d' % self.server.server_port}
        if self.headers.get('Origin') and self.headers['Origin'] not in origins:
            return self.send({'error': 'Invalid origin.'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 12000 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Expected a small JSON request.')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Expected an object.')
            if self.path == '/api/connect':
                token = data.get('token', '')
                if not isinstance(token, str) or not 12 < len(token) < 512 or any(c.isspace() for c in token):
                    raise ValueError('Enter a valid Apify token.')
                Apify(token).request('/users/me')
                app.token = token
                return self.send({'connected': True})
            if self.path == '/api/disconnect':
                app.token = ''
                return self.send({'connected': False})
            if self.path == '/api/scan':
                return self.send(app.start_scan(data), 202)
            if self.path == '/api/feedback':
                report = app.report(data.get('mode', 'demo'), data.get('snapshot_id', ''))
                if data.get('event_id') not in {e['id'] for e in report['events']}:
                    raise ValueError('Finding not found in this snapshot.')
                app.store.set_feedback(report['snapshot']['id'], data['event_id'], data.get('value', ''))
                return self.send({'ok': True})
            return self.send({'error': 'Not found.'}, 404)
        except (ValueError, TypeError, KeyError) as error:
            return self.send({'error': str(error)}, 400)
        except RuntimeError as error:
            return self.send({'error': str(error)}, 502)
        except Exception:
            return self.send({'error': 'Operation failed. No run-start request is automatically retried.'}, 500)


def make_server(port=8920, data_dir=None):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.application = Application(data_dir or ROOT / 'data')
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Campaign Radar local dashboard')
    parser.add_argument('--port', type=int, default=8920)
    args = parser.parse_args()
    server = make_server(args.port)
    print('Campaign Radar: http://127.0.0.1:%d/' % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.application.token = ''
        server.server_close()
