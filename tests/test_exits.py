"""Tests for the exit rules, which are the part of the agent that must not be wrong."""

from datetime import UTC, date, datetime, timedelta

import pytest

from optionsagent.config import ExitConfig
from optionsagent.greeks import compute_greeks, years_to_expiry
from optionsagent.models import ExitReason, OptionContract, OptionQuote, Position
from optionsagent.strategy.exits import evaluate_exit, trailing_stop_level

NOW = datetime(2025, 6, 2, 15, 0, tzinfo=UTC)


def make_position(entry: float = 10.0, dte: int = 30, opened_days_ago: float = 1.0) -> Position:
    contract = OptionContract("AAPL", NOW.date() + timedelta(days=dte), 200.0, "call")
    return Position(
        contract=contract,
        quantity=1,
        entry_price=entry,
        opened_at=NOW - timedelta(days=opened_days_ago),
    )


def make_quote(position: Position, mark: float, iv: float = 0.30) -> OptionQuote:
    c = position.contract
    dte = c.days_to_expiry(NOW.date())
    return OptionQuote(
        contract=c,
        bid=round(mark * 0.99, 2),
        ask=round(mark * 1.01, 2),
        underlying_price=200.0,
        greeks=compute_greeks(200.0, c.strike, years_to_expiry(dte), 0.04, iv, 0.0, "call"),
    )


def test_holds_while_flat():
    pos = make_position()
    d = evaluate_exit(pos, make_quote(pos, 10.2), ExitConfig(), as_of=NOW)
    assert not d.should_exit


def test_take_profit_arms_trailing_instead_of_selling():
    pos = make_position()
    d = evaluate_exit(pos, make_quote(pos, 11.5), ExitConfig(), as_of=NOW)
    # +15% is past the target, but the trailing stop lets it run.
    assert not d.should_exit
    assert pos.trailing_armed
    assert pos.peak_return == pytest.approx(0.15, abs=1e-9)


def test_take_profit_sells_when_trailing_disabled():
    pos = make_position()
    cfg = ExitConfig(trailing_enabled=False)
    d = evaluate_exit(pos, make_quote(pos, 11.5), cfg, as_of=NOW)
    assert d.should_exit
    assert d.reason is ExitReason.TAKE_PROFIT


def test_trailing_stop_fires_on_giveback():
    pos = make_position()
    cfg = ExitConfig()
    evaluate_exit(pos, make_quote(pos, 14.0), cfg, as_of=NOW)  # peak +40%
    assert pos.trailing_armed
    # Stop sits at 40% * (1 - 0.40) = +24%. Still above it at +30%.
    assert not evaluate_exit(pos, make_quote(pos, 13.0), cfg, as_of=NOW).should_exit
    d = evaluate_exit(pos, make_quote(pos, 12.2), cfg, as_of=NOW)
    assert d.should_exit
    assert d.reason is ExitReason.TRAILING_STOP


def test_trailing_stop_never_trails_below_the_target():
    cfg = ExitConfig()
    # Arithmetic would put the stop at +7.2%, but the floor holds it at +10%.
    assert trailing_stop_level(0.12, cfg) == pytest.approx(0.10)
    assert trailing_stop_level(0.50, cfg) == pytest.approx(0.30)


def test_barely_profitable_position_exits_at_the_floor():
    pos = make_position()
    cfg = ExitConfig()
    evaluate_exit(pos, make_quote(pos, 11.2), cfg, as_of=NOW)  # arms at +12%
    d = evaluate_exit(pos, make_quote(pos, 10.9), cfg, as_of=NOW)  # falls to +9%
    assert d.should_exit
    assert d.reason is ExitReason.TRAILING_STOP


def test_hard_stop_loss():
    pos = make_position()
    d = evaluate_exit(pos, make_quote(pos, 4.9), ExitConfig(), as_of=NOW)
    assert d.should_exit
    assert d.reason is ExitReason.STOP_LOSS
    assert d.urgency == "urgent"


def test_stop_loss_beats_trailing_stop_in_priority():
    pos = make_position()
    cfg = ExitConfig()
    evaluate_exit(pos, make_quote(pos, 15.0), cfg, as_of=NOW)  # arm the trail
    d = evaluate_exit(pos, make_quote(pos, 4.0), cfg, as_of=NOW)
    assert d.reason is ExitReason.STOP_LOSS


def test_expiry_guard_closes_regardless_of_pnl():
    for mark in (5.0, 10.0, 20.0):
        pos = make_position(dte=2)
        d = evaluate_exit(pos, make_quote(pos, mark), ExitConfig(), as_of=NOW)
        assert d.should_exit
        assert d.reason is ExitReason.EXPIRY_GUARD


def test_tighter_stop_near_expiry():
    pos = make_position(dte=5)
    # Down 35%: survives the -50% stop but not the near-expiry -30% one.
    d = evaluate_exit(pos, make_quote(pos, 6.5), ExitConfig(), as_of=NOW)
    assert d.should_exit
    assert d.reason is ExitReason.STOP_LOSS
    assert "only 5d left" in d.detail


def test_time_stop_after_two_weeks():
    pos = make_position(opened_days_ago=14.5)
    d = evaluate_exit(pos, make_quote(pos, 10.1), ExitConfig(), as_of=NOW)
    assert d.should_exit
    assert d.reason is ExitReason.TIME_STOP


def test_earnings_exit():
    pos = make_position()
    d = evaluate_exit(pos, make_quote(pos, 10.1), ExitConfig(), as_of=NOW, earnings_date=NOW.date())
    assert d.should_exit
    assert "IV crush" in d.detail


def test_theta_bleed_exit_only_when_not_working():
    pos = make_position(dte=8)
    cfg = ExitConfig(expiry_guard_dte=1, theta_bleed_pct_per_day=0.02)
    # Flat and decaying fast: get out.
    decision = evaluate_exit(pos, make_quote(pos, 10.0), cfg, as_of=NOW)
    assert decision.reason is ExitReason.THETA_BLEED
    # Already working: decay is the price of the trade, so hold.
    pos2 = make_position(dte=8)
    assert not evaluate_exit(pos2, make_quote(pos2, 11.5), cfg, as_of=NOW).should_exit


def test_peak_return_is_monotonic():
    pos = make_position()
    cfg = ExitConfig()
    for mark in (11.0, 13.0, 11.5, 12.0):
        evaluate_exit(pos, make_quote(pos, mark), cfg, as_of=NOW)
    assert pos.peak_return == pytest.approx(0.30, abs=1e-9)


def test_days_to_expiry_uses_the_provided_date():
    c = OptionContract("AAPL", date(2025, 6, 20), 200.0, "call")
    assert c.days_to_expiry(date(2025, 6, 2)) == 18
