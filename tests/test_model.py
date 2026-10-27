import copy
import json
from pathlib import Path
import unittest

from intelligence.model import canonical_page_url, classify_ad, compare_snapshots, normalize_ad, safe_url


ROOT = Path(__file__).resolve().parents[1]


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.company = {"key": "hubspot", "name": "HubSpot", "linkedin_id": "68529"}
        self.raw = {"ad_id": "123456", "ad_url": "https://www.linkedin.com/ad-library/detail/123456",
                    "advertiser_name": "HubSpot", "advertiser_company_id": "68529",
                    "commentary": "A free trial for sales teams", "headline": "CRM for your team", "cta": "Try now",
                    "first_shown": "2026-10-01", "last_shown": "2026-10-20", "details_status": "ok",
                    "targeting": {"language": {"includes": ["English"], "excludes": []}},
                    "has_eu_transparency_data": True, "impressions_range": "1K–5K"}

    def test_actual_oski_fields_and_disclosures(self):
        ad = normalize_ad(self.raw, self.company, "2026-10-27T12:00:00Z")
        self.assertEqual(ad["text"], self.raw["commentary"])
        self.assertEqual(ad["identity_status"], "verified_id")
        self.assertEqual(ad["start_date"], "2026-10-01")
        self.assertEqual(ad["targeting"], self.raw["targeting"])
        self.assertTrue(ad["disclosures"]["has_eu_transparency_data"])
        self.assertEqual(ad["landing_url"], "")
        self.assertEqual(classify_ad(ad)["audience_terms"], ["sales teams"])
        self.assertNotIn("sales teams", json.dumps(ad["targeting"]))

    def test_wrong_advertiser_rejected_even_with_matching_name(self):
        self.raw["advertiser_company_id"] = "123"
        self.assertIsNone(normalize_ad(self.raw, self.company, "now"))

    def test_no_fabricated_id_or_identity(self):
        del self.raw["ad_id"]
        self.assertIsNone(normalize_ad(self.raw, self.company, "now"))
        self.raw["ad_id"] = "123456"
        del self.raw["advertiser_company_id"]
        self.assertEqual(normalize_ad(self.raw, self.company, "now")["identity_status"], "name_only")
        self.raw["advertiser_name"] = "Unrelated brand"
        self.assertEqual(normalize_ad(self.raw, self.company, "now")["identity_status"], "unverified")

    def test_ambiguous_landing_links_remain_unknown(self):
        self.raw["commentary_links"] = [{"url": "https://brand.example/offer"}, {"href": "https://brand.example/blog"}]
        ad = normalize_ad(self.raw, self.company, "now")
        self.assertEqual(ad["landing_url"], "")
        self.assertEqual(ad["landing_url_source"], "")
        self.assertEqual(len(ad["landing_url_candidates"]), 2)
        self.raw["commentary_links"] = ["https://brand.example/offer?utm_source=one", "https://brand.example/offer?utm_source=two"]
        ad = normalize_ad(self.raw, self.company, "now")
        self.assertTrue(ad["landing_url"].startswith("https://brand.example/offer"))
        self.assertEqual(ad["landing_url_source"], "commentary_link")

    def test_destination_provenance_and_external_link_validation(self):
        self.raw["message_ctas"] = [{"url": "https://brand.example/trial"}]
        ad = normalize_ad(self.raw, self.company, "now")
        self.assertEqual(ad["landing_url_source"], "message_cta_link")
        self.raw["commentary_links"] = ["https://brand.example/article"]
        self.assertEqual(normalize_ad(self.raw, self.company, "now")["landing_url"], "")
        self.raw["destination_url"] = "https://brand.example/offer"
        ad = normalize_ad(self.raw, self.company, "now")
        self.assertEqual(ad["landing_url"], "https://brand.example/offer")
        self.assertEqual(ad["landing_url_source"], "explicit_destination")
        self.raw["destination_url"] = "https://www.linkedin.com/company/hubspot/"
        self.assertEqual(normalize_ad(self.raw, self.company, "now")["landing_url"], "")
        self.raw["message_ctas"] = []
        self.raw["commentary_links"] = []
        for bad in ("https://linkedin.com/feed", "javascript:bad", "https://user:secret@brand.example/"):
            self.raw["destination_url"] = bad
            self.assertEqual(normalize_ad(self.raw, self.company, "now")["landing_url"], "")

    def test_numeric_company_url_fallback_requires_trusted_host_and_matching_id(self):
        del self.raw["advertiser_company_id"]
        self.raw["advertiser_url"] = "https://www.linkedin.com/company/68529/?trackingId=public"
        ad = normalize_ad(self.raw, self.company, "now")
        self.assertEqual((ad["advertiser_id"], ad["identity_status"], ad["identity_source"]), ("68529", "verified_id", "advertiser_url"))
        self.raw["advertiser_url"] = "https://www.linkedin.com/company/123/"
        self.assertIsNone(normalize_ad(self.raw, self.company, "now"))
        for unknown in ("https://linkedin.com.attacker.example/company/68529/", "https://www.linkedin.com/company/hubspot/"):
            self.raw["advertiser_url"] = unknown
            self.assertEqual(normalize_ad(self.raw, self.company, "now")["identity_status"], "name_only")

    def test_urls_reject_credentials_and_unsafe_schemes(self):
        self.assertEqual(safe_url("javascript:alert(1)"), "")
        self.assertEqual(safe_url("https://user:secret@example.com"), "")
        self.assertEqual(safe_url("https://example.com:bad/"), "")
        self.raw["ad_url"] = "https://linkedin.com.attacker.example/ad"
        self.assertEqual(normalize_ad(self.raw, self.company, "now")["library_url"], "https://www.linkedin.com/ad-library/detail/123456")

    def test_page_key_does_not_erase_semantic_query(self):
        self.assertEqual(canonical_page_url("https://Example.com/page?plan=pro&utm_source=ad#hero"), "https://example.com/page?plan=pro")
        self.assertNotEqual(canonical_page_url("https://example.com/page?plan=pro"), canonical_page_url("https://example.com/page?plan=basic"))


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((ROOT / "examples/demo.json").read_text())
        self.before, self.after = copy.deepcopy(fixture["snapshots"])

    def test_demo_covers_five_changes_with_stable_evidence_ids(self):
        events = compare_snapshots(self.before, self.after)
        self.assertEqual(len(events), 5)
        self.assertEqual({e["type"] for e in events}, {"offer_changed", "messaging_changed", "landing_page_changed", "first_observed", "not_observed"})
        self.assertEqual(events, compare_snapshots(self.before, self.after))
        self.assertTrue(all(e["before_at"] and e["after_at"] and e["explanation"] for e in events))
        self.assertFalse(any(e["company_key"] == "bridgeflow" for e in events))
        self.assertFalse(any(e["company_key"] == "signalsuite" for e in events))

    def test_baseline_and_incompatible_scopes_produce_no_claims(self):
        self.assertEqual(compare_snapshots(None, self.after), [])
        self.assertEqual(compare_snapshots(self.after, self.after), [])
        self.after["mode"] = "live"
        self.assertEqual(compare_snapshots(self.before, self.after), [])
        self.after["mode"] = "demo"
        self.after["scope"]["countries"] = ["DE"]
        self.assertEqual(compare_snapshots(self.before, self.after), [])

    def test_country_order_is_not_a_scope_change(self):
        self.before["scope"]["countries"] = ["FR", "DE"]
        self.after["scope"]["countries"] = ["DE", "FR"]
        self.assertEqual(len(compare_snapshots(self.before, self.after)), 5)

    def test_partial_scan_never_implies_disappearance_but_keeps_present_evidence(self):
        self.after["companies"][2]["status"] = "partial"
        types = {e["type"] for e in compare_snapshots(self.before, self.after)}
        self.assertNotIn("not_observed", types)
        self.assertIn("first_observed", types)
        self.assertIn("landing_page_changed", types)

    def test_previous_partial_scan_suppresses_disappearance(self):
        self.before["companies"][2]["status"] = "partial"
        self.assertFalse(any(e["type"] == "not_observed" for e in compare_snapshots(self.before, self.after)))

    def test_failed_page_and_new_destination_are_not_page_text_diffs(self):
        page = self.after["companies"][2]["ads"][0]["landing_page"]
        page["status"] = "failed"
        self.assertFalse(any(e["type"] == "landing_page_changed" for e in compare_snapshots(self.before, self.after)))
        page["status"] = "ok"
        page["url"] = "https://example.com/new-offer"
        self.assertFalse(any(e["type"] == "landing_page_changed" for e in compare_snapshots(self.before, self.after)))

    def test_duplicate_ids_do_not_count_as_new_ads(self):
        self.after["companies"][0]["ads"].append(copy.deepcopy(self.after["companies"][0]["ads"][0]))
        self.assertEqual(len(compare_snapshots(self.before, self.after)), 5)

    def test_unavailable_ad_details_are_not_a_copy_change(self):
        ad = self.after["companies"][0]["ads"][0]
        ad.update(details_status="unavailable", text="", headline="", cta="")
        self.assertFalse(any(e["company_key"] == "orbitcrm" for e in compare_snapshots(self.before, self.after)))

    def test_unknown_or_missing_ad_details_cannot_imply_removed_offer(self):
        for status in ("unknown", "", None):
            after = copy.deepcopy(self.after)
            ad = after["companies"][0]["ads"][0]
            ad.update(text="", headline="", cta="")
            if status is None:
                ad.pop("details_status", None)
            else:
                ad["details_status"] = status
            with self.subTest(status=status):
                self.assertFalse(any(e["company_key"] == "orbitcrm" for e in compare_snapshots(self.before, after)))
        self.before["companies"][0]["ads"][0]["details_status"] = "unknown"
        self.assertFalse(any(e["company_key"] == "orbitcrm" for e in compare_snapshots(self.before, self.after)))

    def test_new_company_is_its_own_baseline(self):
        self.before["companies"] = self.before["companies"][1:]
        self.assertFalse(any(e["company_key"] == "orbitcrm" for e in compare_snapshots(self.before, self.after)))

    def test_first_observed_and_absence_are_not_launch_or_stop_claims(self):
        events = compare_snapshots(self.before, self.after)
        new = next(e for e in events if e["type"] == "first_observed")
        missing = next(e for e in events if e["type"] == "not_observed")
        self.assertIn("not evidence of its launch", new["explanation"])
        self.assertIn("does not establish that it stopped", missing["explanation"])
        self.assertIn("rolling date windows", missing["explanation"])


if __name__ == "__main__":
    unittest.main()
