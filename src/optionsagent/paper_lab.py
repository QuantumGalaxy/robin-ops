"""Isolated forward paper comparisons; never promotes a winner or submits real orders."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
import zlib
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .brokers.paper import PaperBroker
from .config import Config, Mode
from .engine import TradingEngine
from .health import Alerts
from .portfolio import Portfolio
from .runtime import RuntimeStore, encode, single_writer

PROFILES = {
    "control-v1": ("Current rules", "Fresh control using the current rules"),
    "stop25-v1": ("Smaller loss stop", "−25% stop; same premium budget as control"),
    "market-v1": ("Market agreement", "Stock direction must agree with SPY; other rules unchanged"),
}


def experiment_configs(base):
    if (
        base.mode != Mode.PAPER
        or base.broker.kind != "paper"
        or base.data_provider != "robinhood_mcp"
    ):
        raise ValueError("Comparisons require Robinhood market data and a paper broker")
    if not base.reference_data_file:
        raise ValueError("Comparisons require a shared reference data file")
    root = Path(base.state_dir).resolve()
    result = {}
    for key in PROFILES:
        raw = base.model_dump(mode="json")
        raw.update(
            state_dir=str(root / "experiments" / key),
            reference_auto_refresh=False,
            iv_history_state_dir=str(root),
            reference_data_file=str(Path(base.reference_data_file).resolve()),
        )
        # All comparisons use the dashboard pause control; each has its own account state.
        raw["risk"]["kill_switch_file"] = str(Path(base.risk.kill_switch_file).resolve())
        if key == "stop25-v1":
            raw["exit"]["stop_loss_pct"] = -0.25
            # Premium = equity * risk_fraction / stop_fraction. Preserve baseline premium.
            raw["sizing"]["risk_per_trade_pct"] = (
                base.sizing.risk_per_trade_pct * 0.25 / abs(base.exit.stop_loss_pct)
            )
        if key == "market-v1":
            raw["entry"]["require_market_alignment"] = True
        result[key] = Config.model_validate(raw)
    return result


def lab_db(root):
    folder = Path(root) / "experiments"
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "comparison.sqlite3", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS readings(
        id INTEGER PRIMARY KEY, at TEXT, method TEXT, arguments TEXT, body TEXT);
      CREATE TABLE IF NOT EXISTS equity(
        profile TEXT, at TEXT, value REAL, PRIMARY KEY(profile,at));
    """)
    return db


class SharedReadings:
    """Reuse recent read-only responses across portfolios; store quote evidence for analysis.

    TTL is deliberately shorter than the 120-second trading freshness limit.
    Long scans can use different timestamps; this is disclosed, not called an exact replay.
    """

    METHODS = {
        "underlying_price",
        "option_chain",
        "quote",
        "historical_closes",
        "iv_rank",
        "next_earnings_date",
        "earnings_known",
        "is_market_open",
    }

    def __init__(self, provider, root):
        self.provider, self.root, self.cache = provider, root, {}

    def clear(self):
        self.cache.clear()

    def __getattr__(self, name):
        target = getattr(self.provider, name)
        if name not in self.METHODS:
            return target

        def call(*args, **kwargs):
            key = (name, json.dumps([args, kwargs], default=encode, sort_keys=True))
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 30:
                return copy.deepcopy(cached[1])
            # Count network latency against TTL; do not extend old data by caching it later.
            started = time.monotonic()
            value = target(*args, **kwargs)
            self.cache[key] = (started, copy.deepcopy(value))
            if name in {"option_chain", "quote", "historical_closes"}:
                records = (
                    [asdict(q) for q in value]
                    if name == "option_chain"
                    else asdict(value)
                    if name == "quote" and value is not None
                    else value
                )
                with closing(lab_db(self.root)) as db, db:
                    db.execute(
                        "INSERT INTO readings(at,method,arguments,body) VALUES (?,?,?,?)",
                        (
                            datetime.now(UTC).isoformat(),
                            name,
                            key[1],
                            zlib.compress(json.dumps(records, default=encode).encode()),
                        ),
                    )
            return value

        return call


