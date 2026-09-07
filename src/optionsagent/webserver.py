"""Loopback-only dashboard. Private per-launch token, no credential/broker endpoints."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path

from .health import Alerts
from .runtime import RuntimeStore


def state(config):
    store = RuntimeStore(config.state_dir)
    snapshot = store.read()
    alerts = Alerts(config.state_dir)
    alerts.watchdog(max_age=max(180, config.execution.poll_interval_seconds * 3))
    with store.connect() as db:
        recent = db.execute("SELECT at,kind,body FROM audit ORDER BY id DESC LIMIT 20").fetchall()
        history = db.execute(
            "SELECT body FROM audit WHERE kind='loop' ORDER BY id DESC LIMIT 500"
        ).fetchall()
    events = []
    for at, kind, raw in recent:
        data = json.loads(raw)
        message = (
            data.get("reason")
            or data.get("blocked_reason")
            or data.get("message")
            or ", ".join(data.get("errors", []))
            or kind.replace("_", " ")
        )
        events.append({"at": at, "kind": kind, "message": str(message)[:500]})
    curve = [
        {"at": d.get("at"), "equity": d["equity"]}
        for (raw,) in reversed(history)
        if "equity" in (d := json.loads(raw))
    ]
    reference = "Synthetic" if config.data_provider == "synthetic" else "Unavailable"
    if config.reference_data_file:
        try:
            from datetime import UTC, datetime

            from .marketdata.reference import ReferenceSnapshot

            ref = ReferenceSnapshot.model_validate_json(
                Path(config.reference_data_file).read_text()
            )
            ready = sum(
                r.iv_rank is not None
                and r.iv_history_days >= 200
                and r.earnings_checked
                and len(r.daily_closes) >= 30
                for r in ref.symbols.values()
            )
            reference = (
                f"{ready}/{len(config.universe.symbols)} ready"
                if (datetime.now(UTC) - ref.as_of).total_seconds() < 86400
                else "Stale"
            )
        except (ValueError, OSError):
            pass
    return {
        "checkpoint": snapshot,
        "paused": Path(config.risk.kill_switch_file).exists(),
        "pending": sum(
            o["state"] == "pending" for o in (snapshot or {}).get("orders", {}).values()
        ),
        "alerts": alerts.list(),
        "events": events,
        "curve": curve,
        "reference": reference,
    }


def make_server(config, port=8766, token=None):
    token = token or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, kind="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src "
                "'self' 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        def authorized(self):
            host = self.headers.get("Host", "")
            if host != f"127.0.0.1:{self.server.server_port}":
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                return False
            return secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)

        def do_GET(self):
            if self.path == "/":
                self.send(
                    200,
                    files("optionsagent").joinpath("web/index.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
                return
            if not self.authorized():
                self.send(403, {"error": "Unauthorized"})
                return
            if self.path != "/api/state":
                self.send(404, {"error": "Not found"})
                return
            try:
                self.send(200, state(config))
            except (OSError, ValueError, sqlite3.Error):
                self.send(503, {"error": "State unavailable"})

        def do_POST(self):
            if not self.authorized():
                self.send(403, {"error": "Unauthorized"})
                return
            if self.path != "/api/control":
                self.send(404, {"error": "Not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError()
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError()
                action = body.get("action")
                kill = Path(config.risk.kill_switch_file)
                if action == "pause":
                    kill.parent.mkdir(parents=True, exist_ok=True)
                    kill.touch()
                elif action == "resume":
                    kill.unlink(missing_ok=True)
                elif action == "ack":
                    Alerts(config.state_dir).ack(body["key"])
                else:
                    raise ValueError()
                RuntimeStore(config.state_dir).event("operator", {"action": action})
                self.send(200, {"ok": True})
            except (ValueError, KeyError, OSError):
                self.send(400, {"error": "Invalid request"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler), token


def serve(config, port=8766, open_browser=True):
    server, token = make_server(config, port)
    url = f"http://127.0.0.1:{server.server_port}/#{token}"
    print("Private dashboard: " + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    stop = threading.Event()

    def watchdog():
        while not stop.wait(30):
            Alerts(config.state_dir).watchdog(max(180, config.execution.poll_interval_seconds * 3))

    thread = threading.Thread(target=watchdog, daemon=True)
    thread.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
