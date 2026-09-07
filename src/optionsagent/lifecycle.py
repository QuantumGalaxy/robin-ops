"""Durable broker lifecycle accounting. Consumes verified events; never submits trades."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

TERMINAL = {"filled", "cancelled", "rejected", "failed", "expired"}
STATES = TERMINAL | {
    "pending",
    "queued",
    "confirmed",
    "partially_filled",
    "pending_cancelled",
    "unknown",
}


def limit_tick(price, tick, side):
    from decimal import ROUND_CEILING, ROUND_FLOOR

    p, t = Decimal(str(price)), Decimal(str(tick))
    if not p.is_finite() or not t.is_finite() or p <= 0 or t <= 0 or side not in {"buy", "sell"}:
        raise ValueError("Invalid limit/tick/side")
    return float(
        (p / t).to_integral_value(rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING) * t
    )


class Lifecycle:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, account "
                "TEXT, contract TEXT, side TEXT, qty INTEGER, state TEXT, broker_id TEXT UNIQUE)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS executions(id TEXT PRIMARY KEY, "
                "intent TEXT, qty INTEGER, price TEXT, fee TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, "
                "intent TEXT, body TEXT)"
            )

    def reserve(self, identity, account, contract, side, qty):
        if (
            not account
            or not contract
            or side not in {"buy", "sell"}
            or isinstance(qty, bool)
            or not isinstance(qty, int)
            or qty <= 0
        ):
            raise ValueError(
                "Explicit account, contract, side and positive integer quantity required"
            )
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT account,contract,side,qty FROM intents WHERE id=?", (identity,)
            ).fetchone()
            if old:
                if old != (account, contract, side, qty):
                    raise ValueError("Intent identity collision")
                return False
            if db.execute(
                "SELECT 1 FROM intents WHERE account=? AND contract=? AND state "
                "NOT IN ('filled','cancelled','rejected','failed','expired')",
                (account, contract),
            ).fetchone():
                raise ValueError("Unresolved order already exists for this contract")
            db.execute(
                "INSERT INTO intents VALUES(?,?,?,?,?,?,NULL)",
                (identity, account, contract, side, qty, "pending"),
            )
            return True

    def apply(self, identity, *, account, broker_id, state, executions, source):
        if state not in STATES or not source.strip() or not broker_id:
            raise ValueError("Verified broker identity, source and state required")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT account,qty,state,broker_id FROM intents WHERE id=?", (identity,)
            ).fetchone()
            if not row or row[0] != account or (row[3] and row[3] != broker_id):
                raise ValueError("Account/order identity mismatch")
            for fill in executions:
                qty = fill["quantity"]
                price = Decimal(str(fill["price"]))
                fee = Decimal(str(fill["fee"]))
                if (
                    not isinstance(qty, int)
                    or isinstance(qty, bool)
                    or qty <= 0
                    or not price.is_finite()
                    or price <= 0
                    or not fee.is_finite()
                    or fee < 0
                    or not fill["id"]
                ):
                    raise ValueError("Invalid execution")
                values = (identity, qty, str(price), str(fee))
                prior = db.execute(
                    "SELECT intent,qty,price,fee FROM executions WHERE id=?", (fill["id"],)
                ).fetchone()
                if prior and prior != values:
                    raise ValueError("Execution identity collision")
                db.execute(
                    "INSERT OR IGNORE INTO executions VALUES(?,?,?,?,?)", (fill["id"], *values)
                )
            total = db.execute(
                "SELECT COALESCE(SUM(qty),0) FROM executions WHERE intent=?", (identity,)
            ).fetchone()[0]
            if total > row[1] or (state == "filled" and total != row[1]):
                raise ValueError("Fill quantity inconsistent with order")
            if state in {"rejected", "failed"} and total:
                raise ValueError("Rejected order cannot contain fills")
            # Late fills are retained even after cancellation; cancel requests are not terminal.
            resolved = "filled" if total == row[1] else state
            if row[2] in TERMINAL and state not in TERMINAL:
                resolved = row[2]
            db.execute(
                "UPDATE intents SET state=?,broker_id=? WHERE id=?", (resolved, broker_id, identity)
            )
            db.execute(
                "INSERT INTO evidence(intent,body) VALUES(?,?)",
                (identity, json.dumps({"source": source, "state": state, "executed": total})),
            )
            return {"state": resolved, "filled": total, "remaining": row[1] - total}

    def reconcile(self, account, expected, actual, expected_cash, actual_cash, tolerance=0.01):
        if not account:
            raise ValueError("Explicit account required")
        missing = [
            k for k in expected.keys() | actual.keys() if expected.get(k, 0) != actual.get(k, 0)
        ]
        cash_ok = abs(Decimal(str(expected_cash)) - Decimal(str(actual_cash))) <= Decimal(
            str(tolerance)
        )
        return {
            "matched": not missing and cash_ok,
            "position_mismatches": missing,
            "cash_matched": cash_ok,
        }
