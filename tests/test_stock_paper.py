import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from optionsagent.daily_report import daily_report
from optionsagent.runtime import single_writer
from optionsagent.stock_paper import RULES, SYMBOLS, StockAccount, candles, quote, signal, status

OPEN = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
CLOSE = OPEN + timedelta(hours=6, minutes=30)
NOW = OPEN + timedelta(minutes=20)


def raw_quote(now=NOW, bid=101.01, ask=101.02):
    return dict(
        state="active",
        has_traded=True,
        bid_price=bid,
        ask_price=ask,
        venue_bid_time=now.isoformat(),
        venue_ask_time=now.isoformat(),
    )


def bar(i, close=100, high=101, low=99, volume=100):
    return dict(
        begins_at=(OPEN + timedelta(minutes=5 * i)).isoformat(),
        open_price=100,
        high_price=high,
        low_price=low,
        close_price=close,
        volume=volume,
        session="reg",
    )


def setup():
    source = [bar(i) for i in range(3)] + [bar(3, close=101.02, high=101.03, volume=150)]
    spy = [bar(i) for i in range(3)] + [bar(3, close=100.5)]
    bars = candles(source, NOW, OPEN, CLOSE)
    benchmark = candles(spy, NOW, OPEN, CLOSE)
    sig, reason = signal(bars, benchmark, NOW, OPEN)
    assert not reason
    return source, bars, benchmark, sig


def account(root):
    a = StockAccount(root)
    a.monitor({}, NOW, OPEN, CLOSE)
    return a


def buy(a, symbol="AAPL", now=NOW):
    sig = setup()[3]
    result = a.consider(symbol, sig, quote(raw_quote(now), now), now, OPEN, CLOSE)
    assert result.startswith("BUY filled")
    return a.s["positions"][symbol]


def test_real_candle_requirements_and_freshness():
    source, bars, spy, sig = setup()
    assert sig["stop"] == 99 and sig["volume_ratio"] == 1.5
    # A new incomplete interval must not enter the signal calculation.
    assert candles(source + [bar(4)], NOW, OPEN, CLOSE) == bars
    assert signal(bars, spy, NOW + timedelta(minutes=5), OPEN)[0] is None
    for bad in [
        dict(interpolated=True),
        dict(volume=0),
        dict(high_price=99),
        dict(close_price="nan"),
        dict(session="post"),
    ]:
        changed = deepcopy(source)
        changed[1].update(bad)
        assert signal(candles(changed, NOW, OPEN, CLOSE), spy, NOW, OPEN)[0] is None
    assert signal(candles(source + [source[0]], NOW, OPEN, CLOSE), spy, NOW, OPEN)[0] is None
    assert signal(bars, [], NOW, OPEN)[0] is None
    source[-1]["volume"] = 110
    assert "volume" in signal(candles(source, NOW, OPEN, CLOSE), spy, NOW, OPEN)[1]


@pytest.mark.parametrize(
    "change",
    [
        {"venue_bid_time": (NOW - timedelta(seconds=31)).isoformat()},
        {"venue_ask_time": (NOW + timedelta(seconds=6)).isoformat()},
        {"venue_bid_time": "2026-09-08T13:50:00"},
        {"bid_price": "nan"},
        {"ask_price": 0},
        {"bid_price": 110},
        {"has_traded": False},
        {"state": "inactive"},
    ],
)
def test_bad_quotes_cannot_execute(change):
    with pytest.raises((ValueError, KeyError)):
        quote(dict(raw_quote(), **change), NOW)


def test_share_sizing_target_accounting_and_restart(tmp_path):
    a = account(tmp_path)
    p = buy(a)
    assert 1 <= p["quantity"] < 100  # Shares, not option contract multipliers.
    assert p["planned_risk"] <= 25
    assert p["quantity"] * p["entry"] + 0.01 <= 5000
    assert a.s["cash"] == pytest.approx(25000 - p["quantity"] * p["entry"] - 0.01)
    a.save(NOW)
    b = StockAccount(tmp_path)
    assert b.s == a.s
    exit_bid = (p["entry"] + 51 / p["quantity"]) / (1 - RULES["slippage"])
    later = NOW + timedelta(seconds=15)
    b.monitor(
        {"AAPL": quote(raw_quote(later, exit_bid, exit_bid + 0.01), later)}, later, OPEN, CLOSE
    )
    b.save(later)
    assert not b.s["positions"]
    assert b.s["trades"][0]["pnl"] == pytest.approx(50.98)
    assert b.s["cash"] == pytest.approx(25050.98)
    assert b.s["realized_pnl"] == pytest.approx(50.98)
    assert status(tmp_path)["trades"][0]["reason"] == "$50 net target"
    assert (
        "cooldown"
        in b.consider("AAPL", setup()[3], quote(raw_quote(), NOW), later, OPEN, CLOSE).lower()
    )
    report = daily_report(tmp_path, "2026-09-08")
    assert "BUY filled: AAPL" in report["markdown"]
    assert "SELL filled: AAPL" in report["markdown"]
    assert len(report["accounts"][-1]["closed_trades"]) == 1


