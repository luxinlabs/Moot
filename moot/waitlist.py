"""Waitlist for the landing page: people scan a QR code, type their name and email, and land in runtime/waitlist.db.

Phones can't reach a loopback-only Studio, and the Studio must not be exposed on the network (its routes include
API-key settings and paid runs). So the sign-up form gets its own tiny server that serves only /join and the
sign-up POST. It is opt-in (--waitlist) and the export stays on the loopback Studio.
"""
import csv
import io
import re
import socket
import threading
import time
from pathlib import Path

from flask import Blueprint, Flask, jsonify, request, send_from_directory

from . import RUNTIME
from .store import Store

STATIC = Path(__file__).resolve().parent / "static"
bp = Blueprint("waitlist", __name__)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STATE = {"store": None, "join_url": None}
_LOCK = threading.Lock()


def store():
    with _LOCK:
        if STATE["store"] is None:
            RUNTIME.mkdir(exist_ok=True)
            s = Store(RUNTIME / "waitlist.db")
            s.db.execute("CREATE TABLE IF NOT EXISTS waitlist(email TEXT PRIMARY KEY, name TEXT, created_at REAL)")
            STATE["store"] = s
        return STATE["store"]


def count():
    return store().rows("SELECT COUNT(*) AS n FROM waitlist")[0]["n"]


def lan_ip():
    """The address other devices on this Wi-Fi use to reach this machine (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


@bp.get("/join")
def join_page():
    return send_from_directory(STATIC, "join.html")


@bp.post("/api/waitlist")
def join():
    body = request.get_json(silent=True) or request.form
    name = " ".join(str(body.get("name") or "").split())[:80]
    email = str(body.get("email") or "").strip().lower()[:200]
    if not name:
        return jsonify({"error": {"code": "NAME", "message": "Please tell us your name."}}), 400
    if not EMAIL.match(email):
        return jsonify({"error": {"code": "EMAIL", "message": "That email doesn't look right."}}), 400
    s = store()
    existing = s.rows("SELECT email FROM waitlist WHERE email=?", (email,))
    if not existing:
        s.insert("waitlist", email=email, name=name, created_at=time.time())
    return jsonify({"ok": True, "already": bool(existing), "count": count()})


def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "email", "joined_utc"])
    for r in store().rows("SELECT name, email, created_at FROM waitlist ORDER BY created_at"):
        w.writerow([r["name"], r["email"], time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["created_at"]))])
    return out.getvalue()


def start_server(port, public_url=None, host="0.0.0.0"):
    """Serve only the sign-up form on the network, in a background thread. Returns the URL for the QR code."""
    from werkzeug.serving import make_server
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(bp)
    server = make_server(host, port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if public_url:
        STATE["join_url"] = public_url.rstrip("/") + ("" if public_url.rstrip("/").endswith("/join") else "/join")
    else:
        STATE["join_url"] = f"http://{lan_ip() or '127.0.0.1'}:{port}/join"
    print(f"Waitlist sign-up on {STATE['join_url']} (serves only the form)")
    return server
