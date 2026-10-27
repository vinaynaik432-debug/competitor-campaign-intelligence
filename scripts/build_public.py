#!/usr/bin/env python3
"""Build the static portfolio demo from an explicitly sanitized data allowlist.

No network requests or credentials are used. A clone without local research data
reuses the checked-in public bundle, so the build remains reproducible offline.
"""
import copy
import ipaddress
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from intelligence.apify import WATCHLIST
from intelligence.model import classify_ad, compare_snapshots

AD_FIELDS = {'id', 'advertiser_name', 'advertiser_id', 'text', 'headline', 'cta',
             'library_url', 'landing_url', 'landing_url_source', 'format',
             'start_date', 'end_date', 'details_status', 'identity_status'}
DISCLOSURES = {'has_eu_transparency_data', 'impressions_range', 'impressions_min',
               'impressions_max', 'impressions_by_country', 'targeting_parameters', 'paid_by'}
SCOPE_FIELDS = {'platform', 'date_range', 'countries', 'max_ads_per_company', 'actor', 'build', 'capture_pages'}
SECRET_PARAMETERS = {'token', 'access_token', 'api_key', 'apikey', 'key', 'auth', 'authorization', 'signature', 'sig'}


def public_url(value):
    if not isinstance(value, str):
        return ''
    try:
        parts = urlsplit(value)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            return ''
        if parts.hostname == 'localhost' or parts.hostname.endswith(('.localhost', '.local', '.internal')):
            return ''
        try:
            if not ipaddress.ip_address(parts.hostname).is_global:
                return ''
        except ValueError:
            pass
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in SECRET_PARAMETERS
                 and k.lower() not in {'gclid', 'fbclid', 'msclkid', 'li_fat_id'}]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
    except ValueError:
        return ''


def excerpt_fields(ad, limit=25):
    """Limit the total quoted creative to 25 words, across all visible fields."""
    result = {}
    remaining = limit
    for key in ('headline', 'text', 'cta'):
        words = str(ad.get(key) or '').split()
        chosen = words[:remaining]
        result[key] = ' '.join(chosen)
        if len(words) > len(chosen) and chosen:
            result[key] += '…'
        remaining -= len(chosen)
    return result


