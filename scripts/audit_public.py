#!/usr/bin/env python3
"""Fail closed on credentials, local paths and private fields in published files."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(rb'(?:apify_api_[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
LOCAL_PATH = re.compile(rb'(?:/' + rb'Users/[^/\s]+/|/' + rb'home/[^/\s]+/|C:\\Users\\)')
PRIVATE_FIELDS = {'token', 'api_key', 'access_token', 'csrf', 'budget', 'reservations', 'run_id', 'run_url', 'usage_usd',
                  'usageTotalUsd', 'pricing', 'cost_known', 'raw', 'eventUsage', 'defaultDatasetId', 'defaultKeyValueStoreId'}
SENSITIVE_NAMES = re.compile(r'^(?:\.env(?:\..*)?|credentials?(?:[._-].*)?|secrets?(?:[._-].*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?)$', re.I)
SENSITIVE_SUFFIXES = re.compile(r'\.(?:sqlite\d*(?:-(?:wal|shm|journal))?|db(?:-journal)?|pem|key|p12|pfx|keystore|log)$', re.I)


def audit_bundle(bundle):
    def visit(value, path='root'):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in PRIVATE_FIELDS:
                    raise ValueError('Private field found at ' + path + '.' + key)
                if key == 'feedback' and child:
                    raise ValueError('Private review feedback must not be exported.')
                if key == 'landing_page' and isinstance(child, dict) and 'text' in child:
                    raise ValueError('Full captured page text must not be exported.')
                visit(child, path + '.' + key)
        elif isinstance(value, list):
            for child in value:
                visit(child, path + '[]')
    visit(bundle)
    encoded = json.dumps(bundle, ensure_ascii=False).encode()
    if SECRET.search(encoded) or LOCAL_PATH.search(encoded) or b'console.apify.com/actors/runs/' in encoded:
        raise ValueError('Sensitive material found in public bundle.')
    live = bundle['reports']['live']
    if live['events'] or live['previous'] or not live['summary']['baseline']:
        raise ValueError('The public live dataset must remain an honest first baseline.')
    for company in live['snapshot']['companies']:
        for ad in company['ads']:
            if sum(len(str(ad.get(k, '')).split()) for k in ('headline', 'text', 'cta')) > 25:
                raise ValueError('Creative excerpt exceeds the public word limit.')
            if not ad.get('is_excerpt') or not ad.get('library_url'):
                raise ValueError('Each public ad excerpt needs a label and public evidence link.')
    return True


def audit_files(root=ROOT, files=None):
    root = Path(root).resolve()
    if files is None:
        files = [p for p in root.rglob('*') if p.is_file() and p.name != '.DS_Store' and not any(x in p.relative_to(root).parts for x in
                 ('.git', '.tools', 'data', '__pycache__', '.venv', 'outputs', '.pytest_cache'))]
    audited = []
    for path in files:
        path = Path(path)
        if path.is_symlink():
            raise ValueError('Linked files cannot be selected for publishing.')
        path = path.resolve()
        relative = path.relative_to(root)
        if any(x in relative.parts for x in ('.git', '.tools', 'data', '__pycache__', '.venv', 'outputs', '.ssh', '.aws', '.codex', '.agents')):
            raise ValueError('Private or linked file selected for publishing: ' + str(relative))
        if SENSITIVE_NAMES.match(path.name) or SENSITIVE_SUFFIXES.search(path.name) or path.name == '.DS_Store':
            raise ValueError('Sensitive filename selected for publishing: ' + str(relative))
        raw = path.read_bytes()
        if SECRET.search(raw) or LOCAL_PATH.search(raw):
            raise ValueError('Sensitive material found in ' + str(relative))
        audited.append(str(relative))
    public_data = root / 'docs/public-data.json'
    if public_data.exists():
        audit_bundle(json.loads(public_data.read_text(encoding='utf-8')))
    return sorted(audited)


if __name__ == '__main__':
    files = audit_files()
    print('Public audit passed for %d source/demo files.' % len(files))
