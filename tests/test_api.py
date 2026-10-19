import json
from io import BytesIO
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import server


class APITestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = server.DB_PATH
        server.DB_PATH = Path(self.temp_dir.name) / "test.db"
        server.init_db()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.conn = HTTPConnection("127.0.0.1", self.httpd.server_port)

    def tearDown(self):
        self.conn.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        server.DB_PATH = self.original_db
        self.temp_dir.cleanup()

    def request(self, method, path, data=None, headers=None):
        body = json.dumps(data) if data is not None else None
        self.conn.request(method, path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
        response = self.conn.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw else {}, response.getheader("Set-Cookie")

    def test_missing_supabase_table_returns_schema_setup_instructions(self):
        error = urllib.error.HTTPError(
            "https://example.supabase.co/rest/v1/transactions", 404, "Not Found", {},
            BytesIO(b'{"code":"PGRST205","message":"Could not find table"}'),
        )
        with patch.object(server, "SUPABASE_URL", "https://example.supabase.co"), patch.object(
            server, "SUPABASE_ANON_KEY", "test-key"
        ), patch.object(server.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(server.APIError) as raised:
                server.supabase_request("/rest/v1/transactions")
        self.assertEqual(raised.exception.status, 503)
        self.assertIn("Apply the latest supabase/schema.sql", raised.exception.message)

    def test_public_config_exposes_only_configured_adsense_ids(self):
        with patch.object(server, "ADSENSE_CLIENT_ID", "ca-pub-1234567890123456"), patch.object(
            server, "ADSENSE_SLOT_ID", "1234567890"
        ):
            status, payload, _ = self.request("GET", "/api/public-config")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {
            "adsense_client_id": "ca-pub-1234567890123456",
            "adsense_slot_id": "1234567890",
        })

    def test_quote_cache_is_fresh_for_a_day(self):
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(hours=23)).isoformat()
        stale = (now - timedelta(hours=24)).isoformat()
        self.assertTrue(server.quote_cache_fresh(fresh))
        self.assertFalse(server.quote_cache_fresh(stale))

    def create_account(self):
        with patch.object(server, "supabase_auth", return_value={"user": {"id": "auth-user-1", "email": "person@example.com"}, "access_token": "access"}):
            status, payload, cookie = self.request("POST", "/api/register", {"email": "Person@example.com", "password": "correct horse battery"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["user"]["email"], "person@example.com")
        self.assertTrue(cookie)
        return cookie.split(";", 1)[0]

    def test_market_quote_parses_provider_price_and_day(self):
        response = BytesIO(json.dumps({"Global Quote": {"05. price": "231.7500", "07. latest trading day": "2026-10-16"}}).encode())
        with patch.object(server, "ALPHAVANTAGE_API_KEY", "test-key"), patch.object(server.urllib.request, "urlopen", return_value=response) as open_url:
            quote = server.market_quote("VTI")
        self.assertEqual(quote, {"symbol": "VTI", "price": 231.75, "as_of": "2026-10-16"})
        request = open_url.call_args.args[0]
        self.assertIn("symbol=VTI", request.full_url)
        self.assertNotIn("entitlement=", request.full_url)

    def test_market_daily_series_returns_opening_prices(self):
        response = BytesIO(json.dumps({"Time Series (Daily)": {"2026-10-16": {"1. open": "231.7500", "4. close": "235.00"}}}).encode())
        with patch.object(server, "ALPHAVANTAGE_API_KEY", "test-key"), patch.object(server.urllib.request, "urlopen", return_value=response) as open_url:
            prices = server.market_daily_series("VTI")
        self.assertEqual(prices, {"2026-10-16": 231.75})
        self.assertIn("function=TIME_SERIES_DAILY", open_url.call_args.args[0].full_url)
        self.assertIn("outputsize=compact", open_url.call_args.args[0].full_url)

    def test_account_creation_and_authenticated_overview(self):
        cookie = self.create_account()
        status, payload, _ = self.request("GET", "/api/overview", headers={"Cookie": cookie})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["transactions"], [])
        self.assertEqual(payload["monthly_budget"], 4000)

    def test_account_creation_requires_supabase_user(self):
        with patch.object(server, "supabase_auth", return_value={"user": {}, "access_token": "access"}):
            status, payload, _ = self.request("POST", "/api/register", {"email": "person@example.com", "password": "correct horse battery"})
        self.assertEqual(status, 502)
        self.assertIn("did not return an account", payload["error"])

    def test_json_must_be_an_object(self):
        self.conn.request("POST", "/api/register", "[]", {"Content-Type": "application/json"})
        response = self.conn.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 400)
        self.assertIn("JSON object", payload["error"])

    def test_transaction_rejects_non_finite_amount_and_invalid_calendar_date(self):
        cookie = self.create_account()
        base = {"title": "Paycheck", "kind": "income", "cadence": "once", "date": "2026-10-16"}
        status, payload, _ = self.request("POST", "/api/transactions", {**base, "amount": "NaN"}, {"Cookie": cookie})
        self.assertEqual(status, 400, payload)
        status, payload, _ = self.request("POST", "/api/transactions", {**base, "amount": 100, "date": "2026-02-30"}, {"Cookie": cookie})
        self.assertEqual(status, 400, payload)

    def test_transaction_can_be_saved_without_category(self):
        cookie = self.create_account()
        payload = {"title": "Paycheck", "amount": 3000, "kind": "income", "cadence": "once", "date": "2026-10-18"}
        with patch.object(server, "DATA_BACKEND", "sqlite"):
            status, response, _ = self.request("POST", "/api/transactions", payload, {"Cookie": cookie})
            self.assertEqual(status, 201, response)
            with server.connect() as db:
                saved = db.execute("SELECT category FROM transactions WHERE id=?", (response["id"],)).fetchone()
        self.assertEqual(saved["category"], "Income")

    def test_transaction_saves_custom_frequency(self):
        cookie = self.create_account()
        payload = {"title": "Quarterly insurance", "amount": 240, "kind": "expense", "cadence": "custom", "custom_frequency": "Every 3 months", "date": "2026-10-18"}
        with patch.object(server, "DATA_BACKEND", "sqlite"):
            status, response, _ = self.request("POST", "/api/transactions", payload, {"Cookie": cookie})
            self.assertEqual(status, 201, response)
            status, overview, _ = self.request("GET", "/api/overview", headers={"Cookie": cookie})
        self.assertEqual(status, 200, overview)
        self.assertEqual(overview["transactions"][0]["cadence"], "custom")
        self.assertEqual(overview["transactions"][0]["custom_frequency"], "Every 3 months")

    def test_transaction_can_be_edited(self):
        cookie = self.create_account()
        original = {"title": "Paycheck", "amount": 3000, "kind": "income", "cadence": "once", "date": "2026-10-18"}
        updated = {"title": "New paycheck", "amount": 3250, "kind": "income", "cadence": "custom", "custom_frequency": "Every 1 week", "date": "2026-10-20"}
        with patch.object(server, "DATA_BACKEND", "sqlite"):
            status, created, _ = self.request("POST", "/api/transactions", original, {"Cookie": cookie})
            self.assertEqual(status, 201, created)
            status, response, _ = self.request("PUT", f"/api/transactions/{created['id']}", updated, {"Cookie": cookie})
            self.assertEqual(status, 200, response)
            status, overview, _ = self.request("GET", "/api/overview", headers={"Cookie": cookie})
        transaction = overview["transactions"][0]
        self.assertEqual((transaction["title"], transaction["amount"], transaction["date"]), ("New paycheck", 3250, "2026-10-20"))
        self.assertEqual((transaction["cadence"], transaction["custom_frequency"]), ("custom", "Every 1 week"))

    def test_transaction_edit_rejects_missing_or_invalid_records(self):
        cookie = self.create_account()
        payload = {"title": "Rent", "amount": 1200, "kind": "expense", "cadence": "once", "date": "2026-10-01"}
        with patch.object(server, "DATA_BACKEND", "sqlite"):
            status, response, _ = self.request("PUT", "/api/transactions/999", payload, {"Cookie": cookie})
            self.assertEqual(status, 404, response)
            status, response, _ = self.request("PUT", "/api/transactions/999", {**payload, "amount": "NaN"}, {"Cookie": cookie})
        self.assertEqual(status, 400, response)

    def test_custom_frequency_requires_a_description(self):
        cookie = self.create_account()
        payload = {"title": "Quarterly insurance", "amount": 240, "kind": "expense", "cadence": "custom", "date": "2026-10-18"}
        status, response, _ = self.request("POST", "/api/transactions", payload, {"Cookie": cookie})
        self.assertEqual(status, 400)
        self.assertIn("frequency", response["error"])

    def test_investment_stores_asset_type_purchase_basis_and_refreshes_quote(self):
        cookie = self.create_account()
        with patch.object(server, "DATA_BACKEND", "sqlite"):
            status, payload, _ = self.request(
                "POST", "/api/investments",
                {"asset_type": "etf", "symbol": "VTI", "name": "Total Market ETF", "shares": 2.5, "purchase_price": 220.25, "purchase_date": "2026-10-18"},
                {"Cookie": cookie},
            )
            self.assertEqual(status, 201, payload)
            status, overview, _ = self.request("GET", "/api/overview", headers={"Cookie": cookie})
            self.assertEqual(status, 200, overview)
            self.assertEqual(overview["investments"][0]["asset_type"], "etf")
            self.assertEqual(overview["investments"][0]["price"], 220.25)
            self.assertEqual(overview["investments"][0]["current_price"], None)

            with patch.object(server, "market_quote", return_value={"symbol": "VTI", "price": 231.75, "as_of": "2026-10-17"}) as provider:
                status, quote, _ = self.request("POST", "/api/investment-quote", {"symbol": "VTI"}, {"Cookie": cookie})
                status, cached_quote, _ = self.request("POST", "/api/investment-quote", {"symbol": "VTI"}, {"Cookie": cookie})
            self.assertEqual(status, 200, quote)
            self.assertEqual(quote["price"], 231.75)
            self.assertEqual(quote["as_of"], "2026-10-17")
            self.assertTrue(cached_quote["cached"])
            provider.assert_called_once()
            status, overview, _ = self.request("GET", "/api/overview", headers={"Cookie": cookie})
            self.assertEqual(status, 200, overview)
            self.assertEqual(overview["investments"][0]["current_price"], 231.75)

    def test_investment_event_opening_value_uses_purchase_date(self):
        cookie = self.create_account()
        with patch.object(server, "DATA_BACKEND", "sqlite"):
            status, payload, _ = self.request("POST", "/api/investments", {"asset_type": "stock", "symbol": "AAPL", "name": "Apple", "shares": 3, "purchase_price": 200, "purchase_date": "2026-10-16"}, {"Cookie": cookie})
            self.assertEqual(status, 201, payload)
            with patch.object(server, "market_daily_series", return_value={"2026-10-16": 205.5}) as provider:
                status, opened, _ = self.request("POST", "/api/investment-open", {"symbol": "AAPL"}, {"Cookie": cookie})
            self.assertEqual(status, 200, opened)
            self.assertEqual(opened["updated"], 1)
            provider.assert_called_once_with("AAPL")
            status, overview, _ = self.request("GET", "/api/overview", headers={"Cookie": cookie})
        self.assertEqual(status, 200, overview)
        self.assertEqual(overview["investments"][0]["event_open_price"], 205.5)
        self.assertIsNotNone(overview["investments"][0]["event_open_checked_at"])

    def test_quote_refresh_rejects_bond_holdings(self):
        cookie = self.create_account()
        status, _, _ = self.request(
            "POST", "/api/investments",
            {"asset_type": "bond", "symbol": "91282CJL6", "name": "Treasury note", "shares": 1, "purchase_price": 1000},
            {"Cookie": cookie},
        )
        self.assertEqual(status, 201)
        with patch.object(server, "market_quote") as provider:
            status, payload, _ = self.request("POST", "/api/investment-quote", {"symbol": "91282CJL6"}, {"Cookie": cookie})
        self.assertEqual(status, 404)
        self.assertIn("No stock or ETF holding", payload["error"])
        provider.assert_not_called()

    def test_quote_refresh_reports_missing_provider_key(self):
        cookie = self.create_account()
        self.request(
            "POST", "/api/investments",
            {"asset_type": "stock", "symbol": "AAPL", "name": "Apple", "shares": 1, "purchase_price": 200},
            {"Cookie": cookie},
        )
        with patch.object(server, "ALPHAVANTAGE_API_KEY", ""):
            status, payload, _ = self.request("POST", "/api/investment-quote", {"symbol": "AAPL"}, {"Cookie": cookie})
        self.assertEqual(status, 503)
        self.assertIn("Set ALPHAVANTAGE_API_KEY", payload["error"])

    def test_supabase_backend_authenticates_and_persists_finance_data(self):
        def fake_request(path, method="GET", payload=None, access_token=None, prefer=None):
            self.assertEqual(access_token, "test-access")
            if path == "/auth/v1/user":
                return {"id": "auth-user-1", "email": "person@example.com"}
            if path == "/auth/v1/logout":
                return {}
            if path.startswith("/rest/v1/transactions") and method == "GET":
                return [{"id": 33, "title": "Rent", "category": "Housing", "amount": 1200, "kind": "expense", "cadence": "monthly", "date": "2026-10-01"}]
            if path.startswith("/rest/v1/investments") and method == "GET":
                return [{"id": 44, "symbol": "TEST", "name": "Sample Fund", "shares": 2, "price": 75, "updated_at": "2026-10-18T12:00:00Z"}]
            if path.startswith("/rest/v1/settings") and method == "GET":
                return [{"monthly_budget": 2500}]
            if path == "/rest/v1/transactions" and method == "POST":
                self.assertEqual(payload["category"], "Income")
                return [{"id": 33}]
            if path == "/rest/v1/investments" and method == "POST":
                return [{"id": 44}]
            if path == "/rest/v1/settings?on_conflict=user_id" and method == "POST":
                return {}
            if path == "/rest/v1/transactions?id=eq.33&select=id" and method == "DELETE":
                return [{"id": 33}]
            self.fail(f"Unexpected Supabase request: {method} {path}")

        with patch.object(server, "DATA_BACKEND", "supabase"), patch.object(
            server,
            "supabase_auth",
            return_value={"user": {"id": "auth-user-1", "email": "person@example.com"}, "access_token": "test-access", "refresh_token": "test-refresh"},
        ), patch.object(server, "supabase_request", side_effect=fake_request):
            status, payload, raw_cookie = self.request("POST", "/api/register", {"email": "person@example.com", "password": "correct horse battery"})
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["user"]["id"], "auth-user-1")
            cookie = raw_cookie.split(";", 1)[0]

            status, overview, _ = self.request("GET", "/api/overview", headers={"Cookie": cookie})
            self.assertEqual(status, 200, overview)
            self.assertEqual(overview["transactions"][0]["title"], "Rent")
            self.assertEqual(overview["investments"][0]["symbol"], "TEST")
            self.assertEqual(overview["monthly_budget"], 2500)

            transaction = {"title": "Paycheck", "category": "Salary", "kind": "income", "cadence": "once", "date": "2026-10-18", "amount": 3000}
            status, payload, _ = self.request("POST", "/api/transactions", transaction, {"Cookie": cookie})
            self.assertEqual(status, 201, payload)
            self.assertEqual(payload["id"], 33)

            investment = {"name": "Sample Fund", "symbol": "TEST", "shares": 2, "price": 75}
            status, payload, _ = self.request("POST", "/api/investments", investment, {"Cookie": cookie})
            self.assertEqual(status, 201, payload)
            self.assertEqual(payload["id"], 44)

            status, payload, _ = self.request("POST", "/api/budget", {"amount": 2500}, {"Cookie": cookie})
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["monthly_budget"], 2500)

            status, payload, _ = self.request("POST", "/api/delete", {"kind": "transaction", "id": 33}, {"Cookie": cookie})
            self.assertEqual(status, 200, payload)

            status, payload, cleared_cookie = self.request("POST", "/api/logout", {}, {"Cookie": cookie})
            self.assertEqual(status, 200, payload)
            self.assertIn("Max-Age=0", cleared_cookie)


if __name__ == "__main__":
    unittest.main()
