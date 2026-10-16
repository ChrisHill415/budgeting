import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("BUDGET_DB", ROOT / "budgeting.db"))
PORT = int(os.environ.get("PORT", "8000"))
COOKIE = "ledgerly_session"


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            salt BLOB NOT NULL, password_hash BLOB NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL, category TEXT NOT NULL, amount REAL NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('income','expense','saving','investment')),
            cadence TEXT NOT NULL DEFAULT 'once', date TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL, name TEXT NOT NULL, shares REAL NOT NULL, price REAL NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            monthly_budget REAL NOT NULL DEFAULT 4000
        );
        CREATE INDEX IF NOT EXISTS tx_user_date ON transactions(user_id, date DESC);
        CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
        """)


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
        if extra_headers:
            for key, value in extra_headers:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 20000:
                raise APIError(413, "Request is too large.")
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise APIError(400, "Please send valid JSON.")

    def token(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def user(self):
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
        token = secrets.token_urlsafe(32)
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + 60 * 60 * 24 * 14))
        return f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=1209600" + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else "")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/me":
                user = self.user()
                return self.send_json(200, {"user": user})
            if path == "/api/overview":
                user = self.require_user()
                with connect() as db:
                    tx = [dict(r) for r in db.execute("SELECT id,title,category,amount,kind,cadence,date FROM transactions WHERE user_id=? ORDER BY date DESC,id DESC", (user["id"],))]
                    inv = [dict(r) for r in db.execute("SELECT id,symbol,name,shares,price,updated_at FROM investments WHERE user_id=? ORDER BY id DESC", (user["id"],))]
                    settings = db.execute("SELECT monthly_budget FROM settings WHERE user_id=?", (user["id"],)).fetchone()
                return self.send_json(200, {"transactions": tx, "investments": inv, "monthly_budget": settings["monthly_budget"] if settings else 4000})
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
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
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
                if path.endswith("register") and len(password) < 10:
                    raise APIError(400, "Use a password with at least 10 characters.")
                with connect() as db:
                    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                    if path.endswith("register"):
                        if user:
                            raise APIError(409, "An account with that email already exists.")
                        salt = secrets.token_bytes(16)
                        password_hash = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
                        cur = db.execute("INSERT INTO users(email,salt,password_hash,created_at) VALUES(?,?,?,?)", (email, salt, password_hash, int(time.time())))
                        uid = cur.lastrowid
                    else:
                        if not user or not hmac.compare_digest(hashlib.scrypt(password.encode(), salt=user["salt"], n=2**14, r=8, p=1), user["password_hash"]):
                            raise APIError(401, "Email or password is incorrect.")
                        uid = user["id"]
                return self.send_json(200, {"user": {"id": uid, "email": email}}, [("Set-Cookie", self.set_session(uid))])
            user = self.require_user()
            if path == "/api/logout":
                token = self.token()
                if token:
                    with connect() as db:
                        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
                return self.send_json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")])
            if path == "/api/transactions":
                title = str(data.get("title", "")).strip()[:80]
                category = str(data.get("category", "Other")).strip()[:40]
                kind, cadence = data.get("kind"), data.get("cadence", "once")
                try:
                    amount = round(float(data.get("amount")), 2)
                except (ValueError, TypeError):
                    raise APIError(400, "Enter a valid amount.")
                date = str(data.get("date", ""))[:10]
                if not title or amount <= 0 or amount > 1_000_000_000 or kind not in ("income", "expense", "saving", "investment") or cadence not in ("once", "weekly", "monthly", "yearly"):
                    raise APIError(400, "Check the name, amount, type, and frequency.")
                if len(date) != 10 or date[4] != "-" or date[7] != "-":
                    raise APIError(400, "Choose a valid date.")
                with connect() as db:
                    cur = db.execute("INSERT INTO transactions(user_id,title,category,amount,kind,cadence,date,created_at) VALUES(?,?,?,?,?,?,?,?)", (user["id"], title, category or "Other", amount, kind, cadence, date, int(time.time())))
                return self.send_json(201, {"id": cur.lastrowid})
            if path == "/api/investments":
                symbol = str(data.get("symbol", "")).strip().upper()[:10]
                name = str(data.get("name", "")).strip()[:60]
                try:
                    shares, price = float(data.get("shares")), round(float(data.get("price")), 2)
                except (ValueError, TypeError):
                    raise APIError(400, "Enter valid share and price values.")
                if not symbol or not name or shares <= 0 or price < 0 or shares > 1e9 or price > 1e9:
                    raise APIError(400, "Check the investment details.")
                with connect() as db:
                    cur = db.execute("INSERT INTO investments(user_id,symbol,name,shares,price,updated_at) VALUES(?,?,?,?,?,?)", (user["id"], symbol, name, shares, price, int(time.time())))
                return self.send_json(201, {"id": cur.lastrowid})
            if path == "/api/budget":
                try:
                    amount = round(float(data.get("amount")), 2)
                except (ValueError, TypeError):
                    raise APIError(400, "Enter a valid monthly budget.")
                if amount <= 0 or amount > 1_000_000_000:
                    raise APIError(400, "Your monthly budget must be greater than zero.")
                with connect() as db:
                    db.execute("INSERT INTO settings(user_id,monthly_budget) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET monthly_budget=excluded.monthly_budget", (user["id"], amount))
                return self.send_json(200, {"monthly_budget": amount})
            if path.startswith("/api/delete/"):
                kind, item_id = path.removeprefix("/api/delete/").split("/", 1)
                table = "transactions" if kind == "transaction" else "investments" if kind == "investment" else None
                if not table or not item_id.isdigit():
                    raise APIError(404, "That item was not found.")
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


if __name__ == "__main__":
    init_db()
    print(f"Ledgerly is running at http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
