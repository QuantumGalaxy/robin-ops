"""Regression tests for the independent review's failure scenarios."""

from datetime import UTC, date, datetime, timedelta

import pytest
from typer.testing import CliRunner

from optionsagent.brokers.paper import PaperBroker
from optionsagent.brokers.robinhood_mcp import RobinhoodMcpBroker
from optionsagent.cli import app
from optionsagent.config import Config, EntryConfig, ExitConfig, Mode, SizingConfig
from optionsagent.engine import LoopReport, TradingEngine
from optionsagent.marketdata.synthetic import SyntheticMarketData
from optionsagent.mcp.client import FakeToolCaller
from optionsagent.models import ExitReason, OptionContract, OptionQuote, Position
from optionsagent.orders import OrderState
from optionsagent.portfolio import Portfolio
from optionsagent.runtime import RuntimeStore, single_writer
from optionsagent.strategy.entry import EntryScreener
from optionsagent.strategy.exits import evaluate_exit
from optionsagent.strategy.signals import PriceHistory
from optionsagent.strategy.sizing import size_position

NOW = datetime(2026, 9, 8, 15, tzinfo=UTC)


def quote():
    c = OptionContract("AAPL", NOW.date() + timedelta(days=14), 195, "call")
    return OptionQuote(c, 4.9, 5.1, 200, as_of=NOW)


def engine(tmp_path, broker=None, mode=Mode.PAPER):
    cfg = Config(state_dir=str(tmp_path), mode=mode)
    data = SyntheticMarketData(today=NOW.date())
    return TradingEngine(cfg, data, broker or PaperBroker(miss_probability=0), Portfolio(tmp_path))


class QueuedBroker(PaperBroker):
    synchronous_fills = False

    def __post_init__(self):
        super().__post_init__()
        self.submissions = 0
        self.timeout = False

    def buy_to_open(self, q, quantity, limit_price):
        self.submissions += 1
        if self.timeout:
            raise TimeoutError("accepted but response lost")
        return None

    def has_open_order(self, symbol):
        return self.submissions > 0


def test_queued_entry_is_submitted_once_even_across_later_loops(tmp_path):
    b = QueuedBroker()
    e = engine(tmp_path, b)
    assert e._buy_with_reprice(quote(), 1, NOW) is None
    assert e._buy_with_reprice(quote(), 1, NOW + timedelta(hours=1)) is None
    assert b.submissions == 1
    assert e.orders.pending()


def test_timeout_keeps_reservation_across_restart(tmp_path):
    b = QueuedBroker()
    b.timeout = True
    e = engine(tmp_path, b)
    with pytest.raises(TimeoutError):
        e._buy_with_reprice(quote(), 1, NOW)
    restarted = engine(tmp_path, b)
    assert restarted._buy_with_reprice(quote(), 1, NOW + timedelta(days=1)) is None
    assert b.submissions == 1
    assert next(iter(restarted.orders.orders.values())).state == OrderState.PENDING


def test_partial_fill_parser_uses_executed_quantity():
    caller = FakeToolCaller(
        responses={
            "review_option_order": {"alerts": []},
            "place_option_order": {
                "id": "one",
                "state": "partially_filled",
                "quantity": 2,
                "filled_quantity": 1,
                "average_price": 5.0,
            },
        }
    )
    b = RobinhoodMcpBroker(caller, dry_run=False)
    assert b.buy_to_open(quote(), 2, 5.1).quantity == 1


def test_missing_fill_fields_do_not_invent_execution():
    caller = FakeToolCaller(
        responses={
            "review_option_order": {"alerts": []},
            "place_option_order": {"id": "one", "state": "filled"},
        }
    )
    with pytest.raises(ValueError, match="fill schema"):
        RobinhoodMcpBroker(caller, dry_run=False).buy_to_open(quote(), 1, 5.1)


def test_partial_exit_preserves_remainder_and_allocates_entry_fees(tmp_path):
    pf = Portfolio(tmp_path)
    q = quote()
    pf.add(Position(q.contract, 2, 5, entry_fees=0.12))
    first = pf.close(
        q.contract.occ_symbol, 6, ExitReason.TAKE_PROFIT, fees=0.06, quantity=1, at=NOW
    )
    assert first.pnl == pytest.approx(99.88)
    assert pf.positions[q.contract.occ_symbol].quantity == 1
    assert pf.positions[q.contract.occ_symbol].entry_fees == pytest.approx(0.06)
    pf.close(q.contract.occ_symbol, 4, ExitReason.STOP_LOSS, fees=0.06, at=NOW)
    loaded = Portfolio.load(tmp_path)
    assert loaded.summary()["closed_trades"] == 2
    assert loaded.realized_pnl == pytest.approx(-0.24)


