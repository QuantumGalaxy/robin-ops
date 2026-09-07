"""Historical EOD option IV downloads. Credentials never appear in logs or files."""

from __future__ import annotations

import csv
import json
import math
import re
import ssl
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi

from .reference_feed import calendar, last_completed

SOURCE = "Alpha Vantage EOD ATM call; expiry 20-45D nearest30; strike nearest unadjusted close v1"
SERVICE = "robin-ops-alpha-vantage"


def key_store():
    from .mcp.oauth import CredentialStore

    return CredentialStore().backend


def save_key(key):
    key = key.strip()
    if not key or not key.isalnum():
        raise ValueError("Invalid API key format")
    key_store().set_password(SERVICE, "api-key", key)


def load_key():
    key = key_store().get_password(SERVICE, "api-key")
    if not key:
        raise ValueError("Run optionsagent alpha-key in your Terminal first")
    return key


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AlphaClient:
    def __init__(self, key, rpm=5):
        if not 1 <= rpm <= 75:
            raise ValueError("Use 1-75 requests per minute within your plan allowance")
        self.key, self.interval, self.last = key, 60 / rpm, 0.0
        self.opener = build_opener(
            NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
        )

    def get(self, function, **params):
        time.sleep(max(0, self.last + self.interval - time.monotonic()))
        self.last = time.monotonic()
        query = urlencode(dict(function=function, apikey=self.key, **params))
        try:
            request = Request("https://www.alphavantage.co/query?" + query)
            with self.opener.open(request, timeout=60) as response:
                payload = json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            raise ValueError("Alpha Vantage request failed; credentials and URL withheld") from None
        if not isinstance(payload, dict):
            raise ValueError("Unexpected Alpha Vantage response")
        if any(k in payload for k in ("Information", "Note", "Error Message")):
            raise ValueError(
                "Provider denied request: check historical-options entitlement/rate limit"
            )
        return payload


def select_iv(payload, symbol, day, spot):
    if payload.get("message") != "success" or not isinstance(payload.get("data"), list):
        raise ValueError("Historical options data unavailable")
    candidates = []
    for row in payload["data"]:
        try:
            expiry = date.fromisoformat(row["expiration"])
            strike = float(row["strike"])
            if row["symbol"] != symbol or row["date"] != day.isoformat() or row["type"] != "call":
                continue
            if not 20 <= (expiry - day).days <= 45 or not math.isfinite(strike) or strike <= 0:
                continue
            # Standard OCC identity excludes adjusted option roots.
            expected = symbol + expiry.strftime("%y%m%d") + "C" + f"{round(strike * 1000):08d}"
            if row.get("contractID") != expected:
                continue
            candidates.append(
                (abs((expiry - day).days - 30), expiry, abs(strike - spot), strike, row)
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not candidates or not math.isfinite(spot) or spot <= 0:
        raise ValueError("No standard near-30D ATM call available")
    row = min(candidates, key=lambda r: r[:4])[-1]
    try:
        iv, bid, ask = (float(row[k]) for k in ("implied_volatility", "bid", "ask"))
        if not all(math.isfinite(v) for v in (iv, bid, ask)) or iv <= 0 or not 0 < bid <= ask:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError("Selected ATM contract has missing/invalid IV or quotes") from None
    return iv


def download(client, symbols, output, days=252, max_requests=10, now=None):
    """Resume from validated daily observations; stop cleanly at the request budget."""
    if not 1 <= days <= 252 or max_requests < 2:
        raise ValueError("Use 1-252 sessions and at least two requests")
    symbols = list(dict.fromkeys(symbols))
    if any(not re.fullmatch(r"[A-Z]{1,6}", s) for s in symbols):
        raise ValueError("Only standard uppercase equity symbols supported")
    completed = last_completed(now or datetime.now(UTC))
    sessions = [
        d.date()
        for d in calendar().sessions_in_range(
            (completed - timedelta(days=400)).isoformat(), completed.isoformat()
        )
    ][-days:]
    target = Path(output)
    records = {}
    if target.exists():
        with target.open() as f:
            for row in csv.DictReader(f):
                if row["source"] != SOURCE:
                    raise ValueError("Output belongs to another IV definition; use a new path")
                value = float(row["iv"])
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("Invalid cached IV")
                records[row["symbol"], row["date"]] = row
    requests = 0
    failures = []
    for symbol in symbols:
        missing = [d for d in reversed(sessions) if (symbol, d.isoformat()) not in records]
        if not missing:
            continue
        if requests + 2 > max_requests:
            break
        bars = client.get("TIME_SERIES_DAILY", symbol=symbol, outputsize="full")
        requests += 1
        daily = bars.get("Time Series (Daily)")
        if not isinstance(daily, dict):
            raise ValueError("Unadjusted daily equity prices unavailable")
        for day in missing:
            if requests >= max_requests:
                break
            raw = client.get("HISTORICAL_OPTIONS", symbol=symbol, date=day.isoformat())
            requests += 1
            try:
                spot = float(daily[day.isoformat()]["4. close"])
                iv = select_iv(raw, symbol, day, spot)
            except (KeyError, TypeError, ValueError):
                failures.append(f"{symbol} {day}: missing/invalid point-in-time data")
                continue
            records[symbol, day.isoformat()] = dict(
                symbol=symbol, date=day.isoformat(), iv=iv, source=SOURCE
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".tmp")
            with temp.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["symbol", "date", "iv", "source"])
                writer.writeheader()
                writer.writerows(records[k] for k in sorted(records))
            temp.replace(target)
    counts = {s: sum((s, d.isoformat()) in records for d in sessions) for s in symbols}
    return dict(
        requests=requests,
        observations=counts,
        failures=failures,
        complete=all(n == days for n in counts.values()),
    )
