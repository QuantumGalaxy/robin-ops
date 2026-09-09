"""Durable, independent near-close Robinhood IV collection and paper source selection.

V1: first valid ATM standard call snapshot in the final 15 minutes of XNYS,
expiry 20–45 calendar days nearest 30. No vendor rows or legacy observations
are promoted into this archive. Missing sessions are never fabricated.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .health import Alerts
from .iv_history import paper_iv_rank
from .mcp.robinhood import RobinhoodMcp
from .reference_feed import NY, calendar, last_completed, session

DATABASE = "iv-robinhood-daily.sqlite3"
SOURCE = "Robinhood ATM call near-close v1"


def connect(state_dir):
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(state_dir) / DATABASE, timeout=20)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS samples(
            symbol TEXT, day TEXT, iv REAL, source TEXT, evidence TEXT,
            PRIMARY KEY(symbol,day));
        CREATE TABLE IF NOT EXISTS attempts(
            symbol TEXT, day TEXT, attempted_at TEXT, error TEXT,
            PRIMARY KEY(symbol,day));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS switches(
            symbol TEXT PRIMARY KEY, switched_at TEXT, evidence TEXT);
    """)
    return db


class IVValidationError(ValueError):
    """Safe, locally generated validation reason; contains no broker payload."""


def stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise IVValidationError("Missing timezone")
    return result


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise IVValidationError("Invalid positive value")
    return result


def validate(sample):
    day = date.fromisoformat(sample["day"])
    close = stamp(session(day)["close"])
    option_time, spot_time = stamp(sample["quote_at"]), stamp(sample["spot_at"])
    received = stamp(sample["received_at"])
    if sample["source"] != SOURCE or not sample["instrument_id"]:
        raise IVValidationError("Wrong IV definition or missing contract")
    if not close - timedelta(minutes=15) <= option_time <= close:
        raise IVValidationError("Option quote outside closing window")
    if not close - timedelta(minutes=15) <= spot_time <= close:
        raise IVValidationError("Underlying quote outside closing window")
    if abs((option_time - spot_time).total_seconds()) > 120:
        raise IVValidationError("Underlying and option timestamps differ")
    if not option_time <= received <= close + timedelta(minutes=30):
        raise IVValidationError("Quote timestamp inconsistent with receipt")
    if received <= close and (received - option_time).total_seconds() > 120:
        raise IVValidationError("Stale intraday option quote")
    if not 20 <= (date.fromisoformat(sample["expiry"]) - day).days <= 45:
        raise IVValidationError("Expiry outside methodology")
    iv, bid, ask = positive(sample["iv"]), positive(sample["bid"]), positive(sample["ask"])
    spot, strike = positive(sample["spot"]), positive(sample["strike"])
    if ask < bid or (ask - bid) / ((ask + bid) / 2) > 0.20:
        raise IVValidationError("Crossed or excessively wide option market")
    if abs(strike - spot) / spot > 0.05 or iv > 10:
        raise IVValidationError("Invalid ATM distance or implausible IV")
    return sample


def capture(api, symbol, day, clock):
    chain = api.option_chains(symbol)
    expiries = sorted(date.fromisoformat(d) for d in chain.get("expiration_dates", []))
    expiries = [d for d in expiries if 20 <= (d - day).days <= 45]
    if not expiries:
        raise IVValidationError("No eligible expiry")
    expiry = min(expiries, key=lambda d: abs((d - day).days - 30))
    instruments = api.option_instruments(symbol, expiry, "call")
    eligible = [
        r
        for r in instruments
        if r.get("type") == "call"
        and r.get("expiration_date") == str(expiry)
        and r.get("chain_symbol") == symbol
        and float(r.get("trade_value_multiplier", 100)) == 100
        and r.get("underlying_type", "equity") == "equity"
        and not r.get("cash_component")
        and r.get("state") == "active"
        and r.get("tradability") == "tradable"
    ]
    if not eligible:
        raise IVValidationError("No standard tradable call")
    equity = api.equity_quote(symbol)
    spot = positive(equity.get("last_trade_price"))
    atm = min(
        eligible,
        key=lambda r: (abs(positive(r["strike_price"]) - spot), positive(r["strike_price"])),
    )
    quotes = api.option_quotes([atm["id"]])
    matched = [
        q
        for q in quotes
        if str(q.get("instrument_id", q.get("instrument", q.get("id", q.get("option_id", "")))))
        .rstrip("/")
        .rsplit("/", 1)[-1]
        == atm["id"]
    ]
    if len(matched) != 1:
        raise IVValidationError("Missing or mismatched option quote")
    quote = matched[0]
    return validate(
        dict(
            symbol=symbol,
            day=str(day),
            source=SOURCE,
            instrument_id=atm["id"],
            expiry=str(expiry),
            strike=positive(atm["strike_price"]),
            spot=spot,
            spot_at=equity.get("venue_last_trade_time", equity.get("updated_at")),
            quote_at=quote.get("updated_at"),
            received_at=clock().isoformat(),
            iv=positive(quote.get("implied_volatility")),
            bid=positive(quote.get("bid_price")),
            ask=positive(quote.get("ask_price")),
        )
    )