def test_off_makes_no_calls(tmp_path):
    e = engine(tmp_path, mode=Mode.OFF)
    e.broker.equity = lambda: pytest.fail("OFF queried broker")
    assert e.run_once(NOW).blocked_reason == "off"


def test_scan_only_never_manages_exits_or_settles(tmp_path):
    e = engine(tmp_path, mode=Mode.SCAN_ONLY)
    e._manage_exits = lambda *a: pytest.fail("scan sent exit")
    e._settle_expirations = lambda *a: pytest.fail("scan settled position")
    assert "scan-only" in e.run_once(NOW).blocked_reason


@pytest.mark.parametrize("mode", [Mode.LIVE_APPROVAL, Mode.LIVE_AUTO])
def test_unverified_live_modes_cannot_start(tmp_path, mode):
    with pytest.raises(RuntimeError, match="Live trading disabled"):
        engine(tmp_path, mode=mode)


def test_paper_rejects_real_broker(tmp_path):
    b = RobinhoodMcpBroker(FakeToolCaller(), dry_run=False)
    with pytest.raises(RuntimeError, match="PaperBroker"):
        engine(tmp_path, b)


def test_cli_live_flag_blocks_before_any_connection():
    result = CliRunner().invoke(app, ["run", "--live"])
    assert result.exit_code != 0
    assert "Live execution is disabled" in result.output


def test_complete_checkpoint_restores_cash_holdings_risk_and_world(tmp_path):
    e = engine(tmp_path)
    e.store = RuntimeStore(tmp_path)
    q = quote()
    fill = e.broker.buy_to_open(q, 1, q.ask)
    e.portfolio.add(
        Position(
            q.contract, 1, fill.price, entry_fees=fill.fees, peak_return=0.3, trailing_armed=True
        )
    )
    e.risk.start_session(NOW.date(), 25000)
    e.risk.state.consecutive_losses = 4
    e.risk.state.halted_reason = "drawdown review required"
    e.data.step(2)
    e._checkpoint()
    cash, equity = e.broker.cash, e.broker.equity()
    random_next = e.data.rng.random()
    restarted = engine(tmp_path)
    assert e.store.restore(restarted)
    assert restarted.broker.cash == cash
    assert restarted.broker.equity() == equity
    assert restarted.risk.state.consecutive_losses == 4
    assert restarted.risk.state.halted_reason == "drawdown review required"
    assert restarted.data.today == e.data.today
    assert restarted.data.rng.random() == random_next
    assert restarted.portfolio.positions[q.contract.occ_symbol].trailing_armed
    report = LoopReport(NOW, equity)
    restarted._reconcile(report)
    assert report.reconcile_mismatch == ""


def test_broker_only_holding_latches_halt(tmp_path):
    e = engine(tmp_path)
    q = quote()
    e.broker.buy_to_open(q, 1, q.ask)
    report = LoopReport(NOW, 25000)
    e._reconcile(report)
    assert "broker-only" in e.reconcile_halt
    e.broker._positions.clear()
    e._reconcile(report)
    assert e.reconcile_halt
    assert not e.portfolio.trades


def test_quantity_mismatch_is_not_silently_repaired(tmp_path):
    e = engine(tmp_path)
    q = quote()
    e.broker.buy_to_open(q, 1, q.ask)
    e.portfolio.add(Position(q.contract, 2, 5))
    report = LoopReport(NOW, 25000)
    e._reconcile(report)
    assert "quantity mismatch" in report.reconcile_mismatch
    assert e.portfolio.positions[q.contract.occ_symbol].quantity == 2


def test_two_writers_cannot_share_account(tmp_path):
    with single_writer(tmp_path), pytest.raises(RuntimeError, match="Another agent"):
        with single_writer(tmp_path):
            pass


def test_polling_does_not_turn_daily_signal_into_minute_signal():
    history = PriceHistory()
    for i in range(60):
        history.push_daily("AAPL", 200 + i, date(2026, 9, 8))
    assert history.get("AAPL") == [200]
    history.push_daily("AAPL", 205, date(2026, 9, 9))
    assert history.get("AAPL") == [200, 205]


def test_stale_quotes_and_nonfinite_quotes_are_rejected():
    q = quote()
    assert q.is_fresh(NOW, 120)
    assert not q.is_fresh(NOW + timedelta(minutes=3), 120)
    q.ask = float("inf")
    assert not q.is_tradeable()


def test_default_profile_and_hard_budget():
    cfg = Config()
    assert (cfg.entry.min_dte, cfg.entry.max_dte) == (12, 18)
    assert (cfg.entry.min_abs_delta, cfg.entry.max_abs_delta) == (0.55, 0.70)
    decision = size_position(quote(), 100000, 100000, 0, SizingConfig())
    assert decision.premium <= 1000
    assert decision.contracts == 1


