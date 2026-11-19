"""Pulse Check, an API uptime monitor. "Is your API still breathing?"

Add the addresses you care about. Pulse Check checks each one on a schedule,
times every part of the answer, works out uptime and response-time
percentiles, opens an incident when a service really fails (not on one
blip), and sends alerts that are tried again until they arrive.

Flask builds the app inside create_app() (the "app factory" pattern).
Tests call it with their own settings, so each test gets a fresh app and database.
"""
import mimetypes
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

# Python doesn't know this one, and browsers want the right type for the app manifest.
mimetypes.add_type("application/manifest+json", ".webmanifest")

APP_NAME = "Pulse Check"
TAGLINE = "Is your API still breathing?"
DEV_SECRET_KEY = "dev-only-not-secret"
LOCAL_PORT = 5014

# What the browser may load and talk to. Scripts, styles and fonts come only
# from this site, so even if someone sneaked a <script> into a monitor's name,
# the browser would refuse to run it.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "worker-src 'self'",  # the service worker, /sw.js
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


def env_flag(name, default):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    on_render = bool(os.environ.get("RENDER"))  # Render sets RENDER=true on its servers
    app.config.from_mapping(
        # Signs the login cookie and every link inside an alert (Acknowledge, ...).
        SECRET_KEY=os.environ.get("SECRET_KEY", DEV_SECRET_KEY),
        # Local SQLite file, in the instance/ folder (git ignores it).
        DATABASE=str(Path(app.instance_path) / "pulsecheck.sqlite"),
        # Set on the live site: then we use PostgreSQL (Supabase) instead of SQLite.
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        ON_RENDER=on_render,
        # The site's own address, for links inside alerts (read far from any web page,
        # so they must be full addresses) and for the self-check.
        BASE_URL=os.environ.get("BASE_URL", f"http://127.0.0.1:{LOCAL_PORT}").rstrip("/"),
        # Pulse Check never calls addresses inside a private network (targets.py): on a
        # server that would let anyone with a login look around the server's own network.
        # Locally you may want to watch your own apps on 127.0.0.1, so you can allow it.
        ALLOW_PRIVATE_TARGETS=env_flag("ALLOW_PRIVATE_TARGETS", False),
        # Cookie safety: not readable by JavaScript (Flask's default), not sent
        # with forms from other sites, and only over HTTPS on the live site.
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=on_render,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=1 * 1000 * 1000,
        # The secret the GitHub Actions timer sends to POST /tasks/tick (tick.py).
        TICK_KEY=os.environ.get("TICK_KEY"),
        # Worker threads inside the web app (worker.py). Locally you can turn them
        # off (RUN_WORKER=0) and run `flask --app pulsecheck worker` in its own window.
        RUN_WORKER=env_flag("RUN_WORKER", True),
        CREW_SIZE=int(os.environ.get("CREW_SIZE", "4")),
        # The canary (canary.py): the crew checks its own address every minute (on Render
        # that also keeps the free plan from sleeping) and one outside address that is
        # always up. CANARY_URL="" turns the outside check off.
        SELF_CHECK=env_flag("SELF_CHECK", True),
        CANARY_URL=os.environ.get("CANARY_URL", "https://www.gstatic.com/generate_204"),
        # Keeps channel secrets (webhook addresses, bot tokens) encrypted (crypto.py).
        ENCRYPTION_KEY=os.environ.get("ENCRYPTION_KEY"),
        # Email alerts go through Brevo's HTTP API (Render's free plan blocks SMTP).
        # BREVO_SENDER is an address Brevo has checked.
        BREVO_API_KEY=os.environ.get("BREVO_API_KEY"),
        BREVO_SENDER=os.environ.get("BREVO_SENDER"),
        # Phone alerts (web push): flask --app pulsecheck make-vapid-keys makes the two keys.
        VAPID_PUBLIC_KEY=os.environ.get("VAPID_PUBLIC_KEY", ""),
        VAPID_PRIVATE_KEY=os.environ.get("VAPID_PRIVATE_KEY", ""),
        VAPID_SUBJECT=os.environ.get("VAPID_SUBJECT", ""),
    )
    if test_config:
        app.config.update(test_config)
    if app.testing:
        # Tests run the crew by hand (worker.simulate), never in threads of their own.
        app.config["RUN_WORKER"] = bool(test_config and test_config.get("RUN_WORKER"))
        # ...and never call the internet: no canary unless a test asks for one.
        app.config["SELF_CHECK"] = bool(test_config and test_config.get("SELF_CHECK"))
        app.config["CANARY_URL"] = (test_config or {}).get("CANARY_URL", "")
    if on_render:
        # Render's proxy talks HTTPS to the visitor and HTTP to us. This makes Flask
        # believe the X-Forwarded-* headers: links start with https://, and the rate
        # limits see the visitor's address instead of the proxy's.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        if app.config["ALLOW_PRIVATE_TARGETS"]:
            raise RuntimeError("ALLOW_PRIVATE_TARGETS is for your own PC only. Remove it on Render.")
    if on_render and app.config["SECRET_KEY"] == DEV_SECRET_KEY:
        raise RuntimeError("Set the SECRET_KEY environment variable on Render.")
    if on_render and not app.config["ENCRYPTION_KEY"]:
        raise RuntimeError("Set the ENCRYPTION_KEY environment variable on Render.")
    if on_render and not app.config["DATABASE_URL"]:
        # Without it we'd use a SQLite file, and Render forgets files on every restart.
        raise RuntimeError("Set the DATABASE_URL environment variable on Render (Supabase's connection string).")

    from . import alerts, auth, canary, db, formatting, history, incidents, pulse_words, rollups, scheduler, seed, tick, worker  # noqa: F401
    from .views import alerts as alert_views
    from .views import crew, dashboard, demo, monitors, numbers, settings
    from .views import incidents as incident_views

    db.init_app(app)
    auth.init_app(app)
    formatting.init_app(app)
    pulse_words.init_app(app)
    monitors.init_app(app)
    settings.init_app(app)
    crew.init_app(app)
    tick.init_app(app)
    worker.init_app(app)
    incidents.init_app(app)
    incident_views.init_app(app)
    canary.init_app(app)
    seed.init_app(app)
    rollups.init_app(app)
    history.init_app(app)
    numbers.init_app(app)
    alerts.init_app(app)
    alert_views.init_app(app)
    # auth comes first: its before_request (load_user) must run before the others'.
    for blueprint in (auth.bp, dashboard.bp, monitors.bp, incident_views.bp, alert_views.bp, crew.bp, settings.bp, demo.bp,
                      tick.bp):
        app.register_blueprint(blueprint)

    from .dates import utcnow
    app.jinja_env.globals.update(APP_NAME=APP_NAME, TAGLINE=TAGLINE, now=utcnow,
                                 has_page=lambda endpoint: endpoint in app.view_functions)

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/sw.js")
    def service_worker():
        # From the top of the site, not /static/: a service worker only looks after
        # the pages below the address it came from.
        response = app.send_static_file("sw.js")
        response.headers["Cache-Control"] = "no-cache"  # always check for a new version
        return response

    @app.get("/favicon.ico")
    def favicon():
        return app.send_static_file("icons/favicon.ico")

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if g.get("user") is not None:
            # Pages with someone's monitors on them are never kept by the browser or a proxy.
            response.headers.setdefault("Cache-Control", "no-store")
        if on_render:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    def error_page(code, message):
        return render_template("error.html", code=code, message=message), code

    @app.errorhandler(400)
    def bad_request(error):
        return error_page(400, error.description or "Something in that request was wrong.")

    @app.errorhandler(403)
    def forbidden(error):
        return error_page(403, error.description or "You can't do that.")

    @app.errorhandler(404)
    def not_found(error):
        return error_page(404, "We couldn't find that. It may have been deleted.")

    @app.errorhandler(409)
    def conflict(error):
        return error_page(409, error.description or "That can't be done right now.")

    @app.errorhandler(413)
    def too_big(error):
        return error_page(413, "That is too big to send.")

    @app.errorhandler(429)
    def too_many(error):
        return error_page(429, error.description or "Too many tries. Please wait a little and try again.")

    return app
