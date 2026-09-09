"""Build timestamped reference snapshots from Robinhood and a daily ATM IV archive."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from .marketdata.reference import ReferenceSnapshot
from .mcp.robinhood import RobinhoodMcp, as_date, rows

NY = ZoneInfo("America/New_York")


def calendar():
    return xcals.get_calendar("XNYS")


def session(day):
    cal = calendar()
    if not cal.is_session(day.isoformat()):
        return None
    return {
        "open": cal.session_open(day.isoformat()).isoformat(),
        "close": cal.session_close(day.isoformat()).isoformat(),
    }


def completed_intraday_close(caller, symbol, day, now):
    """Recover a session-end trade close only from a complete regular-session series.

    This is not an official auction close. Store its provenance and replace it
    with the split-adjusted vendor daily bar when that becomes available.
    """
    from .stock_paper import candles, stamp

    hours = session(day)
    opening, closing = stamp(hours["open"]), stamp(hours["close"])
    if now < closing:
        raise ValueError("Session not completed")
    raw = caller.call(
        "get_equity_historicals",
        dict(
            symbols=[symbol],
            start_time=opening.isoformat(),
            end_time=closing.isoformat(),
            interval="5minute",
            bounds="regular",
            adjustment_type="split",
        ),
    )
    matches = [r for r in rows(raw) if r.get("symbol") == symbol]
    if len(matches) != 1:
        raise ValueError("Missing intraday series")
    valid = candles(matches[0].get("bars", []), now, opening, closing)
    expected = int((closing - opening).total_seconds() // 300)
    if len(valid) != expected:
        raise ValueError("Incomplete intraday session")
    return valid[-1]["close"]


def last_completed(now):
    cal = calendar()
    d = now.astimezone(NY).date()
    label = cal.date_to_session(d.isoformat(), direction="previous")
    if cal.session_close(label).to_pydatetime() >= now:
        label = cal.previous_session(label)
    return label.date()


class IVArchive:
    """One completed-session ATM ~30D IV observation per symbol, with provenance."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS iv(symbol TEXT, day TEXT, iv REAL, "
                "source TEXT, PRIMARY KEY(symbol,day))"
            )

    def add(self, symbol, day, value, source, now=None):
        now = now or datetime.now(UTC)
        if day > last_completed(now) or not session(day):
            raise ValueError("IV observations must describe completed trading sessions")
        if not math.isfinite(value) or value <= 0 or not source.strip():
            raise ValueError("Positive IV and nonempty source required")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO iv VALUES (?,?,?,?)",
                (symbol, day.isoformat(), value, source),
            )

    def rank(self, symbol, as_of, current_iv=None):
        cal = calendar()
        end = cal.date_to_session(as_of.isoformat(), direction="previous")
        start = cal.session_offset(end, -251).date().isoformat()
        with closing(sqlite3.connect(self.path)) as db, db:
            values = db.execute(
                "SELECT day,iv,source FROM iv WHERE symbol=? AND day>=? AND day<=? "
                "ORDER BY day DESC LIMIT 252",
                (symbol, start, as_of.isoformat()),
            ).fetchall()
        if len(values) < 200 or date.fromisoformat(values[0][0]) != as_of:
            return None, len(values)
        if len({x[2] for x in values}) != 1:
            return None, len(values)
        v = [x[1] for x in values]
        if min(v) == max(v):
            return None, len(v)
        current = v[0] if current_iv is None else current_iv
        return max(0.0, min(1.0, (current - min(v)) / (max(v) - min(v)))), len(v)

    def import_csv(self, path, now=None):
        records = list(csv.DictReader(Path(path).open()))
        # Validate all rows before changing the archive.
        prepared = []
        for row in records:
            d = date.fromisoformat(row["date"])
            v = float(row["iv"])
            source = row["source"].strip()
            symbol = row["symbol"].upper().strip()
            if (
                not symbol.isalnum()
                or not math.isfinite(v)
                or v <= 0
                or not source
                or not session(d)
                or d > last_completed(now or datetime.now(UTC))
            ):
                raise ValueError("Invalid IV archive row")
            prepared.append((symbol, d.isoformat(), v, source))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("INSERT OR REPLACE INTO iv VALUES (?,?,?,?)", prepared)
        return len(prepared)