def test_unknown_iv_rejects_entry():
    assert EntryScreener(EntryConfig()).screen([quote()], "call", iv_rank=None) == []


def test_profit_lock_uses_bid_and_exits_on_signal_loss():
    q = quote()
    p = Position(q.contract, 1, 5, opened_at=NOW)
    q.bid, q.ask = 5.4, 5.8  # mid +12%, bid +8%: do not arm
    evaluate_exit(p, q, ExitConfig(), as_of=NOW, direction="call")
    assert not p.trailing_armed
    q.bid, q.ask = 5.6, 5.8
    assert not evaluate_exit(p, q, ExitConfig(), as_of=NOW, direction="call").should_exit
    assert p.trailing_armed
    assert (
        evaluate_exit(p, q, ExitConfig(), as_of=NOW, direction="none").reason
        == ExitReason.SIGNAL_LOSS
    )


def test_unrelated_history_outage_does_not_prevent_exits(tmp_path):
    e = engine(tmp_path)
    called = []
    e.data.underlying_price = lambda *a: (_ for _ in ()).throw(ConnectionError("outage"))
    e._manage_exits = lambda *a: called.append("exits")
    e._scan_entries = lambda *a, **kw: None
    e.run_once(NOW)
    assert called == ["exits"]


def test_entry_fees_included_in_round_trip_profit(tmp_path):
    e = engine(tmp_path)
    q = quote()
    buy = e.broker.buy_to_open(q, 1, q.ask)
    p = Position(q.contract, 1, buy.price, entry_fees=buy.fees)
    e.portfolio.add(p)
    sell = e.broker.sell_to_close(p, q, q.bid, urgent=True)
    record = e.portfolio.close(q.contract.occ_symbol, sell.price, ExitReason.MANUAL, fees=sell.fees)
    assert record.pnl == pytest.approx(e.broker.equity() - 25000)


def test_http_transport_rejects_mutation_without_network():
    from optionsagent.mcp.client import HttpToolCaller, McpError

    caller = HttpToolCaller(token="fake-test-token")
    with pytest.raises(McpError, match="disabled"):
        caller.call("place_option_order", {})
    with pytest.raises(McpError, match="disabled"):
        caller.call("cancel_option_order", {})


def test_no_open_order_is_not_proof_of_no_fill(tmp_path):
    b = QueuedBroker()
    b.has_open_order = lambda symbol: False
    e = engine(tmp_path, b)
    e._buy_with_reprice(quote(), 1, NOW)
    assert e.orders.pending()
    e._buy_with_reprice(quote(), 1, NOW + timedelta(minutes=1))
    assert b.submissions == 1


def test_cli_restart_restores_checkpoint_instead_of_resetting_account(tmp_path):
    cfg = Config(state_dir=str(tmp_path / "account"))
    config_file = tmp_path / "config.yaml"
    cfg.dump(config_file)
    runner = CliRunner()
    first = runner.invoke(app, ["run", "--config", str(config_file), "--loops", "20"])
    assert first.exit_code == 0, first.exception
    store = RuntimeStore(cfg.state_dir)
    before = store.read()
    # A zero-loop second invocation restores state without trading or resetting it.
    second = runner.invoke(app, ["run", "--config", str(config_file), "--loops", "0"])
    assert second.exit_code == 0, second.exception
    after = store.read()
    assert before["paper"] == after["paper"]
    assert before["trades"] == after["trades"]
    assert before["positions"] == after["positions"]
    assert before["risk"] == after["risk"]
    third = runner.invoke(app, ["run", "--config", str(config_file), "--loops", "1"])
    assert third.exit_code == 0, third.exception
    assert store.read()["synthetic"]["today"] > after["synthetic"]["today"]


def test_unknown_reference_data_blocks_live_data_filters():
    from optionsagent.marketdata.robinhood_mcp import RobinhoodMcpMarketData

    data = RobinhoodMcpMarketData(
        FakeToolCaller(
            tools=[
                {"name": n}
                for n in (
                    "get_equity_quotes",
                    "get_option_chains",
                    "get_option_instruments",
                    "get_option_quotes",
                )
            ]
        )
    )
    assert data.iv_rank("AAPL") is None
    assert not data.earnings_known("AAPL")
    assert not data.is_market_open()


def test_runtime_rejects_source_switch(tmp_path):
    e = engine(tmp_path)
    store = RuntimeStore(tmp_path)
    store.save(e)
    e.config.data_provider = "robinhood_mcp"
    with pytest.raises(RuntimeError, match="separate state_dir"):
        store.restore(e)
