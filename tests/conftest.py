import html
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from pulsecheck import auth, create_app, db
from pulsecheck.models import Monitor, User, Workspace

# Tests use a fresh SQLite file per test. To check everything on PostgreSQL
# too, set TEST_DATABASE_URL to an EMPTY Supabase database (its tables are
# dropped and made again for every test, so never point it at the live one).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

CSRF = "test-csrf-token"
PASSWORD = "correct horse"


@pytest.fixture(autouse=True)
def fast_passwords(monkeypatch):
    """The real password hash is slow on purpose (to slow down guessing). Tests make
    many users, so they use a quick setting of the same kind of hash."""
    from functools import partial

    from werkzeug.security import generate_password_hash
    monkeypatch.setattr(auth, "generate_password_hash", partial(generate_password_hash, method="pbkdf2:sha256:1000"))


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE": str(tmp_path / "test.sqlite"),
        "DATABASE_URL": TEST_DATABASE_URL,
        "SERVER_NAME": "localhost",
        "BASE_URL": "http://localhost",
        # The tests' own little web server (the `site` fixture) is on 127.0.0.1.
        "ALLOW_PRIVATE_TARGETS": True,
    })
    with app.app_context():
        if TEST_DATABASE_URL:
            db.drop_tables()
        db.create_tables()
    return app


@pytest.fixture
def ctx(app):
    """An app context, for tests that use db.session() directly."""
    with app.app_context():
        yield db.session()


@pytest.fixture
def client(app):
    client = app.test_client()
    # Every POST needs the session's CSRF token. Put a known one in the session.
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    return client


def post(client, url, data=None, **kwargs):
    """POST a form with the right CSRF token, like a real page would."""
    # Logging in or out starts a new session (and a new token), so set it every time.
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    return client.post(url, data={"csrf_token": CSRF, **(data or {})}, **kwargs)


def make_owner(app, name="Maria Santos", email="maria@example.com", **workspace_fields):
    """The owner and their workspace, straight into the database. Returns (user id, workspace id)."""
    with app.app_context():
        s = db.session()
        workspace = Workspace(name="Maria's Monitors", **workspace_fields)
        s.add(workspace)
        user = auth.create_user(s, workspace, name, email, PASSWORD)
        s.commit()
        return user.id, workspace.id


def make_monitor(app, workspace_id, url="https://api.example.com/health", name="Payments API", **fields):
    with app.app_context():
        s = db.session()
        monitor = Monitor(workspace_id=workspace_id, name=name, url=url, **fields)
        s.add(monitor)
        s.commit()
        return monitor.id


def log_in_as(client, user_id):
    """Skip the login form: put the user straight into the session."""
    with client.application.app_context():
        version = db.session().get(User, user_id).session_version
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = version
        session["csrf"] = CSRF
    return client


@pytest.fixture
def owner(app, client):
    """A logged-in owner. Returns (client, workspace id)."""
    user_id, workspace_id = make_owner(app)
    log_in_as(client, user_id)
    return client, workspace_id


def browser(app, user_id=None):
    """A fresh client, logged in as this user if given."""
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    return log_in_as(client, user_id) if user_id else client


def is_logged_in(client):
    client.get("/settings")
    with client.session_transaction() as session:
        return "user_id" in session


def get_user(app, email):
    with app.app_context():
        return db.session().scalar(select(User).where(User.email == email))


def text(response):
    """A page's words the way a person reads them (&#39; back to ', &amp; to &)."""
    return html.unescape(response.get_data(as_text=True))


def words(response):
    """A page as plain words: no tags, single spaces (for sentences split by <strong> and such)."""
    return " ".join(re.sub(r"<[^>]+>", " ", text(response)).split())


# ---------- A little web server for the checks to call ----------

class Site(BaseHTTPRequestHandler):
    """Answers like a real API would, in all the ways one can go wrong."""

    def log_message(self, *args):  # keep the test output clean
        pass

    def answer(self, status, body=b"", headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (the name http.server asks for)
        self.server.seen.append((self.command, self.path, self.headers.get("User-Agent")))
        path, _, query = self.path.partition("?")
        if path == "/ok":
            self.answer(200, b"all good")
        elif path == "/json":
            self.answer(200, json.dumps({"status": "ok", "db": {"up": True}}).encode(),
                        [("Content-Type", "application/json")])
        elif path == "/down":
            self.answer(503, b"busy")
        elif path == "/missing":
            self.answer(404, b"no such page")
        elif path == "/slow":
            time.sleep(float(query or "1"))
            self.answer(200, b"finally")
        elif path == "/hop":
            self.answer(302, headers=[("Location", "/ok")])
        elif path == "/loop":
            self.answer(302, headers=[("Location", "/loop")])
        elif path == "/inward":
            self.answer(302, headers=[("Location", "http://10.1.2.3/secret")])
        elif path == "/big":
            self.answer(200, b"x" * 200_000)
        else:
            self.answer(404)

    do_HEAD = do_GET
    do_POST = do_GET


class SiteAddress(str):
    """The server's base address, which also knows what the server was asked (.seen)."""
    seen: list


@pytest.fixture(scope="session")
def site():
    """The base address of a local web server (http://127.0.0.1:<port>), for the whole test run.
    site.seen lists (method, path, User-Agent) for every request it got."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    server.daemon_threads = True
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = SiteAddress(f"http://127.0.0.1:{server.server_address[1]}")
    address.seen = server.seen
    yield address
    server.shutdown()