def refresh_reference(
    caller, symbols, path, iv_path, now=None, require_iv_rank=True, collect_iv=True
):
    now = now or datetime.now(UTC)
    completed = last_completed(now)
    api = RobinhoodMcp(caller)
    archive = IVArchive(iv_path)
    result = {
        "as_of": now.isoformat(),
        "source": "Robinhood MCP daily bars/earnings; XNYS calendar; sourced daily ATM IV archive",
        "session": session(now.astimezone(NY).date()),
        "symbols": {},
    }
    errors = []
    previous = {}
    try:
        previous = ReferenceSnapshot.model_validate_json(Path(path).read_text()).model_dump(
            mode="json"
        )["symbols"]
    except (OSError, ValueError):
        pass
    for symbol in symbols:
        entry = {
            "daily_closes": [],
            "iv_rank": None,
            "iv_history_days": 0,
            "earnings_checked": False,
            "earnings": None,
        }
        # Preserve last verified bars with their original date if publication is delayed.
        # Do not carry forward earnings verification or claim the bars are fresh.
        old = previous.get(symbol, {})
        if old.get("daily_closes"):
            entry.update(
                daily_closes=old["daily_closes"],
                daily_closes_as_of=old["daily_closes_as_of"],
                daily_closes_note=old.get("daily_closes_note"),
            )
        try:
            raw = caller.call(
                "get_equity_historicals",
                {
                    "symbols": [symbol],
                    "start_time": (now - timedelta(days=180)).isoformat(),
                    "end_time": now.isoformat(),
                    "interval": "day",
                    "bounds": "regular",
                    "adjustment_type": "split",
                },
            )
            series = next((r for r in rows(raw) if r.get("symbol") == symbol), {})
            bars = {}
            for bar in series.get("bars") or []:
                d = (
                    datetime.fromisoformat(bar["begins_at"].replace("Z", "+00:00"))
                    .astimezone(NY)
                    .date()
                )
                # Some interday feeds label bars at midnight UTC; their date is the session date.
                stamp = datetime.fromisoformat(bar["begins_at"].replace("Z", "+00:00"))
                if stamp.hour == 0:
                    d = stamp.date()
                v = float(bar["close_price"])
                if (
                    d <= completed
                    and session(d)
                    and math.isfinite(v)
                    and v > 0
                    and not bar.get("interpolated", False)
                ):
                    bars[d] = v
            recovered = False
            if bars and max(bars) != completed:
                try:
                    bars[completed] = completed_intraday_close(caller, symbol, completed, now)
                    recovered = True
                except Exception:
                    pass  # Keep the real daily history; do not fabricate an incomplete session.
            if not bars:
                raise ValueError("latest completed daily bar unavailable")
            entry.update(
                daily_closes=[bars[d] for d in sorted(bars)][-90:],
                daily_closes_as_of=max(bars).isoformat(),
                daily_closes_note=(
                    f"{completed}: complete Robinhood 5-minute session-end close; "
                    "official daily bar pending"
                    if recovered
                    else None
                ),
            )
            if max(bars) != completed:
                errors.append(
                    f"{symbol}: daily bar pending for {completed}; "
                    f"last verified session {max(bars)} retained"
                )
            earnings = caller.call("get_earnings_results", {"symbol": symbol})
            data = earnings.get("data", {})
            future = [as_date((r.get("report") or {}).get("date")) for r in rows(earnings)]
            future = [d for d in future if d and d >= now.astimezone(NY).date()]
            if future and symbol not in (data.get("not_found") or []):
                entry.update(earnings_checked=True, earnings=min(future).isoformat())
            elif symbol in {"SPY", "QQQ"}:
                entry.update(earnings_checked=True, earnings=None)
            else:
                errors.append(f"{symbol}: next earnings date unavailable")
            with closing(sqlite3.connect(archive.path)) as db:
                sources = [
                    r[0]
                    for r in db.execute("SELECT DISTINCT source FROM iv WHERE symbol=?", (symbol,))
                ]
            # Never overwrite a vendor archive with a different IV definition.
            external = any(not source.startswith("Robinhood near-close ATM") for source in sources)
            if not external and collect_iv:
                # Collect a representative completed-session ATM ~30D IV when quote date agrees.
                chain = api.option_chains(symbol)
                expiries = [
                    date.fromisoformat(d)
                    for d in chain.get("expiration_dates", [])
                    if 20 <= (date.fromisoformat(d) - completed).days <= 45
                ]
                q = api.equity_quote(symbol)
                spot = float(q.get("last_trade_price", 0))
                if expiries and spot > 0:
                    exp = min(expiries, key=lambda d: abs((d - completed).days - 30))
                    instruments = api.option_instruments(symbol, exp, "call")
                    eligible = [
                        r
                        for r in instruments
                        if float(r.get("trade_value_multiplier", 100)) == 100
                        and r.get("underlying_type", "equity") == "equity"
                    ]
                    if eligible:
                        atm = min(eligible, key=lambda r: abs(float(r["strike_price"]) - spot))
                        quotes = api.option_quotes([atm["id"]])
                        if quotes:
                            quote = quotes[0]
                            stamp = datetime.fromisoformat(
                                quote["updated_at"].replace("Z", "+00:00")
                            )
                            close = datetime.fromisoformat(session(completed)["close"])
                            if close - timedelta(minutes=15) <= stamp <= close:
                                archive.add(
                                    symbol,
                                    completed,
                                    float(quote["implied_volatility"]),
                                    "Robinhood near-close ATM call, 20-45D nearest 30D",
                                    now,
                                )
            rank, n = archive.rank(symbol, completed)
            entry.update(iv_rank=rank, iv_history_days=n)
            if rank is None and require_iv_rank:
                errors.append(f"{symbol}: daily IV history not ready ({n}/200 minimum)")
        except Exception as exc:
            errors.append(f"{symbol}: reference refresh failed ({type(exc).__name__})")
        result["symbols"][symbol] = entry
    validated = ReferenceSnapshot.model_validate(result).model_dump(mode="json")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(validated, indent=2))
    tmp.replace(target)
    return errors