def safe_structure(value, depth=0):
    """Retain public transparency values, without executable content or huge blobs."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(k)[:100]: safe_structure(v, depth + 1) for k, v in list(value.items())[:100]
                if str(k).lower() not in SECRET_PARAMETERS | {'raw'}}
    if isinstance(value, list):
        return [safe_structure(v, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        return value[:3000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return None


def sanitize_ad(ad, mode):
    result = {k: str(ad[k] or '') for k in AD_FIELDS if k in ad}
    for key in ('library_url', 'landing_url'):
        result[key] = public_url(result.get(key, ''))
    if mode == 'live':
        result.update(excerpt_fields(ad))
        result['is_excerpt'] = True
        result['excerpt_notice'] = 'Public excerpt: up to 25 words across headline, copy and CTA. Open the LinkedIn source for full creative.'
    result['classification'] = classify_ad(result)
    result['targeting'] = safe_structure(ad.get('targeting', {}))
    result['disclosures'] = {k: safe_structure(v) for k, v in ad.get('disclosures', {}).items() if k in DISCLOSURES}
    page = ad.get('landing_page')
    if isinstance(page, dict):
        result['landing_page'] = {'url': public_url(page.get('url', '')), 'status': page.get('status', 'failed'),
                                  'title': ' '.join(str(page.get('title', '')).split()[:12]),
                                  'fetched_at': page.get('fetched_at', '')}
        if page.get('error'):
            result['landing_page']['error'] = 'Destination capture unavailable. Review the public source link manually.'
    return result


def sanitize_snapshot(snapshot, mode):
    result = {'id': ('baseline-' + snapshot['collected_at'][:10]) if mode == 'live' else snapshot['id'],
              'collected_at': snapshot['collected_at'], 'mode': mode,
              'scope': {k: copy.deepcopy(v) for k, v in snapshot.get('scope', {}).items() if k in SCOPE_FIELDS},
              'companies': []}
    for company in snapshot['companies']:
        notes = ['Bounded sample of public ads; this is not a complete inventory.'] if mode == 'live' else ['Fictional company and creative for the walkthrough.']
        if mode == 'live' and len(company.get('ads', [])) >= snapshot.get('scope', {}).get('max_ads_per_company', 10):
            notes.append('Collection limit reached; more ads may exist.')
        if company.get('status') == 'failed':
            notes.append('This collection is unavailable; do not infer campaign status from absence.')
        result['companies'].append({'key': company['key'], 'name': company['name'],
                                    'linkedin_url': public_url(company.get('linkedin_url', '')),
                                    'status': company['status'], 'notes': notes,
                                    'ads': [sanitize_ad(a, mode) for a in company.get('ads', [])]})
    return result


def public_report(snapshot, mode, previous=None):
    # Derive fictional changes before removing captured page text from ad records.
    events = compare_snapshots(previous, snapshot) if mode == 'demo' else []
    current = sanitize_snapshot(snapshot, mode)
    companies = current['companies']
    statuses = {c['status'] for c in companies}
    status = 'complete' if statuses == {'complete'} else ('failed' if statuses == {'failed'} else 'partial')
    return {'mode': mode, 'snapshot': current,
            'previous': {'id': previous['id'], 'collected_at': previous['collected_at']} if previous else None,
            'events': events, 'feedback': {},
            'summary': {'companies': len(companies), 'ads': sum(len(c['ads']) for c in companies),
                        'changes': len(events), 'collection_status': status, 'baseline': previous is None},
            'company_summary': [{k: c[k] for k in ('key', 'name', 'status', 'notes')} | {'ad_count': len(c['ads'])} for c in companies],
            'methodology': ('Public portfolio snapshot collected on ' + snapshot['collected_at'][:10] + '. '
                'Real ads use short excerpts and source links. Public ads do not establish spend, conversions or performance. '
                'Live observations are a baseline; sample changes use fictional companies. Feedback stays in this browser tab only.')}


def make_bundle(root=ROOT):
    root = Path(root)
    db_path = root / 'data/campaign-radar.sqlite3'
    saved_path = root / 'docs/public-data.json'
    if not db_path.exists():
        if not saved_path.exists():
            raise ValueError('No local baseline or checked-in public bundle is available.')
        return json.loads(saved_path.read_text(encoding='utf-8'))
    connection = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT body FROM snapshots ORDER BY collected_at DESC, id DESC LIMIT 1').fetchone()
    finally:
        connection.close()
    if not row:
        if saved_path.exists():
            return json.loads(saved_path.read_text(encoding='utf-8'))
        raise ValueError('Collect a real baseline before exporting the live portfolio sample.')
    live = json.loads(row[0])
    demos = json.loads((root / 'examples/demo.json').read_text(encoding='utf-8'))['snapshots']
    reports = {'live': public_report(live, 'live'), 'demo': public_report(demos[-1], 'demo', demos[-2])}
    snapshots = [{'id': r['snapshot']['id'], 'collected_at': r['snapshot']['collected_at'], 'mode': mode,
                  'ad_count': r['summary']['ads'], 'status': r['summary']['collection_status']} for mode, r in reports.items()]
    return {'schema_version': 1, 'public_notice': 'Historical public ad excerpts and a separate fictional walkthrough. No credentials or private run records.',
            'bootstrap': {'connected': False, 'watchlist': [{k: c[k] for k in ('key', 'name', 'linkedin_url')} for c in WATCHLIST],
                          'snapshots': snapshots, 'job': None}, 'reports': reports}


def build(root=ROOT):
    from audit_public import audit_bundle, audit_files
    root = Path(root)
    bundle = make_bundle(root)
    audit_bundle(bundle)
    output = root / 'docs'
    output.mkdir(exist_ok=True)
    (output / 'public-data.json').write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name in ('app.js', 'style.css', 'public-api.js'):
        shutil.copyfile(root / 'static' / name, output / name)
    html = (root / 'static/index.html').read_text(encoding='utf-8')
    html = re.sub(r'(href|src)="/([^"\s]*)"', r'\1="./\2"', html)
    html, injected = re.subn(r'<script\s+src="\./app\.js"\s+defer>\s*</script>', '<script src="./public-api.js" defer></script>\n  <script src="./app.js" defer></script>', html)
    if injected != 1:
        raise ValueError('Public API adapter could not be injected before the application script.')
    html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n  <meta name="referrer" content="no-referrer">\n  <meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'; style-src \'self\'; img-src \'self\' data:; connect-src \'self\'; base-uri \'self\'; form-action \'none\'">')
    html = re.sub(r'<title>\s*Campaign Radar — Competitor intelligence\s*</title>', '<title>Campaign Radar — LinkedIn competitor campaign intelligence</title>', html)
    (output / 'index.html').write_text(html, encoding='utf-8')
    (output / '.nojekyll').write_text('', encoding='utf-8')
    audit_files(root, [output / n for n in ('index.html', 'app.js', 'style.css', 'public-api.js', 'public-data.json', '.nojekyll')])
    return bundle


if __name__ == '__main__':
    bundle = build()
    print('Built static demo: %d real ad excerpts across %d companies; %d fictional changes.' % (
        bundle['reports']['live']['summary']['ads'], bundle['reports']['live']['summary']['companies'],
        bundle['reports']['demo']['summary']['changes']))