def test_pause_limits_and_stale_exit_preserve_holdings(tmp_path):
    a = account(tmp_path)
    sig = setup()[3]
    q = quote(raw_quote(), NOW)
    assert "paused" in a.consider("AAPL", sig, q, NOW, OPEN, CLOSE, True)
    for symbol in ["AAPL", "AMD", "MSFT"]:
        buy(a, symbol)
    assert a.consider("NVDA", sig, q, NOW, OPEN, CLOSE) == "Position limit"
    a.monitor({}, CLOSE - timedelta(minutes=4), OPEN, CLOSE)
    assert len(a.s["positions"]) == 3
    later = CLOSE - timedelta(minutes=3)
    a.monitor({"AAPL": quote(raw_quote(later), later)}, later, OPEN, CLOSE)
    assert len(a.s["positions"]) == 2
    assert a.s["trades"][0]["reason"] == "End of session"
    assert a.s["trades"][0]["pnl"] < 0


def test_daily_loss_halt_sticky_and_gap_is_not_capped(tmp_path):
    a = account(tmp_path)
    p = buy(a)
    later = NOW + timedelta(seconds=15)
    a.monitor({"AAPL": quote(raw_quote(later, 85, 85.01), later)}, later, OPEN, CLOSE)
    assert a.s["halted"]
    assert a.s["trades"][0]["pnl"] < -100  # Stops cannot promise a maximum loss.
    assert a.s["trades"][0]["reason"] == "Daily loss halt"
    a.monitor({}, later + timedelta(minutes=20), OPEN, CLOSE)
    assert a.s["halted"]
    assert a.s["cash"] == pytest.approx(25000 + a.s["trades"][0]["pnl"])
    assert p["quantity"] > 0


def test_protective_stop_and_fresh_signal_after_cooldown(tmp_path):
    a = account(tmp_path)
    buy(a)
    later = NOW + timedelta(seconds=15)
    a.monitor({"AAPL": quote(raw_quote(later, 98.99, 99), later)}, later, OPEN, CLOSE)
    assert a.s["trades"][0]["reason"] == "Protective loss exit"
    sig = setup()[3]
    assert "consumed" in a.consider(
        "AAPL", sig, quote(raw_quote(), NOW), later + timedelta(minutes=16), OPEN, CLOSE
    )
    assert a.s["entries"] == 1


def test_no_afterhours_or_early_close_entry(tmp_path):
    a = account(tmp_path)
    sig = setup()[3]
    q = quote(raw_quote(), NOW)
    assert a.consider("AAPL", sig, q, OPEN + timedelta(minutes=19), OPEN, CLOSE).startswith(
        "Outside"
    )
    early_close = OPEN + timedelta(hours=3, minutes=30)
    assert a.consider(
        "AAPL", sig, q, early_close - timedelta(minutes=29), OPEN, early_close
    ).startswith("Outside")
    buy(a)
    a.monitor({"AAPL": q}, CLOSE, OPEN, CLOSE)
    assert a.s["positions"]
    next_open = OPEN + timedelta(days=1)
    next_q = quote(raw_quote(next_open), next_open)
    a.monitor({"AAPL": next_q}, next_open, next_open, CLOSE + timedelta(days=1))
    assert a.s["trades"][0]["reason"] == "Overnight recovery"


def test_lock_and_rules_change_cannot_reset_cash(tmp_path):
    a = account(tmp_path)
    buy(a)
    a.save(NOW)
    with single_writer(tmp_path / "stocks"), pytest.raises(RuntimeError):
        with single_writer(tmp_path / "stocks"):
            pass
    a.s["fingerprint"] = "old"
    a.save(NOW)
    with pytest.raises(ValueError, match="migration"):
        StockAccount(tmp_path)
    assert len(SYMBOLS) == len(set(SYMBOLS)) == 50


def test_failed_transaction_keeps_previous_checkpoint(tmp_path, monkeypatch):
    a = account(tmp_path)
    a.save(NOW)
    buy(a)
    original = a.connect

    def readonly():
        db = original()
        db.execute("PRAGMA query_only=ON")
        return db

    monkeypatch.setattr(a, "connect", readonly)
    with pytest.raises(sqlite3.OperationalError):
        a.save(NOW)
    restored = StockAccount(tmp_path)
    assert restored.s["cash"] == 25000
    assert restored.s["positions"] == {}


def test_delayed_quotes_fail_again_at_fill_time(tmp_path):
    a = account(tmp_path)
    q = quote(raw_quote(), NOW)
    later = NOW + timedelta(seconds=31)
    assert a.consider('AAPL', setup()[3], q, later, OPEN, CLOSE) == 'No fresh executable quote'
    buy(a)
    a.monitor({'AAPL': quote(raw_quote(NOW, 200, 200.01), NOW)}, later, OPEN, CLOSE)
    assert a.s['positions'] and not a.s['trades']
    fresh = quote(raw_quote(later), later)
    assert a.consider('AMD', setup()[3], fresh, later, OPEN, CLOSE) == 'Held position marks stale'


def test_limits_spread_and_too_small_risk_budget(tmp_path):
    a = account(tmp_path)
    sig = setup()[3]
    wide = quote(raw_quote(NOW, 100, 101.02), NOW)
    assert a.consider('AAPL', sig, wide, NOW, OPEN, CLOSE) == 'Spread too wide'
    q = quote(raw_quote(), NOW)
    assert 'too small' in a.consider('AAPL', dict(sig, stop=1), q, NOW, OPEN, CLOSE)
    a.s['entries'] = 10
    assert a.consider('AAPL', sig, q, NOW, OPEN, CLOSE) == 'Daily entry limit'
    a.s['entries'] = 0
    a.s['symbol_entries']['AAPL'] = 3
    assert a.consider('AAPL', sig, q, NOW, OPEN, CLOSE) == 'Daily entry limit'
    a.s['symbol_entries'].clear()
    a.s['cash'] = 50
    assert 'too small' in a.consider('AAPL', sig, q, NOW, OPEN, CLOSE)
