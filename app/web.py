from __future__ import annotations
import os
from flask import Flask, request, render_template
from app.config import load_config
from app.db import connect, init_schema
from app.catalog import load_catalog

def create_app() -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder=None)
    # Load config ONCE at startup. Missing env vars surface here, not on
    # the first real request.
    app.config["TERMINE_CONFIG"] = load_config()

    @app.route("/healthz")
    def healthz():
        cfg = app.config["TERMINE_CONFIG"]
        conn = connect(cfg.db_path)
        conn.execute("SELECT 1").fetchone()
        return "ok", 200

    @app.route("/")
    def index():
        lang = request.args.get("lang", "de")
        if lang not in ("de", "en"):
            lang = "de"
        city = request.args.get("city", "leipzig")
        catalog = load_catalog(city)
        return render_template("form.html",
                               lang=lang,
                               city=city,
                               appointment_types=catalog.appointment_types,
                               locations=catalog.locations,
                               kofi_url=app.config["TERMINE_CONFIG"].kofi_url)

    return app

# NOTE: do NOT instantiate `app = create_app()` at module level. Doing so
# calls load_config() at import time, which raises KeyError if any env var
# is missing — including during test collection, where fixtures haven't
# yet had a chance to monkeypatch.setenv(). Gunicorn supports the
# application-factory pattern directly: `gunicorn app.web:create_app()`.