def save_sample(state_dir, sample):
    validate(sample)
    with closing(connect(state_dir)) as db, db:
        # First validated snapshot wins. Retries and restarts cannot rewrite the series.
        db.execute(
            "INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?)",
            (sample["symbol"], sample["day"], sample["iv"], SOURCE, json.dumps(sample)),
        )


def robinhood_rank(state_dir, symbol, now=None):
    now = now or datetime.now(UTC)
    completed = last_completed(now)
    cal = calendar()
    start = cal.session_offset(str(completed), -251)
    expected = {str(d.date()) for d in cal.sessions_in_range(start, str(completed))}
    valid = []
    with closing(connect(state_dir)) as db:
        rows = db.execute(
            "SELECT day,iv,source,evidence FROM samples WHERE symbol=? "
            "AND day>=? AND day<=? ORDER BY day",
            (symbol, str(start.date()), str(completed)),
        ).fetchall()
    invalid = 0
    for day, iv, source, evidence in rows:
        try:
            sample = validate(json.loads(evidence))
            if (sample["symbol"], sample["day"], sample["iv"], sample["source"]) != (
                symbol,
                day,
                iv,
                source,
            ):
                raise IVValidationError("Evidence mismatch")
            valid.append((day, iv))
        except (ValueError, TypeError, KeyError, OverflowError):
            invalid += 1
    result = dict(
        source=SOURCE,
        rank=None,
        days=len(valid),
        latest=valid[-1][0] if valid else None,
        missing_sessions=sorted(expected - {r[0] for r in valid}),
        required_through=str(completed),
        reason="Building Robinhood history: 200 required",
    )
    if invalid:
        result["reason"] = "Invalid Robinhood observation evidence; review required"
    elif result["latest"] != str(completed):
        result["reason"] = "Latest completed Robinhood session missing"
    elif len(valid) >= 200:
        values = [v for _, v in valid]
        lo, hi = min(values), max(values)
        if hi == lo:
            result["reason"] = "Flat Robinhood history cannot establish rank"
        else:
            result.update(
                rank=(values[-1] - lo) / (hi - lo),
                reason="Validated Robinhood completed-day IV rank available",
            )
    return result


def switched_symbols(state_dir):
    with closing(connect(state_dir)) as db:
        return {r[0] for r in db.execute("SELECT symbol FROM switches")}


def select_rank(state_dir, symbol, now=None, auto=False):
    if auto and symbol in switched_symbols(state_dir):
        return robinhood_rank(state_dir, symbol, now)
    return paper_iv_rank(state_dir, symbol, now)


def switch_ready(state_dir, symbols, now):
    switched = switched_symbols(state_dir)
    for symbol in symbols:
        if symbol in switched:
            continue
        details = robinhood_rank(state_dir, symbol, now)
        if details["rank"] is None:
            continue
        with closing(connect(state_dir)) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO switches VALUES (?,?,?)",
                (symbol, now.isoformat(), json.dumps(details)),
            )
        from .runtime import RuntimeStore

        RuntimeStore(state_dir).event(
            "iv_source_switch",
            {
                **details,
                "symbol": symbol,
                "message": f"{symbol}: historical IV source switched permanently to Robinhood",
            },
        )


