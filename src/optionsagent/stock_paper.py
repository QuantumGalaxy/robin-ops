"""Isolated, long-only share paper account. Only read-only Robinhood calls are used."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .mcp.client import HttpToolCaller
from .mcp.robinhood import rows
from .reference_feed import session
from .runtime import single_writer

NY = ZoneInfo("America/New_York")
# Versioned research universe, not a claim to be a live market-cap ranking.
SYMBOLS = (
    "NVDA AAPL GOOGL MSFT AMZN AVGO META TSLA MU BRK.B LLY JPM WMT AMD V JNJ "
    "XOM MA INTC ORCL ABBV BAC CSCO PLTR CVX COST LRCX KO CAT MRK AMAT UNH GE "
    "MS PG DELL NFLX HD GS PM WFC PANW RTX SNDK GEV ANET KLAC AMGN TXN ADBE"
).split()
RULES = dict(
    version="stocks-orb-paper-v1",
    mode="paper",
    starting_cash=25000,
    max_position=5000,
    risk_per_trade=25,
    target_net=50,
    daily_loss=100,
    max_positions=3,
    max_entries=10,
    max_symbol_entries=3,
    cooldown_minutes=15,
    volume_multiple=1.2,
    max_spread=0.002,
    slippage=0.0001,
    fee_per_side=0.01,
    quote_age_seconds=30,
    poll_seconds=15,
    symbols=SYMBOLS,
)


def stamp(value):
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("Timezone required")
    return d


def number(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise ValueError("Invalid market value")
    return n


def quote(raw, now):
    """Both sides must be current; a last-trade timestamp is insufficient."""
    if raw.get("state") != "active" or not raw.get("has_traded"):
        raise ValueError("Inactive or untraded stock")
    bid, ask = number(raw["bid_price"]), number(raw["ask_price"])
    for side in ("bid", "ask"):
        age = (now - stamp(raw[f"venue_{side}_time"])).total_seconds()
        if not -5 <= age <= RULES["quote_age_seconds"]:
            raise ValueError("Stale stock quote")
    if ask < bid:
        raise ValueError("Crossed stock quote")
    return dict(
        bid=bid,
        ask=ask,
        at=now.isoformat(),
        bid_at=raw["venue_bid_time"],
        ask_at=raw["venue_ask_time"],
    )


def fresh_quote(q, now):
    if not q:
        return False
    try:
        return number(q["ask"]) >= number(q["bid"]) and all(
            -5 <= (now - stamp(q[k])).total_seconds() <= RULES["quote_age_seconds"]
            for k in ("bid_at", "ask_at")
        )
    except (ValueError, TypeError, KeyError):
        return False


def candles(raw, now, opening, closing):
    """Reject invalid observations; never interpolate the missing opening range."""
    valid = {}
    duplicates = set()
    for r in raw:
        try:
            at = stamp(r["begins_at"])
            if (
                r.get("interpolated")
                or r.get("session", "reg") != "reg"
                or at < opening
                or at + timedelta(minutes=5) > min(now, closing)
                or (at - opening).total_seconds() % 300
            ):
                continue
            o, h, low, c = [number(r[k + "_price"]) for k in ("open", "high", "low", "close")]
            volume = number(r["volume"])
            if not low <= min(o, c) <= max(o, c) <= h:
                continue
            if at in valid:
                duplicates.add(at)
            valid[at] = dict(at=at.isoformat(), open=o, high=h, low=low, close=c, volume=volume)
        except (ValueError, TypeError, KeyError):
            continue
    return [valid[k] for k in sorted(valid) if k not in duplicates]


def signal(bars, spy, now, opening):
    """One confirmed fresh crossing; latest candle must be the last completed interval."""
    by_time = {stamp(b["at"]): b for b in bars}
    first = [by_time.get(opening + timedelta(minutes=5 * i)) for i in range(3)]
    if not all(first):
        return None, "Opening range incomplete"
    last_start = opening + timedelta(seconds=((now - opening).total_seconds() // 300 - 1) * 300)
    latest = by_time.get(last_start)
    previous = by_time.get(last_start - timedelta(minutes=5))
    if not latest or not previous or last_start < opening + timedelta(minutes=15):
        return None, "Waiting for current completed breakout candle"
    high, low = max(b["high"] for b in first), min(b["low"] for b in first)
    if not previous["close"] <= high < latest["close"]:
        return None, "No fresh opening-range breakout"
    ratio = latest["volume"] / (sum(b["volume"] for b in first) / 3)
    if ratio < RULES["volume_multiple"]:
        return None, "Breakout volume below opening-range average × 1.2"
    benchmark = {stamp(b["at"]): b for b in spy}
    a, b, c = (benchmark.get(t) for t in (opening, last_start - timedelta(minutes=5), last_start))
    if not a or not b or not c or not (c["close"] > a["open"] and c["close"] > b["close"]):
        return None, "SPY confirmation absent or incomplete"
    return dict(
        bar=latest["at"],
        stop=low,
        range_high=high,
        close=latest["close"],
        volume_ratio=ratio,
        spy_close=c["close"],
    ), ""


class StockAccount:
    def __init__(self, root):
        self.folder = Path(root) / "stocks"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "stocks.sqlite3"
        self.events = []
        fingerprint = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, body TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit "
                "(id INTEGER PRIMARY KEY, at TEXT, kind TEXT, body TEXT)"
            )
            row = db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()
        self.s = (
            json.loads(row[0])
            if row
            else dict(
                fingerprint=fingerprint,
                rules=RULES,
                cash=25000.0,
                positions={},
                trades=[],
                realized_pnl=0.0,
                day=None,
                day_equity=25000.0,
                halted=False,
                entries=0,
                symbol_entries={},
                cooldown={},
                seen={},
                candidates={},
                status="Starting",
                saved_at=None,
                last_scan=None,
            )
        )
        if self.s["fingerprint"] != fingerprint:
            raise ValueError("Stock paper rules changed; explicit state migration required")

    def connect(self):
        # Caller owns the transaction; always close through closing().
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def emit(self, kind, now, **detail):
        self.events.append((now.isoformat(), kind, json.dumps(detail)))

    def equity(self):
        return self.s["cash"] + sum(p["quantity"] * p["mark"] for p in self.s["positions"].values())

    def save(self, now):
        self.s["saved_at"] = now.isoformat()
        self.s["equity"] = self.equity()
        # Fills, cash, consumed signals and audit events commit together.
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO checkpoint VALUES (1, ?)", (json.dumps(self.s),))
            db.executemany("INSERT INTO audit(at,kind,body) VALUES (?,?,?)", self.events)
        self.events.clear()

    def monitor(self, quotes, now, opening, closing):
        s = self.s
        day = now.astimezone(NY).date().isoformat()
        for symbol, p in s["positions"].items():
            if fresh_quote(quotes.get(symbol), now):
                p["mark"] = quotes[symbol]["bid"] * (1 - RULES["slippage"])
                p["mark_at"] = quotes[symbol]["at"]
        if s["day"] != day:
            s.update(
                day=day,
                day_equity=self.equity(),
                halted=False,
                entries=0,
                symbol_entries={},
                seen={},
                candidates={},
            )
        if self.equity() - s["day_equity"] <= -RULES["daily_loss"]:
            s["halted"] = True
        if not opening <= now < closing:
            return
        for symbol, p in list(s["positions"].items()):
            q = quotes.get(symbol)
            if not fresh_quote(q, now):
                self.emit("exit_wait", now, symbol=symbol, reason="No fresh executable quote")
                continue
            price = q["bid"] * (1 - RULES["slippage"])
            net = p["quantity"] * (price - p["entry"]) - 2 * RULES["fee_per_side"]
            reason = None
            if stamp(p["opened_at"]).astimezone(NY).date().isoformat() != day:
                reason = "Overnight recovery"
            elif s["halted"]:
                reason = "Daily loss halt"
            elif now >= closing - timedelta(minutes=5):
                reason = "End of session"
            elif q["bid"] <= p["stop"] or net <= -RULES["risk_per_trade"]:
                reason = "Protective loss exit"
            elif net >= RULES["target_net"]:
                reason = "$50 net target"
            if reason:
                s["cash"] += p["quantity"] * price - RULES["fee_per_side"]
                s["realized_pnl"] += net
                trade = dict(
                    p,
                    symbol=symbol,
                    exit_price=price,
                    pnl=net,
                    reason=reason,
                    closed_at=now.isoformat(),
                    fees=2 * RULES["fee_per_side"],
                )
                s["trades"].append(trade)
                del s["positions"][symbol]
                s["cooldown"][symbol] = (now + timedelta(minutes=15)).isoformat()
                self.emit("sell_fill", now, **trade, quote=q)

    def consider(self, symbol, sig, q, now, opening, closing, paused=False):
        s = self.s
        reason = None
        if not opening + timedelta(minutes=20) <= now < closing - timedelta(minutes=30):
            reason = "Outside entry window"
        elif paused:
            reason = "Entries paused"
        elif s["halted"]:
            reason = "Daily loss limit reached"
        elif any(
            stamp(p["opened_at"]).astimezone(NY).date() != now.astimezone(NY).date()
            for p in s["positions"].values()
        ):
            reason = "Unresolved overnight position"
        elif any((now - stamp(p["mark_at"])).total_seconds() > 30 for p in s["positions"].values()):
            reason = "Held position marks stale"
        elif symbol in s["positions"] or len(s["positions"]) >= RULES["max_positions"]:
            reason = "Position limit"
        elif s["entries"] >= RULES["max_entries"] or s["symbol_entries"].get(symbol, 0) >= 3:
            reason = "Daily entry limit"
        elif symbol in s["cooldown"] and now < stamp(s["cooldown"][symbol]):
            reason = "Re-entry cooldown"
        elif not sig:
            reason = "No qualifying signal"
        elif s["seen"].get(symbol) == sig["bar"]:
            reason = "Signal already consumed"
        elif not fresh_quote(q, now):
            reason = "No fresh executable quote"
        elif (q["ask"] - q["bid"]) / q["ask"] > RULES["max_spread"]:
            reason = "Spread too wide"
        elif q["ask"] < 5 or not sig["range_high"] < q["ask"] <= sig["close"] * 1.01:
            reason = "Price no longer near confirmed breakout"
        if reason:
            return reason
        entry = q["ask"] * (1 + RULES["slippage"])
        stop_fill = sig["stop"] * (1 - RULES["slippage"])
        per_share = entry - stop_fill
        if per_share <= 0:
            return "Invalid stop distance"
        qty = min(
            math.floor((RULES["risk_per_trade"] - 2 * RULES["fee_per_side"]) / per_share),
            math.floor((min(RULES["max_position"], s["cash"]) - RULES["fee_per_side"]) / entry),
        )
        if qty < 1:
            return "Risk or cash budget too small for one share"
        p = dict(
            symbol=symbol,
            quantity=qty,
            entry=entry,
            stop=sig["stop"],
            mark=q["bid"] * (1 - RULES["slippage"]),
            mark_at=now.isoformat(),
            target_price=entry + (RULES["target_net"] + 2 * RULES["fee_per_side"]) / qty,
            opened_at=now.isoformat(),
            planned_risk=qty * per_share + 0.02,
            signal=sig,
        )
        s["cash"] -= qty * entry + RULES["fee_per_side"]
        s["positions"][symbol] = p
        s["entries"] += 1
        s["symbol_entries"][symbol] = s["symbol_entries"].get(symbol, 0) + 1
        s["seen"][symbol] = sig["bar"]
        self.emit("buy_fill", now, **p, quote=q)
        return "BUY filled in stock paper account"


def status(root):
    path = Path(root) / "stocks" / "stocks.sqlite3"
    if not path.exists():
        return dict(enabled=False)
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        row = db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()
    if not row:
        return dict(enabled=False)
    s = json.loads(row[0])
    s["enabled"] = True
    s["healthy"] = bool(
        s.get("saved_at") and (datetime.now(UTC) - stamp(s["saved_at"])).total_seconds() < 120
    )
    s["trades"] = s["trades"][-100:]
    return s


def fetch_quotes(caller, symbols):
    output = {}
    if symbols:
        for row in rows(caller.call("get_equity_quotes", {"symbols": list(symbols)})):
            raw = row.get("quote", row)
            try:
                if raw["symbol"] in symbols:
                    output[raw["symbol"]] = quote(raw, datetime.now(UTC))
            except (ValueError, TypeError, KeyError):
                continue
    return output


def run(root, kill_file):
    with single_writer(Path(root) / "stocks"):
        account = StockAccount(root)
        caller = HttpToolCaller(timeout=8)
        index = 0
        last_scan = 0.0
        while True:
            tick = time.monotonic()
            now = datetime.now(UTC)
            hours = session(now.astimezone(NY).date())
            try:
                if hours:
                    opening, closing = stamp(hours["open"]), stamp(hours["close"])
                if not hours or not opening <= now < closing:
                    account.s["status"] = (
                        "Market closed · unresolved stock holdings"
                        if account.s["positions"]
                        else "Market closed · waiting for next session"
                    )
                else:
                    # Always attempt exits before scanning more entry candidates.
                    held = fetch_quotes(caller, account.s["positions"])
                    now = datetime.now(UTC)
                    account.monitor(held, now, opening, closing)
                    account.save(now)
                    account.s["status"] = (
                        "Entries paused"
                        if Path(kill_file).exists()
                        else "Daily loss halt"
                        if account.s["halted"]
                        else "Observing stock market"
                    )
                    if account.s["positions"].keys() - held.keys():
                        account.s["status"] = "Some holdings lack fresh quotes · exits waiting"
                    # One batch per cycle bounds how long scans delay the next exit check.
                    if (
                        time.monotonic() - last_scan >= 15
                        and opening + timedelta(minutes=20) <= now < closing - timedelta(minutes=30)
                        and not Path(kill_file).exists()
                        and not account.s["halted"]
                        and not (account.s["positions"].keys() - held.keys())
                    ):
                        batch = SYMBOLS[index : index + 9]
                        index = (index + 9) if index + 9 < len(SYMBOLS) else 0
                        payload = caller.call(
                            "get_equity_historicals",
                            dict(
                                symbols=batch + ["SPY"],
                                start_time=opening.isoformat(),
                                end_time=now.isoformat(),
                                interval="5minute",
                                bounds="regular",
                                adjustment_type="none",
                            ),
                        )
                        now = datetime.now(UTC)
                        bars = {
                            r["symbol"]: candles(r.get("bars", []), now, opening, closing)
                            for r in rows(payload)
                            if "symbol" in r
                        }
                        signals = {
                            s: signal(bars.get(s, []), bars.get("SPY", []), now, opening)
                            for s in batch
                        }
                        qualified = [s for s, (sig, _) in signals.items() if sig]
                        fresh = fetch_quotes(caller, qualified)
                        now = datetime.now(UTC)
                        # Re-evaluate with time after network I/O: don't enter on a candle
                        # that ceased being the latest completed interval during the call.
                        for symbol in batch:
                            sig, reason = signal(
                                bars.get(symbol, []), bars.get("SPY", []), now, opening
                            )
                            if sig:
                                reason = account.consider(
                                    symbol,
                                    sig,
                                    fresh.get(symbol),
                                    now,
                                    opening,
                                    closing,
                                    Path(kill_file).exists(),
                                )
                            account.s["candidates"][symbol] = dict(
                                reason=reason, at=now.isoformat()
                            )
                            account.emit(
                                "stock_scan", now, symbol=symbol, reason=reason, signal=sig
                            )
                        account.s["last_scan"] = now.isoformat()
                        last_scan = time.monotonic()
                    account.emit(
                        "stock_equity",
                        now,
                        equity=account.equity(),
                        held=list(account.s["positions"]),
                        status=account.s["status"],
                    )
            except (sqlite3.Error, OSError):
                raise
            except Exception as exc:
                # Do not log raw broker responses or tokens. Unhandled DB failures exit below.
                account.s["status"] = f"Data unavailable · {type(exc).__name__}; new fills deferred"
                account.emit("stock_error", datetime.now(UTC), reason=account.s["status"])
            account.save(datetime.now(UTC))
            time.sleep(max(1, RULES["poll_seconds"] - (time.monotonic() - tick)))


def report(root, start, end):
    path = Path(root) / "stocks" / "stocks.sqlite3"
    lines = ["## Regular stock trading · separate paper account", ""]
    if not path.exists():
        return None, lines + ["No saved stock account available.", ""]
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        events = [
            dict(at=at, kind=kind, detail=json.loads(body))
            for at, kind, body in db.execute(
                "SELECT at,kind,body FROM audit WHERE at>=? AND at<? ORDER BY id",
                (start.isoformat(), end.isoformat()),
            )
        ]
    from collections import Counter

    reasons = Counter(e["detail"]["reason"] for e in events if e["kind"] == "stock_scan")
    closed = [e["detail"] for e in events if e["kind"] == "sell_fill"]
    lines += [
        f"- Closed stock trades: {len(closed)}; net ${sum(t['pnl'] for t in closed):,.2f}",
        "- Whole shares; simulated fills include spread, 1 bp adverse slippage per side "
        "and $0.01 modeled fee per side. These are estimates, not actual brokerage charges.",
        "",
        "### Stock scans",
    ]
    lines += [f"- {reason}: {count}" for reason, count in reasons.most_common()] or [
        "- None recorded."
    ]
    lines += ["", "### Stock fills and execution problems"]
    timeline = []
    for e in events:
        d = e["detail"]
        at = stamp(e["at"]).astimezone(NY).strftime("%H:%M:%S")
        if e["kind"] == "buy_fill":
            timeline.append(
                f"- {at} BUY filled: {d['symbol']} ×{d['quantity']} at ${d['entry']:.4f}; "
                f"stop ${d['stop']:.4f}; planned risk ${d['planned_risk']:.2f}."
            )
        elif e["kind"] == "sell_fill":
            timeline.append(
                f"- {at} SELL filled: {d['symbol']} ×{d['quantity']} at "
                f"${d['exit_price']:.4f}; net ${d['pnl']:.2f}; {d['reason']}."
            )
        elif e["kind"] in ("stock_error", "exit_wait"):
            timeline.append(f"- {at} {d.get('symbol', '')}: {d['reason']}")
    lines += (timeline or ["- None recorded."]) + [""]
    return dict(
        name="Regular stock paper account",
        events=events,
        closed_trades=closed,
        skipped=dict(reasons),
    ), lines


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--kill-file", required=True)
    args = parser.parse_args()
    run(args.state_dir, args.kill_file)
