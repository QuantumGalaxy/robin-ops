import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from optionsagent.config import Config
from optionsagent.iv_daily import (
    DATABASE,
    SOURCE,
    collect_once,
    collection_status,
    robinhood_rank,
    save_sample,
    select_rank,
    switch_ready,
    switched_symbols,
    validate,
)
from optionsagent.reference_feed import calendar, session


def sample(day="2026-09-08", symbol="AAPL", iv=0.3):
    close = datetime.fromisoformat(session(date.fromisoformat(day))["close"])
    return dict(
        symbol=symbol,
        day=day,
        iv=iv,
        source=SOURCE,
        instrument_id="contract-1",
        expiry=str(date.fromisoformat(day) + timedelta(days=30)),
        spot=100,
        strike=100,
        spot_at=(close - timedelta(minutes=5)).isoformat(),
        quote_at=(close - timedelta(minutes=5)).isoformat(),
        received_at=(close - timedelta(minutes=4, seconds=59)).isoformat(),
        bid=4,
        ask=4.2,
    )


def cfg(path):
    return Config(
        state_dir=str(path),
        data_provider="robinhood_mcp",
        paper_iv_history_experiment=True,
        robinhood_iv_daily_collection=True,
        paper_iv_auto_switch=True,
        universe={"symbols": ["AAPL"]},
    )


def test_calendar_window_early_close_and_stale_rejection():
    row = sample("2026-11-27")
    assert datetime.fromisoformat(row["quote_at"]).hour == 17  # 12:55 EST early close
    validate(row)
    for field, value in [
        ("iv", float("nan")),
        ("bid", 0),
        ("ask", 3),
        ("instrument_id", ""),
        ("quote_at", "2026-11-27T20:55:00+00:00"),
        ("spot_at", "2026-11-27T17:45:00+00:00"),
        ("received_at", "2026-11-27T17:59:59+00:00"),
    ]:
        with pytest.raises(ValueError):
            validate({**row, field: value})


def test_completed_only_idempotent_and_evidence_validation(tmp_path):
    row = sample()
    save_sample(tmp_path, row)
    save_sample(tmp_path, {**row, "iv": 0.9})
    during = datetime(2026, 9, 8, 19, 59, tzinfo=UTC)
    assert robinhood_rank(tmp_path, "AAPL", during)["days"] == 0
    after = datetime(2026, 9, 8, 21, tzinfo=UTC)
    assert robinhood_rank(tmp_path, "AAPL", after)["days"] == 1
    with sqlite3.connect(tmp_path / DATABASE) as db:
        assert db.execute("SELECT iv FROM samples").fetchone()[0] == 0.3
        db.execute("UPDATE samples SET iv=.8")
    assert robinhood_rank(tmp_path, "AAPL", after)["days"] == 0
    assert "Invalid" in robinhood_rank(tmp_path, "AAPL", after)["reason"]


def test_switch_at_200_per_symbol_persists_and_stale_never_falls_back(tmp_path):
    cal = calendar()
    end = "2026-09-04"
    days = cal.sessions_in_range(cal.session_offset(end, -199), end)
    now = datetime(2026, 9, 7, 18, tzinfo=UTC)
    for i, day in enumerate(days[:-1]):
        save_sample(tmp_path, sample(str(day.date()), iv=0.2 + i / 10000))
    switch_ready(tmp_path, ["AAPL", "MA"], now)
    assert not switched_symbols(tmp_path)
    save_sample(tmp_path, sample(end, iv=0.25))
    switch_ready(tmp_path, ["AAPL", "MA"], now)
    assert switched_symbols(tmp_path) == {"AAPL"}
    selected = select_rank(tmp_path, "AAPL", now, auto=True)
    assert selected["source"] == SOURCE and selected["rank"] == 1
    switch_ready(tmp_path, ["AAPL"], now)
    later = datetime(2026, 9, 8, 21, tzinfo=UTC)
    assert select_rank(tmp_path, "AAPL", later, auto=True)["rank"] is None
    assert select_rank(tmp_path, "AAPL", later, auto=True)["source"] == SOURCE
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as db:
        assert (
            db.execute("SELECT COUNT(*) FROM audit WHERE kind='iv_source_switch'").fetchone()[0]
            == 1
        )


class Feed:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def call(self, name, args):
        self.calls += 1
        if self.fail:
            raise TimeoutError("no feed")
        r = sample()
        payload = {
            "get_option_chains": [{"symbol": "AAPL", "expiration_dates": [r["expiry"]]}],
            "get_option_instruments": [
                {
                    "id": r["instrument_id"],
                    "chain_symbol": "AAPL",
                    "type": "call",
                    "expiration_date": r["expiry"],
                    "strike_price": 100,
                    "state": "active",
                    "tradability": "tradable",
                }
            ],
            "get_equity_quotes": [
                {"symbol": "AAPL", "last_trade_price": 100, "venue_last_trade_time": r["spot_at"]}
            ],
            "get_option_market_data": [{}],
            "get_option_quotes": [
                {
                    "instrument_id": r["instrument_id"],
                    "updated_at": r["quote_at"],
                    "implied_volatility": r["iv"],
                    "bid_price": r["bid"],
                    "ask_price": r["ask"],
                }
            ],
        }
        return {"data": {"results": payload[name]}}


def test_retries_restart_no_requests_on_holiday_and_missing_alert(tmp_path):
    config = cfg(tmp_path)
    feed = Feed()
    collect_once(config, feed, lambda: datetime(2026, 9, 7, 19, 55, tzinfo=UTC))
    assert feed.calls == 0
    now = datetime(2026, 9, 8, 19, 55, 1, tzinfo=UTC)
    collect_once(config, Feed(fail=True), lambda: now)
    with sqlite3.connect(tmp_path / DATABASE) as db:
        assert db.execute("SELECT error FROM attempts").fetchone()[0]
    collect_once(config, feed, lambda: now)
    count = feed.calls
    collect_once(config, feed, lambda: now)
    assert feed.calls == count
    assert robinhood_rank(tmp_path, "AAPL", now + timedelta(hours=1))["days"] == 1
    later = datetime(2026, 9, 9, 21, tzinfo=UTC)
    result = collect_once(config, feed, lambda: later)
    assert result["symbols"][0]["missed_since_start"] == 1
    assert feed.calls == count
    assert not collection_status(tmp_path, ["AAPL"], later + timedelta(minutes=11))["healthy"]


def test_auto_config_cannot_enable_live():
    with pytest.raises(ValueError):
        Config(paper_iv_auto_switch=True)
    with pytest.raises(ValueError):
        Config(robinhood_iv_daily_collection=True, data_provider="robinhood_mcp", mode="live_auto")