def collect_once(config, caller, clock=lambda: datetime.now(UTC)):
    if not config.robinhood_iv_daily_collection:
        raise IVValidationError("Daily collector is disabled")
    now = clock()
    cal = calendar()
    today = now.astimezone(NY).date()
    target = cal.date_to_session(str(today), direction="next").date()
    first = target
    if now > stamp(session(target)["close"]) + timedelta(minutes=30):
        first = cal.next_session(str(target)).date()
    with closing(connect(config.state_dir)) as db, db:
        db.execute("INSERT OR IGNORE INTO settings VALUES ('first_session',?)", (str(first),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('heartbeat',?)", (now.isoformat(),))
    close = stamp(session(target)["close"])
    if close - timedelta(minutes=15) <= now <= close + timedelta(minutes=30):
        with closing(connect(config.state_dir)) as db:
            done = {
                r[0] for r in db.execute("SELECT symbol FROM samples WHERE day=?", (str(target),))
            }
            attempts = dict(
                db.execute("SELECT symbol,attempted_at FROM attempts WHERE day=?", (str(target),))
            )
        pending = sorted(
            set(config.universe.symbols) - done, key=lambda s: (attempts.get(s, ""), s)
        )
        api = RobinhoodMcp(caller)
        deadline = time.monotonic() + 90
        for symbol in pending:
            if time.monotonic() >= deadline or clock() > close + timedelta(minutes=30):
                break
            # Persist before network I/O so a killed/hung attempt cannot starve other stocks.
            with closing(connect(config.state_dir)) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)",
                    (symbol, str(target), clock().isoformat(), "Collection in progress"),
                )
            error = ""
            try:
                save_sample(config.state_dir, capture(api, symbol, target, clock))
            except IVValidationError as exc:
                error = f"Reading rejected: {exc}"
            except Exception as exc:
                # No raw API response, account information or token in errors.
                error = f"Reading rejected or unavailable ({type(exc).__name__})"
            with closing(connect(config.state_dir)) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO attempts VALUES (?,?,?,?)",
                    (symbol, str(target), clock().isoformat(), error),
                )
    if config.paper_iv_auto_switch:
        switch_ready(config.state_dir, config.universe.symbols, clock())
    result = collection_status(config.state_dir, config.universe.symbols, clock())
    alerts = Alerts(config.state_dir)
    missing = [s for s in result["symbols"] if s["missed_since_start"]]
    if missing:
        alerts.set(
            "iv_collection_missing",
            "Robinhood IV collection missed sessions: "
            + ", ".join(f"{s['symbol']} ({s['missed_since_start']})" for s in missing),
        )
    else:
        alerts.clear("iv_collection_missing")
    alerts.clear("iv_collector")
    return result


def collection_status(state_dir, symbols, now=None):
    now = now or datetime.now(UTC)
    with closing(connect(state_dir)) as db:
        settings = dict(db.execute("SELECT key,value FROM settings"))
        switched = {r[0]: r[1] for r in db.execute("SELECT symbol,switched_at FROM switches")}
        attempts = {
            r[0]: dict(date=r[1], at=r[2], error=r[3])
            for r in db.execute("SELECT symbol,day,attempted_at,error FROM attempts ORDER BY day")
        }
    heartbeat = settings.get("heartbeat")
    healthy = bool(heartbeat and 0 <= (now - stamp(heartbeat)).total_seconds() < 600)
    target = calendar().date_to_session(str(now.astimezone(NY).date()), direction="next")
    if now > stamp(session(target.date())["close"]) + timedelta(minutes=30):
        target = calendar().next_session(target)
    close = stamp(session(target.date())["close"])
    result = dict(
        healthy=healthy,
        heartbeat=heartbeat,
        first_session=settings.get("first_session"),
        next_window=(close - timedelta(minutes=15)).isoformat(),
        symbols=[],
    )
    # Grace period allows retained closing snapshots to arrive before declaring a miss.
    completed = last_completed(now - timedelta(minutes=30))
    for symbol in symbols:
        rank = robinhood_rank(state_dir, symbol, now)
        missing = [
            d
            for d in rank["missing_sessions"]
            if settings.get("first_session") and settings["first_session"] <= d <= str(completed)
        ]
        result["symbols"].append(
            dict(
                symbol=symbol,
                **rank,
                switched_at=switched.get(symbol),
                missed_since_start=len(missing),
                last_attempt=attempts.get(symbol),
            )
        )
    return result
