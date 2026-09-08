"""Loopback-only dashboard with persistent private access and browser sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .health import Alerts
from .runtime import RuntimeStore


def dashboard_token(state_dir):
    """Keep restart/recovery links valid; credential stays local and owner-only."""
    path = Path(state_dir) / "dashboard-access.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | flags)
        with os.fdopen(fd) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError(
                    "Dashboard access key must be a private file (permissions 0600)"
                ) from None
            value = f.read(256).strip()
        if len(value) != 43 or any(
            c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for c in value
        ):
            raise ValueError("Invalid dashboard access key; restore the private key file") from None
        return value
    with os.fdopen(fd, "w") as f:
        value = secrets.token_urlsafe(32)
        f.write(value + "\n")
    return value


def state(config):
    from .iv_history import history_status
    from .paper_lab import comparison_status

    iv_history = history_status(
        config.state_dir,
        config.universe.symbols,
        experimental=config.paper_iv_history_experiment,
    )
    if config.robinhood_iv_daily_collection:
        from .iv_daily import collection_status, select_rank

        collection = collection_status(config.state_dir, config.universe.symbols)
        iv_history["collection"] = collection
        iv_history["status"] = (
            "Paper IV filter ON · imported DoltHub history plus direct API updates; "
            "Robinhood history collected separately. Automatic source switching "
            + ("ON" if config.paper_iv_auto_switch else "OFF")
        )
        for row in iv_history["symbols"]:
            rh = next(r for r in collection["symbols"] if r["symbol"] == row["symbol"])
            details = select_rank(config.state_dir, row["symbol"], auto=config.paper_iv_auto_switch)
            row["active_source"] = (
                "Robinhood"
                if rh["switched_at"] and config.paper_iv_auto_switch
                else "DoltHub (experimental)"
            )
            row["robinhood_days"] = rh["days"]
            row["robinhood_latest"] = rh["latest"]
            if row["active_source"] == "Robinhood":
                row.update(
                    days=details["days"],
                    latest=details["latest"],
                    missing_sessions=details["missing_sessions"],
                    fresh=details["latest"] == details["required_through"],
                    paper_rank=details["rank"],
                    rank_reason=details["reason"],
                )
        if not collection["healthy"]:
            Alerts(config.state_dir).set(
                "iv_collector", "Daily IV collector heartbeat is missing or stale"
            )
    iv_ready = {r["symbol"] for r in iv_history["symbols"] if r.get("paper_rank") is not None}
    store = RuntimeStore(config.state_dir)
    snapshot = store.read()
    alerts = Alerts(config.state_dir)
    alerts.watchdog(max_age=max(180, config.execution.poll_interval_seconds * 3))
    with store.connect() as db:
        recent = db.execute("SELECT at,kind,body FROM audit ORDER BY id DESC LIMIT 20").fetchall()
        history = db.execute(
            "SELECT body FROM audit WHERE kind='loop' ORDER BY id DESC LIMIT 500"
        ).fetchall()
    latest_block = ""
    if history:
        latest_block = json.loads(history[0][0]).get("blocked_reason", "")
    events = []
    for at, kind, raw in recent:
        data = json.loads(raw)
        message = (
            data.get("reason")
            or data.get("blocked_reason")
            or data.get("message")
            or ", ".join(data.get("errors", []))
            or "; ".join(data.get("skipped", [])[:3])
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
                (
                    symbol in iv_ready
                    if config.paper_iv_history_experiment
                    else (
                        not config.entry.require_iv_rank
                        or (r.iv_rank is not None and r.iv_history_days >= 200)
                    )
                )
                and r.earnings_checked
                and len(r.daily_closes) >= 30
                for symbol, r in ref.symbols.items()
                if symbol in config.universe.symbols
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
        "iv_history": iv_history,
        "entry_block": latest_block,
        "experiments": comparison_status(config.state_dir),
    }


def make_server(config, port=8766, token=None):
    public_origin = os.environ.get("ROBIN_OPS_DASHBOARD_ORIGIN", "")
    if public_origin:
        parsed = urlsplit(public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Dashboard public origin must be an HTTPS origin")
    token = token or dashboard_token(config.state_dir)
    cookie_value = hmac.new(token.encode(), b"dashboard-session-v1", hashlib.sha256).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, kind="application/json", session=False):
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if session:
                self.send_header(
                    "Set-Cookie",
                    (
                        f"robin_desk_{self.server.server_port}={cookie_value}; "
                        "HttpOnly; SameSite=Strict; Path=/api/; Max-Age=604800"
                        + ("; Secure" if public_origin else "")
                    ),
                )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src "
                "'self' 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self, bearer_only=False):
            host = self.headers.get("Host", "")
            expected_origin = public_origin or f"http://127.0.0.1:{self.server.server_port}"
            expected_host = urlsplit(expected_origin).netloc
            if host != expected_host:
                return False
            origin = self.headers.get("Origin")
            if origin and origin != expected_origin:
                return False
            if secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return True
            if bearer_only:
                return False
            # Cookie-only mutations require an exact browser Origin, not merely SameSite.
            if self.command == "POST" and origin != expected_origin:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                entry = cookie.get(f"robin_desk_{self.server.server_port}")
                return bool(entry and secrets.compare_digest(entry.value, cookie_value))
            except (CookieError, TypeError):
                return False

        def do_GET(self):
            if self.path == "/":
                self.send(
                    200,
                    files("optionsagent")
                    .joinpath("web/index.html")
                    .read_bytes()
                    .replace(
                        b"LOCAL WORKSPACE",
                        b"AWS PAPER DESK" if public_origin else b"LOCAL WORKSPACE",
                    ),
                    "text/html; charset=utf-8",
                )
                return
            if not self.authorized():
                self.send(403, {"error": "Unauthorized"})
                return
            if urlsplit(self.path).path == "/api/report":
                from .daily_report import daily_report

                try:
                    day = parse_qs(urlsplit(self.path).query).get("date", [""])[0]
                    self.send(200, daily_report(config.state_dir, day))
                except ValueError:
                    self.send(400, {"error": "Use a date in YYYY-MM-DD format"})
                except (OSError, sqlite3.Error):
                    self.send(503, {"error": "Report unavailable"})
                return
            if self.path != "/api/state":
                self.send(404, {"error": "Not found"})
                return
            try:
                self.send(200, state(config))
            except (OSError, ValueError, sqlite3.Error):
                self.send(503, {"error": "State unavailable"})

        def do_POST(self):
            if self.path == "/api/session":
                if not self.authorized(bearer_only=True):
                    self.send(403, {"error": "Access link required"})
                    return
                self.send(200, {"ok": True}, session=True)
                return
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
    origin = (
        os.environ.get("ROBIN_OPS_DASHBOARD_ORIGIN") or f"http://127.0.0.1:{server.server_port}"
    )
    url = f"{origin}/#{token}"
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
