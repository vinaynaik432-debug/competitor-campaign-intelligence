"""Backend invariants. All provider/HTTP/DNS activity is mocked; no paid calls."""
import copy
import csv
import io
import json
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import app
from intelligence import apify, landing
from intelligence.store import Store


def snapshot(identifier, timestamp, scope=None, text="Book a demo"):
    return {
        "id": identifier, "collected_at": timestamp, "mode": "live",
        "scope": scope or {"platform": "linkedin", "date_range": "last-30-days", "countries": [], "max_ads_per_company": 10},
        "companies": [{"key": "hubspot", "name": "HubSpot", "status": "partial", "notes": [],
                       "ads": [{"id": "123", "headline": "Your CRM", "text": text, "cta": "Learn more",
                                "details_status": "ok", "library_url": "https://www.linkedin.com/ad-library/detail/123"}]}],
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.path)

    def test_concurrent_reservations_cannot_exceed_cap(self):
        barrier = threading.Barrier(12)
        def reserve(index):
            local = Store(self.path)
            barrier.wait(timeout=15)
            try:
                local.reserve("run-%d" % index, .5)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(12)))
        self.assertEqual(sum(results), 10)
        self.assertEqual(self.store.budget(), {"cap_usd": 5.0, "spent_usd": 0.0, "reserved_usd": 5.0, "remaining_usd": 0.0, "unknown_cost_runs": 10})
        with self.assertRaises(ValueError):
            self.store.reserve("one-more", .01)

    def test_pending_allowance_and_reported_cost_survive_restart(self):
        self.store.reserve("uncertain-start", .1)
        self.store.reserve("completed", .1)
        self.store.run_status("completed", "publicrunid", "SUCCEEDED", .002)
        reopened = Store(self.path)
        self.assertEqual(reopened.budget(), {"cap_usd": 5.0, "spent_usd": .002, "reserved_usd": .198, "remaining_usd": 4.8, "unknown_cost_runs": 1})
        with reopened.connect() as db:
            row = db.execute("SELECT status FROM reservations WHERE id='uncertain-start'").fetchone()
        self.assertEqual(row["status"], "start_pending")

    def test_unexpected_overcharge_uses_actual_allocation(self):
        self.store.reserve('overcharge', .1)
        self.store.run_status('overcharge', 'run123', 'SUCCEEDED', 4.95)
        self.assertEqual(self.store.budget()['remaining_usd'], .05)
        with self.assertRaises(ValueError):
            self.store.reserve('another', .1)

    def test_invalid_allowance_and_feedback_fail_without_writes(self):
        for amount in (0, -.1, 5.01, float("inf"), float("nan")):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.store.reserve("invalid", amount)
        self.assertEqual(self.store.budget()["remaining_usd"], 5)
        with self.assertRaises(ValueError):
            self.store.set_feedback("s", "e", "invented-outcome")
        self.store.set_feedback("s", "e", "useful")
        self.assertEqual(Store(self.path).feedback("s"), {"e": "useful"})
        self.store.set_feedback("s", "e", "")
        self.assertEqual(self.store.feedback("s"), {})


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.application = app.Application(self.temp.name)

    def test_empty_live_report_and_first_live_snapshot_are_baselines(self):
        report = self.application.report("live")
        self.assertIsNone(report["snapshot"])
        self.assertEqual(report["events"], [])
        self.assertEqual(report["summary"]["collection_status"], "empty")
        self.application.store.save_snapshot(snapshot("first", "2026-10-20T09:00:00Z"))
        report = self.application.report("live")
        self.assertEqual(report["snapshot"]["id"], "first")
        self.assertTrue(report["summary"]["baseline"])
        self.assertIsNone(report["previous"])
        self.assertEqual(report["events"], [])

    def test_report_selects_comparable_prior_snapshot_not_changed_scope(self):
        first = snapshot("first", "2026-10-20T09:00:00Z")
        different = snapshot("different", "2026-10-21T09:00:00Z")
        different["scope"]["countries"] = ["DE"]
        latest = snapshot("latest", "2026-10-22T09:00:00Z", text="Start a free trial")
        for entry in (first, different, latest):
            self.application.store.save_snapshot(entry)
        changed_report = self.application.report("live", "different")
        self.assertTrue(changed_report["summary"]["baseline"])
        self.assertEqual(changed_report["events"], [])
        latest_report = self.application.report("live", "latest")
        self.assertEqual(latest_report["previous"]["id"], "first")
        self.assertEqual(len(latest_report["events"]), 1)
        self.assertEqual(latest_report["events"][0]["before_at"], first["collected_at"])
        with self.assertRaises(ValueError):
            self.application.report("demo", "latest")

    def test_secret_is_not_returned_or_persisted_and_restart_disconnects(self):
        secret = "test-secret-only-in-memory-123"
        self.application.token = secret
        bootstrap = self.application.bootstrap()
        self.assertTrue(bootstrap["connected"])
        self.assertNotIn(secret, json.dumps(bootstrap))
        for path in Path(self.temp.name).iterdir():
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertFalse(app.Application(self.temp.name).bootstrap()["connected"])

    def test_demo_has_no_provider_dependency(self):
        with patch.object(apify.Apify, "request", side_effect=AssertionError("No network in demo")):
            report = self.application.report("demo")
        self.assertEqual(report["summary"]["companies"], 5)
        self.assertEqual(report["summary"]["changes"], 5)
        self.assertEqual(self.application.store.budget()["remaining_usd"], 5)

    def test_scan_needs_key_valid_parameters_and_available_lock(self):
        with self.assertRaises(ValueError):
            self.application.start_scan({})
        self.application.token = "dummy-memory-token"
        for value in (True, 0, 100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.application.start_scan({"max_ads_per_company": value})
        self.application.lock.acquire()
        try:
            with self.assertRaises(ValueError):
                self.application.start_scan({"max_ads_per_company": 5})
        finally:
            self.application.lock.release()


class CsvTests(unittest.TestCase):
    def test_formula_injection_is_neutralized_after_csv_parsing(self):
        values = ["=HYPERLINK(\"https://evil.example\")", " +SUM(1,2)", "-1+2", "@SUM(A1)", "\tformula", "\n=1", "ordinary, quoted text"]
        encoded = app.csv_bytes([{"text": value} for value in values], ["text"])
        rows = list(csv.DictReader(io.StringIO(encoded.decode("utf-8-sig"))))
        self.assertEqual([row["text"] for row in rows[:-1]], ["'" + value for value in values[:-1]])
        self.assertEqual(rows[-1]["text"], values[-1])


class HttpGuardTests(unittest.TestCase):
    """Exercise the real handler with in-memory streams, without opening a port."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.application = app.Application(self.temp.name)

    def request(self, method, path, body=None, extra_headers=None):
        handler = app.Handler.__new__(app.Handler)
        handler.server = SimpleNamespace(server_port=8920, application=self.application)
        handler.path = path
        handler.headers = Message()
        headers = {"Host": "127.0.0.1:8920", "Content-Type": "application/json"}
        headers.update(extra_headers or {})
        payload = json.dumps(body if body is not None else {}).encode()
        headers.setdefault("Content-Length", str(len(payload)))
        for key, value in headers.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(payload)
        responses = []
        def send(value, status=200, **kwargs):
            responses.append((status, value, kwargs))
        handler.send = send
        getattr(handler, "do_" + method)()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_foreign_host_is_denied_for_get_and_post(self):
        self.assertEqual(self.request("GET", "/api/bootstrap", extra_headers={"Host": "attacker.example:8920"})[0], 403)
        status, _, _ = self.request("POST", "/api/disconnect", extra_headers={"Host": "attacker.example:8920", "X-Session-Token": self.application.csrf})
        self.assertEqual(status, 403)

    def test_post_requires_session_token_and_rejects_foreign_origin(self):
        self.assertEqual(self.request("POST", "/api/disconnect")[0], 403)
        self.assertEqual(self.request("POST", "/api/disconnect", extra_headers={"X-Session-Token": "wrong"})[0], 403)
        status, _, _ = self.request("POST", "/api/disconnect", extra_headers={"X-Session-Token": self.application.csrf, "Origin": "https://attacker.example"})
        self.assertEqual(status, 403)

    def test_valid_connect_is_memory_only_and_cleared_on_disconnect(self):
        headers = {"X-Session-Token": self.application.csrf, "Origin": "http://127.0.0.1:8920"}
        secret = "test-only-apify-token-12345"
        with patch.object(app.Apify, "request", return_value={"data": {"id": "user"}}) as provider:
            status, response, _ = self.request("POST", "/api/connect", {"token": secret}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(response, {"connected": True})
        provider.assert_called_once_with("/users/me")
        self.assertEqual(self.application.token, secret)
        self.assertNotIn(secret, json.dumps(self.request("GET", "/api/bootstrap")[1]))
        status, response, _ = self.request("POST", "/api/disconnect", extra_headers=headers)
        self.assertEqual((status, response, self.application.token), (200, {"connected": False}, ""))

    def test_non_json_and_oversized_posts_are_rejected_before_processing(self):
        base = {"X-Session-Token": self.application.csrf}
        for extra in ({"Content-Type": "text/plain"}, {"Content-Length": "12001"}):
            with self.subTest(extra=extra):
                status, _, _ = self.request("POST", "/api/disconnect", extra_headers=dict(base, **extra))
                self.assertEqual(status, 400)


def dns_result(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]


class LandingTests(unittest.TestCase):
    def test_private_and_mixed_dns_answers_are_rejected(self):
        for address in ("127.0.0.1", "10.0.0.2", "192.168.1.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1"):
            with self.subTest(address=address), patch.object(landing.socket, "getaddrinfo", return_value=dns_result(address)), self.assertRaises(ValueError):
                landing.validate_url("https://public-looking.example/page")
        mixed = dns_result("8.8.8.8") + dns_result("127.0.0.1")
        with patch.object(landing.socket, "getaddrinfo", return_value=mixed), self.assertRaises(ValueError):
            landing.validate_url("https://mixed.example")

    def test_local_names_credentials_and_nonstandard_ports_fail_before_dns(self):
        for url in ("file:///etc/passwd", "http://localhost", "https://foo.local", "http://foo.internal", "https://user:secret@example.com", "https://example.com:8443"):
            with self.subTest(url=url), patch.object(landing.socket, "getaddrinfo") as dns, self.assertRaises(ValueError):
                landing.validate_url(url)
            dns.assert_not_called()

    def test_https_socket_uses_validated_ip_and_original_hostname_for_tls(self):
        connection = landing.PinnedHTTPS("public.example", "8.8.8.8", 443)
        context = MagicMock()
        connection._context = context
        with patch.object(landing.socket, "create_connection", return_value=object()) as connect, patch.object(landing.socket, "getaddrinfo") as dns:
            connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), 12)
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "public.example")
        dns.assert_not_called()

    def test_redirect_is_revalidated_and_private_target_never_connected(self):
        response = MagicMock(status=302)
        response.getheader.side_effect = lambda key: "http://127.0.0.1/private" if key == "Location" else None
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(landing.socket, "getaddrinfo", side_effect=[dns_result("8.8.8.8"), dns_result("127.0.0.1")]) as dns, patch.object(landing, "PinnedHTTPS", return_value=connection) as https, patch.object(landing.http.client, "HTTPConnection") as http:
            result = landing.capture_page("https://public.example/offer")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(dns.call_count, 2)
        https.assert_called_once_with("public.example", "8.8.8.8", 443)
        http.assert_not_called()
        connection.close.assert_called_once()

    def test_meaningful_html_excludes_chrome_and_prefers_main(self):
        parser = landing.PageText()
        parser.feed('<html><head><title>CRM &amp; teams</title><style>bad-style</style></head><body><header>Navigation</header><p>Outside main</p><main><h1>CRM for revenue teams</h1><div class="cookie-banner">Accept cookies</div><p>Start a <strong>30-day free trial</strong>.</p><script>secret-script</script><nav>More links</nav></main><footer>Copyright</footer></body></html>')
        self.assertEqual(parser.titles, ["CRM & teams"])
        text = " ".join(parser.main_text or parser.all_text)
        self.assertIn("30-day free trial", text)
        for absent in ("Navigation", "Outside main", "Accept cookies", "secret-script", "More links", "Copyright", "bad-style"):
            self.assertNotIn(absent, text)

    def test_capture_rejects_non_html_oversized_and_bot_pages(self):
        responses = []
        for content_type, body in (("application/json", b"{}"), ("text/html", b"x" * (landing.MAX_BYTES + 1)), ("text/html", b"<main>Verify you are human before accessing this website. Please wait while we check your browser and connection.</main>")):
            response = MagicMock(status=200)
            response.getheader.side_effect = lambda key, value=content_type: value if key == "Content-Type" else None
            response.read.return_value = body
            connection = MagicMock()
            connection.getresponse.return_value = response
            with patch.object(landing.socket, "getaddrinfo", return_value=dns_result("8.8.8.8")), patch.object(landing, "PinnedHTTPS", return_value=connection):
                responses.append(landing.capture_page("https://public.example/page"))
        self.assertTrue(all(r["status"] == "failed" and not r["text"] for r in responses))


class ApifyContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.raw = Path(self.temp.name) / "raw"

    def test_paid_request_is_never_retried_after_uncertain_error(self):
        api = apify.Apify("fake-token")
        api.opener = MagicMock()
        api.opener.open.side_effect = URLError("timeout")
        with patch.object(apify.time, "sleep") as sleep, self.assertRaises(RuntimeError):
            api.request("/acts/test/runs", method="POST", body={})
        self.assertEqual(api.opener.open.call_count, 1)
        sleep.assert_not_called()
        request = api.opener.open.call_args.args[0]
        self.assertNotIn("fake-token", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-token")

    def test_successful_fake_collection_keeps_samples_partial_and_redacts_token(self):
        token = "only-in-memory-secret-test"
        fake = MagicMock()
        fake.verify_pricing.return_value = {"pricingModel": "PAY_PER_EVENT"}
        requests = []
        def start(path, method="GET", body=None, query=None):
            requests.append((path, method, copy.deepcopy(body), copy.deepcopy(query)))
            index = len(requests) - 1
            return {"data": {"id": "run%d" % index, "status": "SUCCEEDED", "defaultDatasetId": "data%d" % index,
                             "options": {"maxTotalChargeUsd": .1}, "usageTotalUsd": .002}}
        def dataset(identifier):
            index = int(identifier[4:])
            company = apify.WATCHLIST[index]
            return ([{"ad_id": str(1000 + index), "advertiser_company_id": company["linkedin_id"],
                      "advertiser_name": company["name"], "commentary": "Public copy " + token, "details_status": "ok"}], False)
        fake.request.side_effect = start
        fake.dataset.side_effect = dataset
        with patch.object(apify, "Apify", return_value=fake), patch.object(apify, "capture_page", side_effect=AssertionError("Page capture disabled")):
            result = apify.collect(token, self.store, self.raw, max_ads=5, capture_pages=False)
        self.assertEqual(len(result["companies"]), 5)
        self.assertTrue(all(c["status"] == "partial" and len(c["ads"]) == 1 for c in result["companies"]))
        self.assertEqual(len(requests), 5)
        for index, (_, method, body, query) in enumerate(requests):
            self.assertEqual(method, "POST")
            self.assertEqual(body["companyIds"], [apify.WATCHLIST[index]["linkedin_id"]])
            self.assertEqual(query["maxTotalChargeUsd"], .1)
            self.assertEqual(query["restartOnError"], "false")
        self.assertEqual(self.store.budget()["remaining_usd"], 4.5)
        self.assertEqual(self.store.budget()["spent_usd"], .01)
        self.assertNotIn(token, json.dumps(self.store.snapshots()))
        for path in self.raw.rglob("*.json"):
            self.assertNotIn(token, path.read_text())

    def test_uncertain_start_halts_further_paid_calls_and_retains_reservation(self):
        fake = MagicMock()
        fake.verify_pricing.return_value = {"pricingModel": "PAY_PER_EVENT"}
        fake.request.side_effect = RuntimeError("A run start may have timed out")
        with patch.object(apify, "Apify", return_value=fake):
            result = apify.collect("fake-token", self.store, self.raw, max_ads=5, capture_pages=False)
        self.assertEqual(fake.request.call_count, 1)
        self.assertEqual(len(result["companies"]), 5)
        self.assertTrue(all(c["status"] == "failed" for c in result["companies"]))
        self.assertEqual(self.store.budget()["reserved_usd"], .1)
        self.assertEqual(self.store.budget()["remaining_usd"], 4.9)
        self.assertIn("Not started", result["companies"][1]["notes"][0])


if __name__ == "__main__":
    unittest.main()
