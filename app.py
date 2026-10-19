"""Authenticated local dashboard for the paper-only Ares bot."""
from __future__ import annotations

import hmac
from functools import wraps

from flask import Flask, Response, jsonify, render_template, request

from config import SETTINGS
from engine import ENGINE
from state import event, get_value, initialize, recent_events, recent_trades, set_value

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
initialize()
ENGINE.start()


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not SETTINGS.ui_user or not SETTINGS.ui_password:
            return Response("Dashboard credentials are not configured.", 503)
        if not auth or not hmac.compare_digest(auth.username or "", SETTINGS.ui_user) or not hmac.compare_digest(auth.password or "", SETTINGS.ui_password):
            return Response("Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="Ares paper bot"'})
        return view(*args, **kwargs)
    return wrapped


@app.get("/")
@require_auth
def home():
    return render_template("index.html", state=ENGINE.snapshot(), trades=recent_trades(), events=recent_events())


@app.get("/api/state")
@require_auth
def api_state():
    return jsonify({"state": ENGINE.snapshot(), "trades": recent_trades(10), "events": recent_events(10)})


@app.post("/control/pause")
@require_auth
def pause():
    if request.headers.get("X-Ares-Action") != "dashboard":
        return jsonify({"ok": False, "error": "Invalid dashboard action"}), 400
    set_value("paused", "1")
    event("info", "Bot paused from dashboard")
    return jsonify({"ok": True, "paused": True})


@app.post("/control/resume")
@require_auth
def resume():
    if request.headers.get("X-Ares-Action") != "dashboard":
        return jsonify({"ok": False, "error": "Invalid dashboard action"}), 400
    if not SETTINGS.configured or not SETTINGS.paper:
        return jsonify({"ok": False, "error": "Fresh paper credentials and dashboard credentials are required"}), 409
    set_value("paused", "0")
    event("info", "Paper trading resumed from dashboard")
    return jsonify({"ok": True, "paused": False})


if __name__ == "__main__":
    # Network binding requires HTTPS certs; never expose this service publicly.
    tls = (SETTINGS.tls_cert, SETTINGS.tls_key) if SETTINGS.tls_cert and SETTINGS.tls_key else None
    host = SETTINGS.bind_host if tls else "127.0.0.1"
    app.run(host=host, port=8080, threaded=True, ssl_context=tls)