class PaperLab:
    def __init__(self, base, provider):
        self.base = base
        self.configs = experiment_configs(base)
        self.data = SharedReadings(provider, base.state_dir)
        self.engines = {}
        self.locks = ExitStack()
        # Prevent another comparison service from touching any of these portfolios.
        self.locks.enter_context(single_writer(Path(base.state_dir) / "experiments"))
        fingerprint = hashlib.sha256(
            json.dumps(
                {k: c.model_dump(mode="json") for k, c in self.configs.items()}, sort_keys=True
            ).encode()
        ).hexdigest()
        try:
            with closing(lab_db(base.state_dir)) as db, db:
                old = db.execute("SELECT value FROM metadata WHERE key='fingerprint'").fetchone()
                if old and old[0] != fingerprint:
                    raise ValueError("Comparison rules changed: create a new experiment version")
                db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('fingerprint',?)", (fingerprint,)
                )
                db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('started_at',?)",
                    (datetime.now(UTC).isoformat(),),
                )
            for key, cfg in self.configs.items():
                self.locks.enter_context(single_writer(cfg.state_dir))
                store = RuntimeStore(cfg.state_dir)
                engine = TradingEngine(
                    cfg,
                    self.data,
                    PaperBroker(cfg.broker.starting_equity),
                    Portfolio(cfg.state_dir),
                    store=store,
                )
                if not store.restore(engine):
                    engine.warmup()
                    engine._checkpoint()
                self.engines[key] = engine
        except BaseException:
            self.close()
            raise

    def close(self):
        self.locks.close()

    def run_once(self, now=None):
        self.data.clear()
        with closing(lab_db(self.base.state_dir)) as db, db:
            # Rolling research evidence, separate from durable trade/account audit.
            db.execute("DELETE FROM readings WHERE julianday(at) < julianday('now','-30 days')")
        for key, engine in self.engines.items():
            try:
                report = engine.run_once(as_of=now)
                Alerts(engine.config.state_dir).clear("comparison_failure")
                with closing(lab_db(self.base.state_dir)) as db, db:
                    db.execute(
                        "INSERT OR REPLACE INTO equity VALUES (?,?,?)",
                        (key, report.at.isoformat(), report.equity),
                    )
            except Exception as exc:
                engine.reconcile_halt = "Comparison failed; review before new entries"
                engine._checkpoint()
                Alerts(engine.config.state_dir).set(
                    "comparison_failure",
                    f"Comparison failure ({type(exc).__name__}); entries halted",
                )
        with closing(lab_db(self.base.state_dir)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('heartbeat',?)",
                (datetime.now(UTC).isoformat(),),
            )


def comparison_status(root, now=None):
    path = Path(root) / "experiments" / "comparison.sqlite3"
    if not path.exists():
        return {"enabled": False, "profiles": []}
    now = now or datetime.now(UTC)
    result = {"enabled": True, "profiles": []}
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        meta = dict(db.execute("SELECT key,value FROM metadata"))
        result.update(started_at=meta.get("started_at"), heartbeat=meta.get("heartbeat"))
        for key, (name, description) in PROFILES.items():
            state_path = Path(root) / "experiments" / key / "runtime.sqlite3"
            if not state_path.exists():
                continue
            with closing(
                sqlite3.connect(f"{state_path.resolve().as_uri()}?mode=ro", uri=True)
            ) as state:
                row = state.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()
                if not row:
                    continue
                s = json.loads(row[0])
            trades = s["trades"]
            gains = [t["pnl"] for t in trades if t["pnl"] > 0]
            losses = [t["pnl"] for t in trades if t["pnl"] < 0]
            paper = s["paper"]
            from .runtime import position

            equity = paper["cash"] + sum(
                100 * p.quantity * paper["marks"].get(p.contract.occ_symbol, p.entry_price)
                for p in map(position, s["positions"])
            )
            peak = s["config"]["broker"]["starting_equity"]
            dd = 0
            for (value,) in db.execute(
                "SELECT value FROM equity WHERE profile=? ORDER BY at", (key,)
            ):
                peak = max(peak, value)
                dd = max(dd, 1 - value / peak)
            result["profiles"].append(
                dict(
                    key=key,
                    name=name,
                    description=description,
                    equity=equity,
                    return_pct=equity / s["config"]["broker"]["starting_equity"] - 1,
                    positions=[str(position(p).contract) for p in s["positions"]],
                    last_closed=[
                        {k: t[k] for k in ("contract", "pnl", "closed_at", "reason")}
                        for t in trades[-3:]
                    ],
                    closed_trades=len(trades),
                    open_positions=len(s["positions"]),
                    net_pnl=s["realized_pnl"],
                    win_rate=len(gains) / len(trades) if trades else None,
                    average_win=sum(gains) / len(gains) if gains else None,
                    average_loss=sum(losses) / len(losses) if losses else None,
                    expectancy=sum(t["pnl"] for t in trades) / len(trades) if trades else None,
                    profit_factor=sum(gains) / abs(sum(losses)) if losses else None,
                    max_drawdown=dd,
                    saved_at=s["saved_at"],
                    healthy=(now - datetime.fromisoformat(s["saved_at"])).total_seconds() < 600,
                    blocked=s["reconcile_halt"] or s["risk"]["halted_reason"] or "",
                )
            )
    return result
