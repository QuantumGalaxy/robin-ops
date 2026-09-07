"""Persistent, deduplicated local alerts and an independent process watchdog."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


class Alerts:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "runtime.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS alerts(key TEXT PRIMARY KEY, severity "
                "TEXT, message TEXT, active INTEGER, acknowledged INTEGER, updated_at TEXT)"
            )

    def set(self, key, message, severity="warning"):
        with closing(sqlite3.connect(self.path)) as db, db:
            old = db.execute("SELECT message,active FROM alerts WHERE key=?", (key,)).fetchone()
            if old == (message, 1):
                return False
            db.execute(
                "INSERT OR REPLACE INTO alerts VALUES(?,?,?,?,?,?)",
                (key, severity, message, 1, 0, datetime.now(UTC).isoformat()),
            )
        return True

    def clear(self, key):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE alerts SET active=0 WHERE key=?", (key,))

    def ack(self, key):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE alerts SET acknowledged=1 WHERE key=?", (key,))

    def list(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in db.execute("SELECT * FROM alerts ORDER BY active DESC,updated_at DESC")
            ]

    def watchdog(self, max_age=180):
        with closing(sqlite3.connect(self.path)) as db, db:
            try:
                row = db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()
            except sqlite3.OperationalError:
                row = None
        if not row:
            return self.set("runner", "Agent has not saved a checkpoint yet")
        data = json.loads(row[0])
        stamp = data.get("saved_at")
        if (
            not stamp
            or (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds() > max_age
        ):
            return self.set(
                "runner",
                "Agent heartbeat is stale. Position monitoring may have stopped.",
                "critical",
            )
        self.clear("runner")
        return False
