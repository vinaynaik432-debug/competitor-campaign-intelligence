"""Bounded LinkedIn collection. Paid POST requests are deliberately never retried."""
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .landing import capture_page

ACTOR = 'oski~linkedin-ad-library-scraper'
BUILD = '0.1.8'
RUN_CAP_USD = .10
TERMINAL = {'SUCCEEDED', 'FAILED', 'TIMED-OUT', 'ABORTED'}
WATCHLIST = [
    {'key': 'hubspot', 'name': 'HubSpot', 'linkedin_id': '68529', 'linkedin_url': 'https://www.linkedin.com/company/hubspot/'},
    {'key': 'salesforce', 'name': 'Salesforce', 'linkedin_id': '3185', 'linkedin_url': 'https://www.linkedin.com/company/salesforce/'},
    {'key': 'pipedrive', 'name': 'Pipedrive', 'linkedin_id': '1876917', 'linkedin_url': 'https://www.linkedin.com/company/pipedrive/'},
    {'key': 'zoho', 'name': 'Zoho', 'linkedin_id': '38373', 'linkedin_url': 'https://www.linkedin.com/company/zoho/'},
    {'key': 'freshworks', 'name': 'Freshworks', 'linkedin_id': '1377014', 'linkedin_url': 'https://www.linkedin.com/company/freshworks-inc/'},
]


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_json(path, value, token=''):
    """Strip secrets if a provider unexpectedly echoes them in its output."""
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if token:
        text = text.replace(token, '[redacted]')
    text = re.sub(r'apify_api_[A-Za-z0-9_-]+', '[redacted]', text)
    text = re.sub(r'github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+', '[redacted]', text)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + '\n', encoding='utf-8')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Apify:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect)

    def request(self, path, method='GET', body=None, query=None):
        if not re.fullmatch(r'/[A-Za-z0-9_~./-]+', path):
            raise ValueError('Invalid API path.')
        url = 'https://api.apify.com/v2' + path
        if query:
            url += '?' + urlencode(query)
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        for attempt in range(2 if method == 'GET' else 1):
            try:
                with self.opener.open(Request(url, data=payload, headers=headers, method=method), timeout=25) as response:
                    raw = response.read(15_000_001)
                    if len(raw) > 15_000_000:
                        raise RuntimeError('Apify response exceeded the safety limit.')
                    return json.loads(raw)
            except HTTPError as error:
                if method == 'GET' and error.code in {429, 500, 502, 503, 504} and attempt == 0:
                    time.sleep(3)
                    continue
                # Do not log URLs, response bodies or headers containing credentials.
                raise RuntimeError('Apify returned HTTP %d. Check your account and saved run links.' % error.code) from None
            except (URLError, TimeoutError, OSError, ValueError):
                if method == 'GET' and attempt == 0:
                    time.sleep(2)
                    continue
                raise RuntimeError('Apify request did not complete. A run start is never automatically retried; check Apify before starting again.') from None

    def dataset(self, dataset_id, limit=100):
        if not re.fullmatch(r'[A-Za-z0-9]+', dataset_id):
            raise ValueError('Invalid dataset ID.')
        rows = []
        while len(rows) < limit:
            batch = self.request('/datasets/%s/items' % dataset_id,
                                 query={'format': 'json', 'clean': 'false', 'offset': len(rows), 'limit': min(50, limit - len(rows))})
            if not isinstance(batch, list):
                raise RuntimeError('Unexpected Apify dataset format.')
            rows.extend(batch)
            if len(batch) < 50:
                return rows, False
        return rows, True

    def verify_pricing(self):
        metadata = self.request('/acts/' + ACTOR)['data']
        entries = [p for p in metadata.get('pricingInfos', []) if p.get('startedAt', '') <= now()]
        price = max(entries, key=lambda p: p.get('startedAt', ''), default={})
        if price.get('pricingModel') != 'PAY_PER_EVENT' or (price.get('minimalMaxTotalChargeUsd') or 0) > RUN_CAP_USD:
            raise RuntimeError('Actor pricing changed or its minimum exceeds the per-run cap. Review the provider before collection.')
        return price


