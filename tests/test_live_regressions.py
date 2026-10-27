"""Edge cases found while checking the first real collection, without network calls."""
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from intelligence import landing
from intelligence.model import classify_ad


class Response:
    status = 200

    def getheader(self, name):
        return 'text/html' if name == 'Content-Type' else None

    def read(self, limit):
        return b'<html><head><title>LinkedIn</title></head><body>This link will take you to a page that is not on LinkedIn. Because this is an external link, we are unable to verify it for safety. https://example.com/destination</body></html>'


class Connection:
    def __init__(self, *args):
        pass

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return Response()

    def close(self):
        pass


class LiveRegressionTests(unittest.TestCase):
    def test_performance_percentages_are_not_discount_offers(self):
        self.assertEqual(classify_ad({'text': '36% more revenue. 43.6% now use AI; 48.3% plan to spend more.'})['offer_terms'], [])
        self.assertTrue(classify_ad({'text': 'Save 20% on your plan.'})['offer_terms'])
        self.assertTrue(classify_ad({'text': 'Get a 30% discount.'})['offer_terms'])

    def test_shortened_link_interstitial_is_not_destination_evidence(self):
        url = 'https://lnkd.in/example'
        with patch.object(landing, 'validate_url', return_value=(urlsplit(url), 'lnkd.in', 443, '93.184.216.34')):
            with patch.object(landing, 'PinnedHTTPS', Connection):
                result = landing.capture_page(url)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['text'], '')
        self.assertIn('link gateway', result['error'])

    def test_gateway_phrase_on_ordinary_site_is_not_misclassified(self):
        self.assertFalse(landing.is_link_gateway('https://example.com', 'This link will take you to a page'))


if __name__ == '__main__':
    unittest.main()
