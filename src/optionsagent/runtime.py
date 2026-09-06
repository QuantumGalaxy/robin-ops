"""Transactional paper-account checkpoints and a durable audit trail."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .models import ExitReason, OptionContract, Position, TradeRecord
from .orders import OrderRecord
from .risk import RiskState


def position(row):
    row = dict(row)
    c = dict(row.pop("contract"))
    c["expiry"] = date.fromisoformat(c["expiry"])
    row["opened_at"] = datetime.fromisoformat(row["opened_at"])
    return Position(contract=OptionContract(**c), **row)


def encode(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


class RuntimeStore:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "runtime.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, body TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit "
                "(id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "kind TEXT, body TEXT)"
            )

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def event(self, kind, body):
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit(kind, body) VALUES (?, ?)",
                (kind, json.dumps(body, default=encode)),
            )

    def save(self, engine):
        broker = engine.broker
        payload = {
            "config": engine.config.model_dump(mode="json"),
            "positions": [asdict(p) for p in engine.portfolio.positions.values()],
            "trades": [asdict(t) for t in engine.portfolio.trades],
            "realized_pnl": engine.portfolio.realized_pnl,
            "risk": asdict(engine.risk.state),
            "orders": {k: asdict(v) for k, v in engine.orders.orders.items()},
            "reconcile_halt": engine.reconcile_halt,
            "history": {s: list(v) for s, v in engine.history._series.items()},
            "history_dates": {s: d.isoformat() for s, d in engine.history._dates.items()},
        }
        if broker.synchronous_fills:
            payload["paper"] = {
                "cash": broker.cash,
                "positions": [asdict(p) for p in broker.positions()],
                "marks": broker._marks,
                "day_trades": broker._day_trades,
                "rng": broker._rng.getstate(),
            }
        if engine.config.data_provider == "synthetic":
            market = engine.data
            payload["synthetic"] = {
                "today": market.today,
                "fraction": market._day_fraction,
                "rng": market.rng.getstate(),
                "state": {k: asdict(v) for k, v in market.state.items()},
            }
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO checkpoint(id, body) VALUES (1, ?)",
                (json.dumps(payload, default=encode),),
            )

    def read(self):
        with self.connect() as db:
            row = db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def restore(self, engine):
        data = self.read()
        if data is None:
            if engine.portfolio.positions:
                raise RuntimeError(
                    "Legacy holdings need explicit migration; refusing to reset cash"
                )
            return False
        old = data["config"]
        if (
            old["broker"]["kind"] != engine.config.broker.kind
            or old["data_provider"] != engine.config.data_provider
        ):
            raise RuntimeError("Use a separate state_dir for each broker/data source")
        pf = engine.portfolio
        pf.positions = {p.contract.occ_symbol: p for p in map(position, data["positions"])}
        pf.trades = []
        for raw in data["trades"]:
            row = dict(raw)
            for key in ("opened_at", "closed_at"):
                row[key] = datetime.fromisoformat(row[key])
            row["reason"] = ExitReason(row["reason"])
            pf.trades.append(TradeRecord(**row))
        pf.realized_pnl = data["realized_pnl"]
        risk = data["risk"]
        if risk["session_date"]:
            risk["session_date"] = date.fromisoformat(risk["session_date"])
        engine.risk.state = RiskState(**risk)
        engine.reconcile_halt = data["reconcile_halt"]
        engine.orders.orders = {k: OrderRecord(**r) for k, r in data["orders"].items()}
        engine.orders.save()
        for symbol, values in data["history"].items():
            for value in values:
                engine.history.push(symbol, value)
        engine.history._dates = {s: date.fromisoformat(d) for s, d in data["history_dates"].items()}
        if "paper" in data:
            b = engine.broker
            paper = data["paper"]
            b.cash = paper["cash"]
            b._positions = {p.contract.occ_symbol: p for p in map(position, paper["positions"])}
            b._marks = paper["marks"]
            b._day_trades = [date.fromisoformat(d) for d in paper["day_trades"]]

            def tuples(x):
                return tuple(map(tuples, x)) if isinstance(x, list) else x

            b._rng.setstate(tuples(paper["rng"]))
        if "synthetic" in data:
            from .marketdata.synthetic import SymbolState

            market = engine.data
            saved = data["synthetic"]
            market.today = date.fromisoformat(saved["today"])
            market._day_fraction = saved["fraction"]
            market.rng.setstate(tuples(saved["rng"]))
            market.state = {}
            for symbol, raw in saved["state"].items():
                if raw["earnings"]:
                    raw["earnings"] = date.fromisoformat(raw["earnings"])
                market.state[symbol] = SymbolState(**raw)
        pf.save()
        return True


@contextmanager
def single_writer(state_dir):
    """Fail immediately if another runner owns this account directory (POSIX)."""
    import fcntl

    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    with (path / "runner.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another agent is using this state directory") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
