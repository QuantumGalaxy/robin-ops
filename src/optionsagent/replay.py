"""Point-in-time quote replay with the real engine, transaction costs and held-out folds."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .brokers.paper import PaperBroker
from .engine import TradingEngine
from .greeks import compute_greeks, implied_vol, years_to_expiry
from .marketdata.base import MarketDataProvider
from .marketdata.reference import ReferenceSnapshot
from .models import OptionContract, OptionQuote
from .orders import OrderRegistry
from .portfolio import Portfolio


class ReplayData(MarketDataProvider):
    def __init__(self):
        self.frame = {}
        self.quotes = []

    def advance(self, frame):
        self.frame = frame
        self.now = datetime.fromisoformat(frame["at"])
        if self.now.tzinfo is None:
            raise ValueError("Replay time must include timezone")
        ref = ReferenceSnapshot.model_validate(frame["reference"])
        if ref.as_of > self.now:
            raise ValueError("Future reference data would cause look-ahead")
        if (self.now - ref.as_of).total_seconds() > 86400:
            raise ValueError("Stale reference data")
        for r in ref.symbols.values():
            if r.daily_closes_as_of and r.daily_closes_as_of >= self.now.date():
                raise ValueError("Daily history includes incomplete/future session")
        self.quotes = []
        for raw in frame["quotes"]:
            c = dict(raw["contract"])
            c["expiry"] = date.fromisoformat(c["expiry"])
            at = datetime.fromisoformat(raw["as_of"])
            if at.tzinfo is None or at > self.now:
                raise ValueError("Future/naive quote timestamp")
            q = OptionQuote(
                OptionContract(**c),
                raw["bid"],
                raw["ask"],
                raw["underlying_price"],
                open_interest=raw["open_interest"],
                volume=raw["volume"],
                as_of=at,
            )
            if not q.is_tradeable():
                raise ValueError("Invalid replay quote")
            t = years_to_expiry(q.contract.days_to_expiry(self.now.date()))
            iv = implied_vol(
                q.mid, q.underlying_price, q.contract.strike, t, 0.042, 0, q.contract.right
            )
            if iv:
                q.greeks = compute_greeks(
                    q.underlying_price, q.contract.strike, t, 0.042, iv, 0, q.contract.right
                )
            self.quotes.append(q)

    def underlying_price(self, symbol):
        return next((q.underlying_price for q in self.quotes if q.contract.symbol == symbol), None)

    def option_chain(self, symbol, min_dte, max_dte):
        return [
            q
            for q in self.quotes
            if q.contract.symbol == symbol
            and min_dte <= q.contract.days_to_expiry(self.now.date()) <= max_dte
        ]

    def quote(self, symbol, expiry, strike, right):
        return next(
            (
                q
                for q in self.quotes
                if (q.contract.symbol, q.contract.expiry, q.contract.strike, q.contract.right)
                == (symbol, expiry, strike, right)
            ),
            None,
        )

    def reference(self, symbol):
        return self.frame["reference"].get("symbols", {}).get(symbol, {})

    def historical_closes(self, symbol, days=90):
        return self.reference(symbol).get("daily_closes", [])[-days:]

    def iv_rank(self, symbol):
        r = self.reference(symbol)
        return r.get("iv_rank") if r.get("iv_history_days", 0) >= 200 else None

    def earnings_known(self, symbol):
        return self.reference(symbol).get("earnings_checked") is True

    def next_earnings_date(self, symbol):
        d = self.reference(symbol).get("earnings")
        return date.fromisoformat(d) if d else None

    def is_market_open(self):
        s = self.frame["reference"].get("session")
        return bool(
            s and datetime.fromisoformat(s["open"]) <= self.now < datetime.fromisoformat(s["close"])
        )


def load_frames(path):
    frames = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    times = [datetime.fromisoformat(f["at"]) for f in frames]
    if not frames or any(a >= b for a, b in zip(times, times[1:], strict=False)):
        raise ValueError("Replay needs strictly increasing observations")
    if any(not f.get("source") for f in frames):
        raise ValueError("Replay source provenance required")
    return frames


def replay(config, frames, *, miss_probability=0.1, fill_aggression=0.5):
    cfg = config.model_copy(deep=True)
    cfg.data_provider = "robinhood_mcp"
    cfg.reference_data_file = None
    data = ReplayData()
    data.advance(frames[0])
    broker = PaperBroker(
        starting_equity=cfg.broker.starting_equity,
        miss_probability=miss_probability,
        fill_aggression=fill_aggression,
        clock=lambda: data.now,
    )
    curve = []
    errors = []
    with tempfile.TemporaryDirectory() as directory:
        cfg.state_dir = directory
        cfg.risk.kill_switch_file = str(Path(directory) / "KILL")
        engine = TradingEngine(
            cfg,
            data,
            broker,
            Portfolio(Path(directory), persist=False),
            orders=OrderRegistry(persist=False),
        )
        engine.warmup()
        peak = broker.equity()
        dd = 0
        for frame in frames:
            data.advance(frame)
            report = engine.run_once(data.now)
            equity = broker.equity()
            peak = max(peak, equity)
            dd = min(dd, equity / peak - 1)
            curve.append({"at": frame["at"], "equity": equity})
            errors.extend(report.errors)
        return {
            "summary": engine.portfolio.summary(),
            "return": broker.equity() / cfg.broker.starting_equity - 1,
            "max_drawdown": dd,
            "equity_curve": curve,
            "open_positions": len(engine.portfolio.positions),
            "monitoring_errors": len(errors),
            "note": "Open holdings marked, not liquidated; missing quote paths limit "
            "validity. No profitability certification.",
        }


def walk_forward(config, frames, folds=3):
    if folds < 2 or len(frames) < folds * 2:
        raise ValueError("Need at least two observations per fold")
    size = len(frames) // folds
    result = []
    # Fixed strategy, no parameter optimization on held-out periods.
    for i in range(1, folds):
        test = frames[i * size : (i + 1) * size if i < folds - 1 else len(frames)]
        for name, miss, aggression in [("baseline", 0.1, 0.5), ("worse_fills", 0.25, 1.0)]:
            r = replay(config, test, miss_probability=miss, fill_aggression=aggression)
            r.pop("equity_curve")
            result.append(
                {
                    "fold": i,
                    "scenario": name,
                    "train_end": frames[i * size - 1]["at"],
                    "test_start": test[0]["at"],
                    **r,
                }
            )
    return {
        "method": "Expanding historical prefix; fixed config evaluated only on "
        "subsequent held-out observations. Each fold starts flat.",
        "folds": result,
    }


def capture(data, config, path, now=None):
    from .runtime import encode

    ref = json.loads(Path(config.reference_data_file).read_text())
    quotes = []
    for symbol in config.universe.symbols:
        for q in data.option_chain(symbol, config.entry.min_dte, config.entry.max_dte):
            raw = asdict(q)
            raw.pop("greeks", None)
            quotes.append(raw)
    # Keep monitoring paths after a position leaves the entry DTE window.
    from .runtime import RuntimeStore, position

    saved = RuntimeStore(config.state_dir).read() or {}
    present = {q["contract"]["occ_symbol"] for q in quotes}
    for raw in saved.get("positions", []):
        c = position(raw).contract
        if c.occ_symbol not in present:
            q = data.quote(c.symbol, c.expiry, c.strike, c.right)
            if q:
                item = asdict(q)
                item.pop("greeks", None)
                quotes.append(item)
    now = now or datetime.now().astimezone()
    frame = {
        "at": now.isoformat(),
        "source": "Robinhood MCP captured bid/ask",
        "reference": ref,
        "quotes": quotes,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as f:
        f.write(json.dumps(frame, default=encode) + "\n")
    return len(quotes)
