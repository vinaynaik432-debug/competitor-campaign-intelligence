"""Publication invariants, without credentials, remote requests or paid services."""
import copy
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from audit_public import audit_bundle, audit_files
from build_public import build, excerpt_fields, public_url, sanitize_ad


class PublicBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = json.loads((ROOT / 'docs/public-data.json').read_text(encoding='utf-8'))

    def test_real_baseline_counts_and_verifiable_ad_ids(self):
        report = self.bundle['reports']['live']
        companies = report['snapshot']['companies']
        self.assertEqual({c['name'] for c in companies}, {'HubSpot', 'Salesforce', 'Pipedrive', 'Zoho', 'Freshworks'})
        self.assertEqual(sum(len(c['ads']) for c in companies), 50)
        self.assertEqual(len({a['id'] for c in companies for a in c['ads']}), 50)
        self.assertEqual(report['events'], [])
        self.assertIsNone(report['previous'])
        self.assertTrue(report['summary']['baseline'])
        for company in companies:
            self.assertEqual(company['status'], 'partial')
            for ad in company['ads']:
                self.assertIn('linkedin.com/ad-library/', ad['library_url'])
                self.assertTrue(ad['is_excerpt'])
                self.assertLessEqual(sum(len(ad.get(k, '').split()) for k in ('headline', 'text', 'cta')), 25)

    def test_fictional_changes_remain_separate_from_real_baseline(self):
        demo = self.bundle['reports']['demo']
        self.assertEqual(len(demo['events']), 5)
        self.assertTrue(demo['previous'])
        self.assertTrue(all(c['name'] not in {'HubSpot', 'Salesforce', 'Pipedrive', 'Zoho', 'Freshworks'} for c in demo['snapshot']['companies']))
        self.assertEqual(demo['feedback'], {})

    def test_whitelist_removes_private_provider_fields_and_page_bodies(self):
        ad = {'id': '123', 'headline': 'Hello', 'text': 'Public copy', 'cta': 'Read',
              'run_id': 'private-run-id', 'usage_usd': .01, 'token': 'private-credential',
              'landing_page': {'url': 'https://example.com', 'status': 'ok', 'text': 'full captured page body', 'title': 'Title'},
              'targeting': {'language': {'raw': 'private unsupported raw field', 'items': ['English']}}}
        result = sanitize_ad(ad, 'live')
        serialized = json.dumps(result)
        for forbidden in ('private-run-id', 'private-credential', 'full captured page body', 'unsupported raw'):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn('text', result['landing_page'])

    def test_urls_drop_tracking_and_secrets_but_keep_content_queries(self):
        url = public_url('https://example.com/pricing?plan=team&utm_source=ad&token=secret#fragment')
        self.assertEqual(url, 'https://example.com/pricing?plan=team')
        for url in ('javascript:alert(1)', 'https://user:password@example.com/', 'http://localhost:8920/', 'http://127.0.0.1/'):
            self.assertEqual(public_url(url), '')

    def test_public_bundle_audit_rejects_credentials_and_private_metadata(self):
        self.assertTrue(audit_bundle(self.bundle))
        for field in ('run_id', 'run_url', 'csrf', 'budget'):
            candidate = copy.deepcopy(self.bundle)
            candidate['bootstrap'][field] = 'should not publish'
            with self.assertRaises(ValueError):
                audit_bundle(candidate)
        candidate = copy.deepcopy(self.bundle)
        candidate['public_notice'] = 'apify_' + 'api_' + 'x' * 30
        with self.assertRaises(ValueError):
            audit_bundle(candidate)
        candidate = copy.deepcopy(self.bundle)
        candidate['reports']['demo']['feedback'] = {'private-review': 'useful'}
        with self.assertRaises(ValueError):
            audit_bundle(candidate)

    def test_offline_clone_rebuilds_from_checked_in_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(ROOT / 'static', root / 'static')
            (root / 'docs').mkdir()
            shutil.copyfile(ROOT / 'docs/public-data.json', root / 'docs/public-data.json')
            rebuilt = build(root)
            self.assertEqual(rebuilt, self.bundle)
            html = (root / 'docs/index.html').read_text()
            self.assertIn('src="./public-api.js"', html)
            self.assertLess(html.index('src="./public-api.js"'), html.index('src="./app.js"'))
            self.assertFalse(re.search(r'(?:href|src)="/(?!/)', html))
            self.assertIn("form-action 'none'", html)
            self.assertTrue((root / 'docs/.nojekyll').exists())

    def test_file_audit_rejects_private_selection(self):
        with self.assertRaises(ValueError):
            audit_files(ROOT, [ROOT / 'data/campaign-radar.sqlite3'])

    def test_generic_secrets_in_sensitive_files_fail_without_prefix_matching(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for filename in ('.env', '.env.production', 'credentials.md', 'secrets.json', 'database.sqlite3', 'id_rsa', 'private.pem', 'session.log'):
                path = root / filename
                path.write_text('SOME_PASSWORD=ordinary-looking-value')
                with self.subTest(filename=filename), self.assertRaises(ValueError):
                    audit_files(root, [path])
                path.unlink()


if __name__ == '__main__':
    unittest.main()
