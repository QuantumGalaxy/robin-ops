from datetime import date, timedelta

import pytest

from optionsagent.config import RiskConfig
from optionsagent.models import ExitReason, OptionContract, Position
from optionsagent.portfolio import Portfolio
from optionsagent.risk import RiskManager
from optionsagent.simulate import breakeven_win_rate, expectancy, kelly_fraction

TODAY = date(2025, 6, 2)


def make_position(symbol: str = "AAPL", strike: float = 200.0) -> Position:
    return Position(
        contract=OptionContract(symbol, TODAY + timedelta(days=35), strike, "call"),
        quantity=2,
        entry_price=5.00,
        peak_return=0.22,
        trailing_armed=True,
    )


def test_state_survives_a_restart(tmp_path):
    pf = Portfolio(state_dir=tmp_path)
    pf.add(make_position())

    restored = Portfolio.load(tmp_path)
    assert len(restored.positions) == 1
    pos = next(iter(restored.positions.values()))
    # The high-water mark has to survive, or every restart resets the trailing stop.
    assert pos.peak_return == pytest.approx(0.22)
    assert pos.trailing_armed is True
    assert pos.quantity == 2


def test_closing_records_pnl_and_appends_to_the_log(tmp_path):
    pf = Portfolio(state_dir=tmp_path)
    pos = make_position()
    pf.add(pos)
    record = pf.close(pos.contract.occ_symbol, 6.00, ExitReason.TRAILING_STOP)

    assert record is not None
    assert record.pnl == pytest.approx(200.0)  # $1.00 x 100 x 2 contracts
    assert record.return_pct == pytest.approx(0.20)
    assert pf.positions == {}
    assert pf.trade_log.exists()
    assert pf.trade_log.read_text().count("\n") == 1


def test_find_orphans_spots_positions_the_broker_no_longer_reports(tmp_path):
    pf = Portfolio(state_dir=tmp_path)
    kept = make_position("AAPL")
    gone = make_position("MSFT", 400.0)
    pf.add(kept)
    pf.add(gone)

    orphans = pf.find_orphans([kept])
    assert [p.contract.occ_symbol for p in orphans] == [gone.contract.occ_symbol]
    # Nothing is dropped here: the caller has to book it as a closed trade.
    assert len(pf.positions) == 2


def test_summary_reports_win_rate_and_profit_factor(tmp_path):
    pf = Portfolio(state_dir=tmp_path)
    for strike, exit_price in ((200.0, 6.0), (210.0, 2.5), (220.0, 5.5)):
        pos = make_position(strike=strike)
        pf.add(pos)
        pf.close(pos.contract.occ_symbol, exit_price, ExitReason.TRAILING_STOP)
    s = pf.summary()
    assert s["closed_trades"] == 3
    assert s["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["profit_factor"] is not None


def test_kill_switch_blocks_entries(tmp_path):
    kill = tmp_path / "KILL"
    rm = RiskManager(RiskConfig(kill_switch_file=str(kill)))
    rm.start_session(TODAY, 25_000)
    assert rm.can_open(25_000, 0)[0] is True
    kill.touch()
    allowed, why = rm.can_open(25_000, 0)
    assert allowed is False
    assert "kill switch" in why


def test_daily_loss_limit_halts_new_entries():
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.05))
    rm.start_session(TODAY, 100_000)
    assert rm.can_open(97_000, 0)[0] is True
    allowed, why = rm.can_open(94_000, 0)
    assert allowed is False
    assert "daily loss limit" in why


def test_drawdown_halt():
    rm = RiskManager(RiskConfig(max_drawdown_halt_pct=0.20, daily_loss_limit_pct=1.0))
    rm.start_session(TODAY, 100_000)
    allowed, why = rm.can_open(75_000, 0)
    assert allowed is False
    assert "drawdown" in why


def test_pdt_reserves_a_day_trade_for_stop_losses():
    rm = RiskManager(RiskConfig())
    rm.start_session(TODAY, 20_000)
    assert rm.can_open(20_000, 0)[0] is True
    allowed, why = rm.can_open(20_000, 2)
    assert allowed is False
    assert "PDT" in why
    # Above the threshold the rule does not apply at all.
    rm2 = RiskManager(RiskConfig())
    rm2.start_session(TODAY, 30_000)
    assert rm2.can_open(30_000, 3)[0] is True


def test_consecutive_losses_halt():
    rm = RiskManager(RiskConfig(max_consecutive_losses=3))
    rm.start_session(TODAY, 25_000)
    for _ in range(3):
        rm.record_trade_result(-100)
    assert rm.can_open(25_000, 0)[0] is False
    rm.record_trade_result(50)
    assert rm.can_open(25_000, 0)[0] is True


def test_breakeven_win_rate_of_the_original_rules():
    # The headline number: +10% wins against -50% losses need 5 wins per loss.
    assert breakeven_win_rate(0.10, -0.50) == pytest.approx(5 / 6, abs=1e-9)
    assert breakeven_win_rate(0.50, -0.50) == pytest.approx(0.5)


def test_expectancy_is_negative_below_the_breakeven_rate():
    assert expectancy(0.70, 0.10, -0.50) < 0
    assert expectancy(0.90, 0.10, -0.50) > 0


def test_kelly_is_zero_when_there_is_no_edge():
    assert kelly_fraction(0.70, 0.10, 0.50) == 0.0
    assert kelly_fraction(0.90, 0.10, 0.50) > 0