def collect(token, store, raw_root, max_ads=10, capture_pages=True, progress=lambda message: None):
    from .model import normalize_ad
    if max_ads not in {5, 10, 20}:
        raise ValueError('Choose 5, 10 or 20 ads per company.')
    if store.budget()['remaining_usd'] + 1e-8 < len(WATCHLIST) * RUN_CAP_USD:
        raise ValueError('At least US$0.50 of unallocated allowance is needed for this five-company scan.')
    api = Apify(token)
    pricing = api.verify_pricing()
    snapshot_id = 'live-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
    scope = {'platform': 'linkedin', 'date_range': 'last-30-days', 'countries': [],
             'max_ads_per_company': max_ads, 'actor': ACTOR, 'build': BUILD, 'capture_pages': capture_pages}
    snapshot = {'id': snapshot_id, 'collected_at': now(), 'mode': 'live', 'scope': scope, 'companies': []}
    run_dir = Path(raw_root) / snapshot_id
    page_cache = {}
    for index, company in enumerate(WATCHLIST):
        entry = dict(company, status='failed', notes=[], ads=[])
        snapshot['companies'].append(entry)
        reservation_id = snapshot_id + '-' + company['key']
        run_id = None
        payload = {'companyIds': [company['linkedin_id']], 'countries': [], 'dateRange': 'last-30-days',
                   'includeDetails': True, 'maxAdsPerSearch': max_ads, 'maxAds': max_ads}
        record = {'actor': ACTOR, 'build': BUILD, 'company': company['key'], 'input': payload,
                  'max_total_charge_usd': RUN_CAP_USD, 'pricing': pricing, 'created_at': now(), 'status': 'start_pending'}
        progress('Collecting %s (%d of 5)…' % (company['name'], index + 1))
        try:
            store.reserve(reservation_id, RUN_CAP_USD)
            safe_json(run_dir / (company['key'] + '-run.json'), record, token)
            run = api.request('/acts/' + ACTOR + '/runs', method='POST', body=payload,
                              query={'build': BUILD, 'maxTotalChargeUsd': RUN_CAP_USD, 'timeout': 180, 'restartOnError': 'false'})['data']
            run_id = run['id']
            if not re.fullmatch(r'[A-Za-z0-9]+', run_id):
                raise RuntimeError('Unexpected run identifier. Check your Apify console.')
            entry['run_id'] = run_id
            entry['run_url'] = 'https://console.apify.com/actors/runs/' + run_id
            record.update(run_id=run_id, status=run['status'], url=entry['run_url'])
            store.run_status(reservation_id, run_id, run['status'])
            safe_json(run_dir / (company['key'] + '-run.json'), record, token)
            effective = run.get('options', {}).get('maxTotalChargeUsd')
            if (isinstance(effective, bool) or not isinstance(effective, (float, int))
                    or not math.isfinite(effective) or effective <= 0 or effective > RUN_CAP_USD + 1e-8):
                api.request('/actor-runs/' + run_id + '/abort', method='POST')
                raise RuntimeError('The provider did not confirm the spending cap. The run was asked to stop.')
            deadline = time.monotonic() + 240
            while run.get('status') not in TERMINAL:
                if time.monotonic() > deadline:
                    raise RuntimeError('Monitoring timed out. Check the saved run link before another collection.')
                time.sleep(3)
                run = api.request('/actor-runs/' + run_id)['data']
            cost = run.get('usageTotalUsd')
            store.run_status(reservation_id, run_id, run['status'], cost)
            if isinstance(cost, (float, int)) and math.isfinite(cost) and cost > RUN_CAP_USD + 1e-8:
                raise RuntimeError('Provider reported a charge above the reserved cap. Further starts are stopped; review billing before continuing.')
            if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
                entry['usage_usd'] = cost
            record.update(status=run['status'], usage_usd=cost, finished_at=run.get('finishedAt'), status_message=run.get('statusMessage'))
            rows, truncated = api.dataset(run['defaultDatasetId']) if run.get('defaultDatasetId') else ([], False)
            # Redact before normalizing as well as before writing raw output.
            rows = json.loads(json.dumps(rows).replace(token, '[redacted]'))
            safe_json(run_dir / (company['key'] + '-ads.json'), rows, token)
            excluded, seen = 0, set()
            for row in rows:
                ad = normalize_ad(row, company, snapshot['collected_at']) if isinstance(row, dict) else None
                if not ad or ad.get('identity_status') != 'verified_id':
                    excluded += 1
                    continue
                if ad['id'] not in seen:
                    entry['ads'].append(ad)
                    seen.add(ad['id'])
            # The actor exposes no trustworthy completeness count. Treat every collection
            # as a sample, including zero rows; missing ads never establish campaign end.
            entry['status'] = 'partial' if run['status'] == 'SUCCEEDED' else 'failed'
            entry['notes'].append('Sample of the last 30 days across available countries; library coverage is not guaranteed.')
            if len(entry['ads']) >= max_ads or truncated:
                entry['notes'].append('Collection limit reached. More ads may exist.')
            if excluded:
                entry['notes'].append('%d records excluded because their company identity or ad ID could not be verified.' % excluded)
            if not entry['ads']:
                entry['notes'].append('No verified ads returned. This does not prove that the company is not advertising.')
            if run['status'] != 'SUCCEEDED':
                entry['notes'].append('Provider run finished with status ' + run['status'] + '.')
            captured = 0
            for ad in entry['ads']:
                url = ad.get('landing_url')
                if not capture_pages or not url:
                    continue
                if url in page_cache:
                    ad['landing_page'] = page_cache[url]
                elif captured < 2:
                    progress('Capturing public landing-page text for %s…' % company['name'])
                    page_cache[url] = capture_page(url)
                    ad['landing_page'] = page_cache[url]
                    captured += 1
                else:
                    ad['landing_page'] = {'url': url, 'status': 'skipped', 'text': '', 'title': '',
                                          'fetched_at': now(), 'error': 'Two-page-per-company capture limit.'}
        except Exception as error:
            message = str(error).replace(token, '[redacted]')
            entry['notes'].append(message)
            record['error'] = message
            record['status'] = 'needs_review'
            store.run_status(reservation_id, run_id, 'needs_review')
            # Stop further paid starts when the start/result is uncertain or access fails.
            for remaining in WATCHLIST[index + 1:]:
                snapshot['companies'].append(dict(remaining, status='failed', ads=[],
                    notes=['Not started: a previous provider request needs review.']))
            break
        finally:
            safe_json(run_dir / (company['key'] + '-run.json'), record, token)
    safe_json(run_dir / 'snapshot.json', snapshot, token)
    store.save_snapshot(snapshot)
    return snapshot
