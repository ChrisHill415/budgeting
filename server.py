import base64
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import date as calendar_date
from urllib.parse import quote, urlencode, urlparse

ROOT = Path(__file__).parent


def load_env_file(path):
    """Load simple KEY=VALUE entries without overriding the shell environment."""
    if not path.exists():
        return
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not key.replace("_", "").isalnum():
            raise RuntimeError(f"Invalid entry in {path.name} at line {line_number}.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(ROOT / ".env")
DB_PATH = Path(os.environ.get("BUDGET_DB", ROOT / "budgeting.db"))
PORT = int(os.environ.get("PORT", "8000"))
COOKIE = "ledgerly_session"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
DATA_BACKEND = os.environ.get("LEDGERLY_DATA_BACKEND", "sqlite")
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "")
ALPHAVANTAGE_ENTITLEMENT = os.environ.get("ALPHAVANTAGE_ENTITLEMENT", "")
ADSENSE_CLIENT_ID = os.environ.get("ADSENSE_CLIENT_ID", "")
ADSENSE_SLOT_ID = os.environ.get("ADSENSE_SLOT_ID", "")


def supabase_request(path, method="GET", payload=None, access_token=None, prefer=None):
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise APIError(503, "Account service is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY on the server.")
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {access_token or SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    request = urllib.request.Request(SUPABASE_URL + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise APIError(401, "Your session has expired. Please sign in again.")
        try:
            error_data = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            error_data = {}
        error_code = error_data.get("code")
        if error_code == "PGRST205":
            raise APIError(503, "Supabase is missing the finance tables. Apply the latest supabase/schema.sql in the Supabase SQL Editor, then retry.")
        if error_code == "PGRST204":
            raise APIError(503, "Supabase is missing finance table columns. Apply the latest supabase/schema.sql in the Supabase SQL Editor, then retry.")
        if exc.code == 403:
            detail = str(error_data.get("message") or error_data.get("msg") or "").strip()
            detail = re.sub(r"\s+", " ", detail)[:240]
            code = str(error_code or "")
            if code == "42501" or "row-level security" in detail.lower() or "permission denied" in detail.lower():
                guidance = "Check that the latest supabase/schema.sql was applied and that the signed-in user's access token is being used."
            else:
                guidance = "Check that SUPABASE_URL and SUPABASE_ANON_KEY belong to the same Supabase project and that the key is valid."
            reason = f" Supabase says: {detail}" if detail else ""
            code_label = f" (code {code})" if code else ""
            raise APIError(502, f"Supabase denied the data request{code_label}.{reason} {guidance}")
        if exc.code == 404:
            raise APIError(502, "Supabase returned HTTP 404. Check SUPABASE_URL and confirm the finance tables are available in the public schema.")
        raise APIError(502, f"Supabase data request failed (HTTP {exc.code}).")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise APIError(502, f"Could not reach Supabase: {exc.reason if isinstance(exc, urllib.error.URLError) else 'request timed out'}.")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise APIError(502, "Supabase returned an unreadable data response.")


def supabase_data(table, method="GET", query="", payload=None, access_token=None, prefer=None):
    path = f"/rest/v1/{table}" + (f"?{query}" if query else "")
    return supabase_request(path, method, payload, access_token, prefer)


def market_quote(symbol):
    if not ALPHAVANTAGE_API_KEY:
        raise APIError(503, "Live quote lookup is not configured. Set ALPHAVANTAGE_API_KEY on the server.")
    params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": ALPHAVANTAGE_API_KEY}
    if ALPHAVANTAGE_ENTITLEMENT in ("realtime", "delayed"):
        params["entitlement"] = ALPHAVANTAGE_ENTITLEMENT
    query = urlencode(params)
    request = urllib.request.Request(
        f"https://www.alphavantage.co/query?{query}",
        headers={"User-Agent": "Ledgerly/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise APIError(502, f"Market quote provider returned HTTP {exc.code}.")
    except urllib.error.URLError as exc:
        raise APIError(502, f"Could not reach the market quote provider: {exc.reason}.")
    except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        raise APIError(502, "The market quote provider returned an unreadable or timed-out response.")
    if not isinstance(data, dict):
        raise APIError(502, "The market quote provider returned an unexpected response.")
    quote_data = data.get("Global Quote") or {}
    if quote_data.get("05. price"):
        try:
            price = round(float(quote_data["05. price"]), 4)
        except (ValueError, TypeError, OverflowError):
            raise APIError(502, "The market quote provider returned an invalid price.")
        if not math.isfinite(price) or price < 0:
            raise APIError(502, "The market quote provider returned an invalid price.")
        return {"symbol": symbol, "price": price, "as_of": quote_data.get("07. latest trading day") or None}
    if data.get("Note") or data.get("Information"):
        raise APIError(429, "The quote provider is limiting requests. Wait before refreshing prices again.")
    if data.get("Error Message"):
        raise APIError(404, f"No market quote was found for {symbol}.")
    raise APIError(502, f"The quote provider returned no price for {symbol}.")


def market_daily_series(symbol):
    if not ALPHAVANTAGE_API_KEY:
        raise APIError(503, "Historical market lookup is not configured. Set ALPHAVANTAGE_API_KEY on the server.")
    params = {"function": "TIME_SERIES_DAILY", "symbol": symbol, "outputsize": "compact", "apikey": ALPHAVANTAGE_API_KEY}
    request = urllib.request.Request(
        f"https://www.alphavantage.co/query?{urlencode(params)}",
        headers={"User-Agent": "Ledgerly/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise APIError(502, f"Historical price provider returned HTTP {exc.code}.")
    except urllib.error.URLError as exc:
        raise APIError(502, f"Could not reach the historical price provider: {exc.reason}.")
    except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        raise APIError(502, "The historical price provider returned an unreadable or timed-out response.")
    if not isinstance(data, dict):
        raise APIError(502, "The historical price provider returned an unexpected response.")
    series = data.get("Time Series (Daily)")
    if series is None:
        if data.get("Note") or data.get("Information"):
            raise APIError(429, "The quote provider is limiting requests. Wait before refreshing prices again.")
        if data.get("Error Message"):
            raise APIError(404, f"No historical prices were found for {symbol}.")
        raise APIError(502, f"The quote provider returned no daily prices for {symbol}.")
    prices = {}
    for event_date, point in series.items():
        try:
            price = round(float(point["1. open"]), 4)
        except (KeyError, ValueError, TypeError, OverflowError):
            raise APIError(502, "The historical price provider returned an invalid opening price.")
        if not math.isfinite(price) or price < 0:
            raise APIError(502, "The historical price provider returned an invalid opening price.")
        prices[event_date] = price
    return prices


def quote_cache_fresh(updated_at):
    try:
        timestamp = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    max_age = 86400
    return 0 <= (datetime.now(timezone.utc) - timestamp).total_seconds() < max_age


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    if DATA_BACKEND == "supabase":
        return
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            salt BLOB NOT NULL, password_hash BLOB NOT NULL, created_at INTEGER NOT NULL,
            supabase_user_id TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL, category TEXT NOT NULL, amount REAL NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('income','expense','saving','investment')),
            cadence TEXT NOT NULL DEFAULT 'once', custom_frequency TEXT, date TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL, name TEXT NOT NULL, shares REAL NOT NULL, price REAL NOT NULL,
            updated_at INTEGER NOT NULL, asset_type TEXT NOT NULL DEFAULT 'stock', purchase_date TEXT,
            current_price REAL, quote_updated_at TEXT, quote_as_of TEXT,
            event_open_price REAL, event_open_checked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            monthly_budget REAL NOT NULL DEFAULT 4000
        );
        CREATE INDEX IF NOT EXISTS tx_user_date ON transactions(user_id, date DESC);
        CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "supabase_user_id" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN supabase_user_id TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_supabase_id ON users(supabase_user_id)")
        transaction_columns = {row[1] for row in db.execute("PRAGMA table_info(transactions)")}
        if "custom_frequency" not in transaction_columns:
            db.execute("ALTER TABLE transactions ADD COLUMN custom_frequency TEXT")
        investment_columns = {row[1] for row in db.execute("PRAGMA table_info(investments)")}
        for name, declaration in (
            ("asset_type", "TEXT NOT NULL DEFAULT 'stock'"),
            ("purchase_date", "TEXT"),
            ("current_price", "REAL"),
            ("quote_updated_at", "TEXT"),
            ("quote_as_of", "TEXT"),
            ("event_open_price", "REAL"),
            ("event_open_checked_at", "TEXT"),
        ):
            if name not in investment_columns:
                db.execute(f"ALTER TABLE investments ADD COLUMN {name} {declaration}")


def supabase_auth(action, email, password):
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise APIError(503, "Account service is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY on the server.")
    endpoint = "/auth/v1/signup" if action == "register" else "/auth/v1/token?grant_type=password"
    payload = json.dumps({"email": email, "password": password}).encode()
    request = urllib.request.Request(SUPABASE_URL + endpoint, data=payload, headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            error_data = json.loads(body)
            detail = error_data.get("msg") or error_data.get("message") or error_data.get("error_description") or error_data.get("error", "")
        except (ValueError, TypeError):
            detail = ""
        detail = str(detail)
        if action == "register" and (exc.code == 400 or "already registered" in detail.lower() or "already exists" in detail.lower()):
            raise APIError(409, "An account with that email already exists, or Supabase rejected the password.")
        if exc.code in (400, 401) and action == "login":
            raise APIError(401, "Email or password is incorrect.")
        raise APIError(502, f"Supabase account request failed: {detail or 'Please try again.'}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise APIError(502, f"Could not reach Supabase: {exc.reason if isinstance(exc, urllib.error.URLError) else 'request timed out'}.")


def local_user_for_supabase(auth_user, email):
    sid = str(auth_user["id"])
    with connect() as db:
        row = db.execute("SELECT id,email FROM users WHERE supabase_user_id=?", (sid,)).fetchone()
        if row:
            return dict(row)
        row = db.execute("SELECT id,email FROM users WHERE email=?", (email,)).fetchone()
        if row:
            db.execute("UPDATE users SET supabase_user_id=? WHERE id=?", (sid, row["id"]))
            return dict(row)
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(secrets.token_bytes(32), salt=salt, n=2**14, r=8, p=1)
        cur = db.execute("INSERT INTO users(email,salt,password_hash,created_at,supabase_user_id) VALUES(?,?,?,?,?)", (email, salt, digest, int(time.time()), sid))
        return {"id": cur.lastrowid, "email": email}


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Handler(BaseHTTPRequestHandler):
    server_version = "Ledgerly/1.0"

    def log_message(self, fmt, *args):
        # Request errors are returned to the caller; routine requests stay quiet.
        pass

    def send_json(self, status, data, extra_headers=None):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        headers = list(extra_headers or [])
        if getattr(self, "session_cookie_update", None):
            headers.append(("Set-Cookie", self.session_cookie_update))
            self.session_cookie_update = None
        if headers:
            for key, value in headers:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise APIError(400, "Request length is invalid.")
            if length > 20000:
                raise APIError(413, "Request is too large.")
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise APIError(400, "Please send valid JSON.")
        if not isinstance(data, dict):
            raise APIError(400, "Please send a JSON object.")
        return data

    def token(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def remote_tokens(self):
        if getattr(self, "active_remote_tokens", None):
            return self.active_remote_tokens
        value = self.token()
        if not value:
            return None
        try:
            padded = value + "=" * (-len(value) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            if data.get("access") and data.get("refresh"):
                return data
        except (AttributeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return None

    def session_cookie(self, access_token, refresh_token):
        value = base64.urlsafe_b64encode(json.dumps({"access": access_token, "refresh": refresh_token}).encode()).decode().rstrip("=")
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"{COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=1209600{secure}"

    def user(self):
        if DATA_BACKEND == "supabase":
            tokens = self.remote_tokens()
            if not tokens:
                return None
            self.active_remote_tokens = tokens
            try:
                auth_user = supabase_request("/auth/v1/user", access_token=tokens["access"])
            except APIError as exc:
                if exc.status != 401:
                    raise
                refreshed = supabase_request("/auth/v1/token?grant_type=refresh_token", "POST", {"refresh_token": tokens["refresh"]})
                access, refresh = refreshed.get("access_token"), refreshed.get("refresh_token")
                auth_user = refreshed.get("user") or {}
                if not access or not refresh or not auth_user.get("id"):
                    return None
                self.active_remote_tokens = {"access": access, "refresh": refresh}
                self.session_cookie_update = self.session_cookie(access, refresh)
            return {"id": auth_user["id"], "email": str(auth_user.get("email", "")).lower()}
        token = self.token()
        if not token:
            return None
        with connect() as db:
            row = db.execute("SELECT u.id,u.email FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
        return dict(row) if row else None

    def require_user(self):
        user = self.user()
        if not user:
            raise APIError(401, "Please sign in to continue.")
        return user

    def set_session(self, user_id):
        if DATA_BACKEND == "supabase":
            raise RuntimeError("Supabase session tokens are required.")
        token = secrets.token_urlsafe(32)
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + 60 * 60 * 24 * 14))
        return f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=1209600" + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else "")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/public-config":
                return self.send_json(200, {
                    "adsense_client_id": ADSENSE_CLIENT_ID,
                    "adsense_slot_id": ADSENSE_SLOT_ID,
                })
            if path == "/api/me":
                user = self.user()
                return self.send_json(200, {"user": user})
            if path == "/api/overview":
                user = self.require_user()
                if DATA_BACKEND == "supabase":
                    tokens = self.remote_tokens()
                    access = tokens["access"]
                    tx = supabase_data("transactions", query="select=id,title,category,amount,kind,cadence,custom_frequency,date&order=date.desc,id.desc", access_token=access)
                    inv = supabase_data("investments", query="select=id,symbol,name,shares,price,asset_type,purchase_date,current_price,quote_updated_at,quote_as_of,event_open_price,event_open_checked_at,updated_at&order=id.desc", access_token=access)
                    settings = supabase_data("settings", query="select=monthly_budget&limit=1", access_token=access)
                    budget = settings[0]["monthly_budget"] if settings else 4000
                else:
                    with connect() as db:
                        tx = [dict(r) for r in db.execute("SELECT id,title,category,amount,kind,cadence,custom_frequency,date FROM transactions WHERE user_id=? ORDER BY date DESC,id DESC", (user["id"],))]
                        inv = [dict(r) for r in db.execute("SELECT id,symbol,name,shares,price,asset_type,purchase_date,current_price,quote_updated_at,quote_as_of,event_open_price,event_open_checked_at,updated_at FROM investments WHERE user_id=? ORDER BY id DESC", (user["id"],))]
                        settings = db.execute("SELECT monthly_budget FROM settings WHERE user_id=?", (user["id"],)).fetchone()
                    budget = settings["monthly_budget"] if settings else 4000
                return self.send_json(200, {"transactions": tx, "investments": inv, "monthly_budget": budget})
            if path.startswith("/api/"):
                return self.send_json(404, {"error": "That API route was not found."})
            relative = "index.html" if path == "/" else path.lstrip("/")
            target = (ROOT / "public" / relative).resolve()
            if not target.is_relative_to((ROOT / "public").resolve()) or not target.is_file():
                return self.send_json(404, {"error": "Page not found."})
            mime = "text/html; charset=utf-8" if target.suffix == ".html" else "text/css; charset=utf-8" if target.suffix == ".css" else "text/javascript; charset=utf-8" if target.suffix == ".js" else "application/octet-stream"
            raw = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' https://pagead2.googlesyndication.com https://*.googlesyndication.com https://www.googletagservices.com https://www.google.com https://www.googleadservices.com; img-src 'self' data: https://pagead2.googlesyndication.com https://*.google.com https://*.googleusercontent.com https://*.gstatic.com https://*.doubleclick.net https://www.googleadservices.com; connect-src 'self' https://pagead2.googlesyndication.com https://*.googlesyndication.com https://googleads.g.doubleclick.net https://www.google.com https://www.googleadservices.com https://ad.doubleclick.net; frame-src https://googleads.g.doubleclick.net https://*.doubleclick.net https://*.googlesyndication.com https://tpc.googlesyndication.com https://www.google.com; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)
        except APIError as exc:
            self.send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            self.send_json(500, {"error": f"Something went wrong: {exc}"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.body()
            if path in ("/api/register", "/api/login"):
                email = str(data.get("email", "")).strip().lower()
                password = str(data.get("password", ""))
                if len(email) > 254 or "@" not in email or "." not in email.split("@")[-1]:
                    raise APIError(400, "Enter a valid email address.")
                if path.endswith("register") and len(password) < 6:
                    raise APIError(400, "Use a password with at least 6 characters.")
                action = "register" if path.endswith("register") else "login"
                auth_data = supabase_auth(action, email, password)
                auth_user = auth_data.get("user") or {}
                if not auth_user.get("id"):
                    raise APIError(502, "Supabase did not return an account. Please try again.")
                if action == "register" and not auth_data.get("access_token"):
                    return self.send_json(200, {"needs_confirmation": True, "message": "Account created. Check your email to confirm it, then sign in."})
                if DATA_BACKEND == "supabase":
                    access, refresh = auth_data.get("access_token"), auth_data.get("refresh_token")
                    if not access or not refresh:
                        raise APIError(502, "Supabase did not return a complete sign-in session. Please try again.")
                    return self.send_json(200, {"user": {"id": auth_user["id"], "email": str(auth_user.get("email", email)).lower()}}, [("Set-Cookie", self.session_cookie(access, refresh))])
                local_user = local_user_for_supabase(auth_user, auth_user.get("email", email).lower())
                uid = local_user["id"]
                return self.send_json(200, {"user": {"id": uid, "email": local_user["email"]}}, [("Set-Cookie", self.set_session(uid))])
            user = self.require_user()
            if path in ("/api/investment-open", "/api/investments/open"):
                symbol = str(data.get("symbol", "")).strip().upper()
                if not re.fullmatch(r"[A-Z0-9.^=-]{1,15}", symbol):
                    raise APIError(400, "Enter a valid stock or ETF ticker.")
                if DATA_BACKEND == "supabase":
                    tokens = self.remote_tokens()
                    holdings = supabase_data("investments", query=f"select=id,purchase_date,event_open_price,event_open_checked_at&symbol=eq.{quote(symbol, safe='')}&asset_type=in.(stock,etf)", access_token=tokens["access"])
                else:
                    with connect() as db:
                        holdings = [dict(row) for row in db.execute("SELECT id,purchase_date,event_open_price,event_open_checked_at FROM investments WHERE user_id=? AND symbol=? AND asset_type IN ('stock','etf')", (user["id"], symbol))]
                pending = [row for row in holdings if not row.get("event_open_checked_at")]
                if not pending:
                    return self.send_json(200, {"symbol": symbol, "checked": 0, "updated": 0})
                event_dates = sorted({row.get("purchase_date") for row in pending if row.get("purchase_date")})
                prices = {}
                if event_dates:
                    daily = market_daily_series(symbol)
                    for event_date in event_dates:
                        prices[event_date] = daily.get(event_date)
                checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                updated = 0
                for row in pending:
                    event_date = row.get("purchase_date")
                    opening_price = prices.get(event_date) if event_date else None
                    if DATA_BACKEND == "supabase":
                        supabase_data("investments", "PATCH", query=f"id=eq.{row['id']}", payload={"event_open_price": opening_price, "event_open_checked_at": checked_at}, access_token=tokens["access"], prefer="return=minimal")
                    else:
                        with connect() as db:
                            db.execute("UPDATE investments SET event_open_price=?,event_open_checked_at=? WHERE id=? AND user_id=?", (opening_price, checked_at, row["id"], user["id"]))
                    updated += 1
                return self.send_json(200, {"symbol": symbol, "checked": len(pending), "updated": updated})
            if path in ("/api/investment-quote", "/api/investments/quote"):
                symbol = str(data.get("symbol", "")).strip().upper()
                if not re.fullmatch(r"[A-Z0-9.^=-]{1,15}", symbol):
                    raise APIError(400, "Enter a valid stock or ETF ticker.")
                if DATA_BACKEND == "supabase":
                    tokens = self.remote_tokens()
                    holdings = supabase_data("investments", query=f"select=id,current_price,quote_updated_at,quote_as_of&symbol=eq.{quote(symbol, safe='')}&asset_type=in.(stock,etf)", access_token=tokens["access"])
                else:
                    with connect() as db:
                        holdings = [dict(row) for row in db.execute("SELECT id,current_price,quote_updated_at,quote_as_of FROM investments WHERE user_id=? AND symbol=? AND asset_type IN ('stock','etf')", (user["id"], symbol))]
                if not holdings:
                    raise APIError(404, f"No stock or ETF holding was found for {symbol}.")
                cached = next((row for row in holdings if row.get("current_price") is not None and quote_cache_fresh(row.get("quote_updated_at"))), None)
                if cached:
                    return self.send_json(200, {"symbol": symbol, "price": float(cached["current_price"]), "as_of": cached.get("quote_as_of"), "updated_at": cached.get("quote_updated_at"), "cached": True})
                quote_data = market_quote(symbol)
                quote_updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if DATA_BACKEND == "supabase":
                    supabase_data("investments", "PATCH", query=f"symbol=eq.{quote(symbol, safe='')}&asset_type=in.(stock,etf)", payload={"current_price": quote_data["price"], "quote_updated_at": quote_updated_at, "quote_as_of": quote_data["as_of"]}, access_token=tokens["access"], prefer="return=minimal")
                else:
                    with connect() as db:
                        db.execute("UPDATE investments SET current_price=?,quote_updated_at=?,quote_as_of=? WHERE user_id=? AND symbol=? AND asset_type IN ('stock','etf')", (quote_data["price"], quote_updated_at, quote_data["as_of"], user["id"], symbol))
                return self.send_json(200, {**quote_data, "updated_at": quote_updated_at})
            if path == "/api/logout":
                if DATA_BACKEND == "supabase":
                    tokens = self.remote_tokens()
                    if tokens:
                        supabase_request("/auth/v1/logout", "POST", access_token=tokens["access"])
                    return self.send_json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
                token = self.token()
                if token:
                    with connect() as db:
                        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                return self.send_json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
            if path == "/api/transactions":
                title = str(data.get("title", "")).strip()[:80]
                kind, cadence = data.get("kind"), data.get("cadence", "once")
                custom_frequency = str(data.get("custom_frequency", "")).strip()[:50] if cadence == "custom" else None
                try:
                    amount = round(float(data.get("amount")), 2)
                except (ValueError, TypeError, OverflowError):
                    raise APIError(400, "Enter a valid amount.")
                date = str(data.get("date", ""))
                if not title or not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000 or kind not in ("income", "expense", "saving", "investment") or cadence not in ("once", "weekly", "monthly", "yearly", "custom") or (cadence == "custom" and not custom_frequency):
                    raise APIError(400, "Check the name, amount, type, and frequency.")
                try:
                    if calendar_date.fromisoformat(date).isoformat() != date:
                        raise ValueError
                except ValueError:
                    raise APIError(400, "Choose a valid date.")
                category = {"income": "Income", "expense": "Expense", "saving": "Savings", "investment": "Investment"}[kind]
                if DATA_BACKEND == "supabase":
                    tokens = self.remote_tokens()
                    rows = supabase_data("transactions", "POST", payload={"title": title, "category": category or "Other", "amount": amount, "kind": kind, "cadence": cadence, "custom_frequency": custom_frequency, "date": date}, access_token=tokens["access"], prefer="return=representation")
                    return self.send_json(201, {"id": rows[0]["id"]})
                with connect() as db:
                    cur = db.execute("INSERT INTO transactions(user_id,title,category,amount,kind,cadence,custom_frequency,date,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (user["id"], title, category or "Other", amount, kind, cadence, custom_frequency, date, int(time.time())))
                return self.send_json(201, {"id": cur.lastrowid})
            if path == "/api/investments":
                symbol = str(data.get("symbol", "")).strip().upper()[:10]
                name = str(data.get("name", "")).strip()[:60]
                asset_type = str(data.get("asset_type", "stock")).strip().lower()
                try:
                    shares = float(data.get("shares"))
                    price = round(float(data.get("purchase_price", data.get("price"))), 4)
                except (ValueError, TypeError, OverflowError):
                    raise APIError(400, "Enter valid share and price values.")
                purchase_date = str(data.get("purchase_date", calendar_date.today().isoformat()))
                if asset_type not in ("stock", "etf", "bond"):
                    raise APIError(400, "Choose stock, ETF, or bond.")
                if not symbol or not name or not math.isfinite(shares) or not math.isfinite(price) or shares <= 0 or price <= 0 or shares > 1e9 or price > 1e9:
                    raise APIError(400, "Check the investment details.")
                if not re.fullmatch(r"[A-Z0-9.^=-]{1,15}", symbol):
                    raise APIError(400, "Enter a valid stock, ETF, or bond ticker.")
                try:
                    if calendar_date.fromisoformat(purchase_date).isoformat() != purchase_date:
                        raise ValueError
                except ValueError:
                    raise APIError(400, "Choose a valid purchase date.")
                if DATA_BACKEND == "supabase":
                    tokens = self.remote_tokens()
                    rows = supabase_data("investments", "POST", payload={"symbol": symbol, "name": name, "shares": shares, "price": price, "asset_type": asset_type, "purchase_date": purchase_date}, access_token=tokens["access"], prefer="return=representation")
                    return self.send_json(201, {"id": rows[0]["id"]})
                with connect() as db:
                    cur = db.execute("INSERT INTO investments(user_id,symbol,name,shares,price,updated_at,asset_type,purchase_date) VALUES(?,?,?,?,?,?,?,?)", (user["id"], symbol, name, shares, price, int(time.time()), asset_type, purchase_date))
                return self.send_json(201, {"id": cur.lastrowid})
            if path == "/api/budget":
                try:
                    amount = round(float(data.get("amount")), 2)
                except (ValueError, TypeError, OverflowError):
                    raise APIError(400, "Enter a valid monthly budget.")
                if not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000:
                    raise APIError(400, "Your monthly budget must be greater than zero.")
                if DATA_BACKEND == "supabase":
                    tokens = self.remote_tokens()
                    supabase_data("settings", "POST", query="on_conflict=user_id", payload={"monthly_budget": amount}, access_token=tokens["access"], prefer="resolution=merge-duplicates,return=minimal")
                    return self.send_json(200, {"monthly_budget": amount})
                with connect() as db:
                    db.execute("INSERT INTO settings(user_id,monthly_budget) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET monthly_budget=excluded.monthly_budget", (user["id"], amount))
                return self.send_json(200, {"monthly_budget": amount})
            if path == "/api/delete":
                kind, item_id = str(data.get("kind", "")), str(data.get("id", ""))
            elif path.startswith("/api/delete/"):
                kind, item_id = path.removeprefix("/api/delete/").split("/", 1)
            else:
                kind, item_id = None, None
            if kind is not None:
                table = "transactions" if kind == "transaction" else "investments" if kind == "investment" else None
                if not table or not item_id.isdigit():
                    raise APIError(404, "That item was not found.")
                if DATA_BACKEND == "supabase":
                    tokens = self.remote_tokens()
                    rows = supabase_data(table, "DELETE", query=f"id=eq.{int(item_id)}&select=id", access_token=tokens["access"], prefer="return=representation")
                    if not rows:
                        raise APIError(404, "That item was not found.")
                    return self.send_json(200, {"ok": True})
                with connect() as db:
                    cur = db.execute(f"DELETE FROM {table} WHERE id=? AND user_id=?", (int(item_id), user["id"]))
                if cur.rowcount == 0:
                    raise APIError(404, "That item was not found.")
                return self.send_json(200, {"ok": True})
            raise APIError(404, "That API route was not found.")
        except APIError as exc:
            self.send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            self.send_json(500, {"error": f"Something went wrong: {exc}"})

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            match = re.fullmatch(r"/api/transactions/(\d+)", path)
            if not match:
                raise APIError(404, "That API route was not found.")
            user = self.require_user()
            data = self.body()
            transaction_id = int(match.group(1))
            title = str(data.get("title", "")).strip()[:80]
            kind, cadence = data.get("kind"), data.get("cadence", "once")
            custom_frequency = str(data.get("custom_frequency", "")).strip()[:50] if cadence == "custom" else None
            try:
                amount = round(float(data.get("amount")), 2)
            except (ValueError, TypeError, OverflowError):
                raise APIError(400, "Enter a valid amount.")
            date = str(data.get("date", ""))
            if not title or not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000 or kind not in ("income", "expense", "saving") or cadence not in ("once", "weekly", "monthly", "yearly", "custom") or (cadence == "custom" and not custom_frequency):
                raise APIError(400, "Check the name, amount, type, and frequency.")
            try:
                if calendar_date.fromisoformat(date).isoformat() != date:
                    raise ValueError
            except ValueError:
                raise APIError(400, "Choose a valid date.")
            category = {"income": "Income", "expense": "Expense", "saving": "Savings"}[kind]
            values = {"title": title, "category": category, "amount": amount, "kind": kind, "cadence": cadence, "custom_frequency": custom_frequency, "date": date}
            if DATA_BACKEND == "supabase":
                tokens = self.remote_tokens()
                rows = supabase_data("transactions", "PATCH", query=f"id=eq.{transaction_id}&select=id", payload=values, access_token=tokens["access"], prefer="return=representation")
                if not rows:
                    raise APIError(404, "That transaction was not found.")
            else:
                with connect() as db:
                    cur = db.execute("UPDATE transactions SET title=?,category=?,amount=?,kind=?,cadence=?,custom_frequency=?,date=? WHERE id=? AND user_id=?", (title, category, amount, kind, cadence, custom_frequency, date, transaction_id, user["id"]))
                if cur.rowcount == 0:
                    raise APIError(404, "That transaction was not found.")
            self.send_json(200, {"ok": True})
        except APIError as exc:
            self.send_json(exc.status, {"error": exc.message})
        except Exception as exc:
            self.send_json(500, {"error": f"Something went wrong: {exc}"})


if __name__ == "__main__":
    init_db()
    print(f"Ledgerly is running at http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
