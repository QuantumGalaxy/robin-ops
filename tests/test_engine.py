"""End-to-end tests of the loop against the synthetic market and paper broker."""

from datetime import UTC, date, datetime, time, timedelta

import pytest

from optionsagent.brokers.paper import PaperBroker
from optionsagent.config import Config
from optionsagent.engine import TradingEngine
from optionsagent.marketdata.synthetic import SyntheticMarketData
from optionsagent.models import ExitReason, OptionContract, Position
from optionsagent.portfolio import Portfolio

START = date(2025, 3, 3)


def build(tmp_path, **cfg_overrides):
    cfg_overrides.setdefault("state_dir", str(tmp_path))
    cfg = Config(**cfg_overrides)
    market = SyntheticMarketData(symbols=cfg.universe.symbols, seed=5, today=START)
    broker = PaperBroker(
        starting_equity=cfg.broker.starting_equity,
        seed=3,
        miss_probability=0.0,
        clock=lambda: datetime.combine(market.today, time(15, 0), tzinfo=UTC),
    )
    engine = TradingEngine(
        config=cfg, data=market, broker=broker, portfolio=Portfolio(state_dir=tmp_path)
    )
    engine.warmup()
    return cfg, market, broker, engine


def now_for(market: SyntheticMarketData) -> datetime:
    return datetime.combine(market.today, time(15, 0), tzinfo=UTC)


def test_loop_runs_and_stays_within_position_limits(tmp_path):
    cfg, market, broker, engine = build(tmp_path)
    for _ in range(40):
        market.step(1)
        engine.run_once(as_of=now_for(market))
        assert len(engine.portfolio.positions) <= cfg.sizing.max_positions
        for symbol in engine.portfolio.symbols_held():
            assert engine.portfolio.count_for_symbol(symbol) <= cfg.sizing.max_positions_per_symbol


def test_agent_actually_trades(tmp_path):
    _, market, broker, engine = build(tmp_path)
    for _ in range(60):
        market.step(1)
        engine.run_once(as_of=now_for(market))
    assert engine.portfolio.positions or engine.portfolio.trades


def test_no_position_survives_into_expiry_week(tmp_path):
    cfg, market, broker, engine = build(tmp_path)
    for _ in range(90):
        market.step(1)
        engine.run_once(as_of=now_for(market))
        for pos in engine.portfolio.positions.values():
            assert pos.contract.days_to_expiry(market.today) > cfg.exit.expiry_guard_dte


def test_premium_exposure_stays_under_the_cap(tmp_path):
    cfg, market, broker, engine = build(tmp_path)
    for _ in range(60):
        market.step(1)
        engine.run_once(as_of=now_for(market))
        assert engine.portfolio.open_premium() <= broker.equity() * (
            cfg.sizing.max_portfolio_premium_pct + 0.05
        )


def test_kill_switch_stops_new_entries(tmp_path):
    kill = tmp_path / "KILL"
    cfg_overrides = {"risk": {"kill_switch_file": str(kill)}}
    _, market, broker, engine = build(tmp_path, **cfg_overrides)
    kill.touch()
    for _ in range(30):
        market.step(1)
        report = engine.run_once(as_of=now_for(market))
        assert "kill switch" in report.blocked_reason
    assert engine.portfolio.positions == {}


def test_expired_position_is_settled_not_leaked(tmp_path):
    """A contract past expiry must leave the ledger even with no quote available."""
    _, market, broker, engine = build(tmp_path)
    contract = OptionContract("AAPL", market.today - timedelta(days=1), 150.0, "call")
    engine.portfolio.add(Position(contract=contract, quantity=1, entry_price=4.0))

    engine.run_once(as_of=now_for(market))

    assert contract.occ_symbol not in engine.portfolio.positions
    assert engine.portfolio.trades[-1].reason is ExitReason.EXPIRED


def test_reconcile_drops_a_position_closed_outside_the_agent(tmp_path):
    _, market, broker, engine = build(tmp_path)
    for _ in range(40):
        market.step(1)
        engine.run_once(as_of=now_for(market))
        if engine.portfolio.positions:
            break
    if not engine.portfolio.positions:
        pytest.skip("no position opened in this synthetic path")

    key = next(iter(engine.portfolio.positions))
    broker._positions.clear()
    engine.run_once(as_of=now_for(market))
    assert key not in engine.portfolio.positions
    # It has to land in the trade log rather than silently disappearing.
    assert engine.portfolio.trades[-1].reason is ExitReason.MANUAL


def test_entry_limit_never_pays_more_than_the_ask(tmp_path):
    _, market, _, engine = build(tmp_path)
    quotes = market.option_chain("AAPL", 30, 45)
    for q in quotes[:50]:
        for concession in engine._concession_ladder(0.25):
            limit = engine._entry_limit(q, concession)
            # Penny rounding can nudge the limit a hair either side of the mid.
            assert q.mid - 0.01 <= limit <= q.ask


def test_urgent_exit_crosses_to_the_bid(tmp_path):
    _, market, _, engine = build(tmp_path)
    q = market.option_chain("AAPL", 30, 45)[0]
    assert engine._exit_limit(q, urgent=True) == pytest.approx(round(q.bid, 2))
    assert engine._exit_limit(q, urgent=False) > q.bid
