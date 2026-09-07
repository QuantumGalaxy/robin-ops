"""Tests for the Robinhood MCP adapters and duplicate-order protection."""

from datetime import UTC, date, datetime, timedelta

import pytest

from optionsagent.brokers.robinhood_mcp import RobinhoodMcpBroker
from optionsagent.marketdata.robinhood_mcp import RobinhoodMcpMarketData
from optionsagent.mcp.client import FakeToolCaller, McpError
from optionsagent.mcp.robinhood import pick, review_blocks_order, rows
from optionsagent.models import OptionContract, OptionQuote, Position, Side
from optionsagent.orders import OrderRegistry, OrderState, client_order_id

TODAY = date.today()
EXPIRY = TODAY + timedelta(days=35)


# ---- payload normalisation ----------------------------------------------


def test_rows_handles_every_shape_a_tool_might_return():
    assert rows([{"a": 1}]) == [{"a": 1}]
    assert rows({"results": [{"a": 1}]}) == [{"a": 1}]
    assert rows({"data": [{"a": 1}]}) == [{"a": 1}]
    assert rows({"a": 1}) == [{"a": 1}]
    assert rows(None) == []
    assert rows("not json") == []


def test_pick_falls_through_alternative_field_names():
    row = {"strike_price": "150.0"}
    assert pick(row, "strike", "strike_price") == "150.0"
    assert pick(row, "missing", default="fallback") == "fallback"
    # A present-but-null field must not shadow a later one that has a value.
    assert (
        pick(
            {"bid": None, "updated_at": datetime.now(UTC).isoformat(), "bid_price": 1.2},
            "bid",
            "bid_price",
        )
        == 1.2
    )


# ---- pre-trade review ----------------------------------------------------


def test_empty_review_blocks_the_order():
    blocked, why = review_blocks_order({})
    assert blocked
    assert "blind" in why


def test_broker_error_blocks_the_order():
    blocked, why = review_blocks_order({"error": "insufficient buying power"})
    assert blocked
    assert "buying power" in why


def test_error_severity_alerts_block_but_warnings_do_not():
    blocked, why = review_blocks_order(
        {"alerts": [{"severity": "error", "message": "options level too low"}]}
    )
    assert blocked
    assert "options level" in why

    blocked, _ = review_blocks_order(
        {"alerts": [{"severity": "warning", "message": "wide spread"}]}
    )
    assert not blocked


# ---- market data ---------------------------------------------------------


def build_market_data() -> tuple[RobinhoodMcpMarketData, FakeToolCaller]:
    caller = FakeToolCaller(
        tools=[
            {"name": n}
            for n in (
                "get_equity_quotes",
                "get_option_chains",
                "get_option_instruments",
                "get_option_quotes",
                "get_option_positions",
                "review_option_order",
                "place_option_order",
            )
        ],
        responses={
            "get_equity_quotes": {
                "results": [
                    {
                        "symbol": "AAPL",
                        "last_trade_price": "200.00",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            "get_option_chains": {"expiration_dates": [EXPIRY.strftime("%Y-%m-%d")]},
            "get_option_instruments": lambda args: {
                "results": (
                    [{"id": "inst-call", "strike_price": "195.0", "type": "call"}]
                    if args.get("type") == "call"
                    else [{"id": "inst-put", "strike_price": "195.0", "type": "put"}]
                )
            },
            "get_option_quotes": lambda args: {
                "results": [
                    {
                        "instrument_id": i,
                        "updated_at": datetime.now(UTC).isoformat(),
                        "bid_price": "12.00",
                        "ask_price": "12.20",
                        "open_interest": "4200",
                        "volume": "310",
                    }
                    for i in args["instrument_ids"]
                ]
            },
        },
    )
    return RobinhoodMcpMarketData(caller=caller), caller


def test_underlying_price_is_read_from_the_quote():
    data, _ = build_market_data()
    assert data.underlying_price("AAPL") == pytest.approx(200.0)


def test_chain_is_built_with_locally_computed_greeks():
    data, _ = build_market_data()
    chain = data.option_chain("AAPL", 30, 45)
    assert len(chain) == 2
    for q in chain:
        # Greeks come from our own Black-Scholes, not from the API payload.
        assert q.greeks is not None
        assert q.greeks.iv > 0
        assert q.mid == pytest.approx(12.10)
        assert q.contract.broker_id.startswith("inst-")


def test_quotes_are_batched_rather_than_requested_one_at_a_time():
    data, caller = build_market_data()
    data.option_chain("AAPL", 30, 45)
    assert len(caller.calls_to("get_option_quotes")) == 1


def test_missing_historicals_tool_degrades_quietly():
    data, _ = build_market_data()
    assert data.historical_closes("AAPL") == []


# ---- broker --------------------------------------------------------------


def build_broker(dry_run: bool = False, **kwargs) -> tuple[RobinhoodMcpBroker, FakeToolCaller]:
    caller = FakeToolCaller(
        tools=[{"name": "review_option_order"}, {"name": "place_option_order"}],
        responses={
            "get_portfolio": {"equity": "30000.00", "buying_power": "12000.00"},
            "get_option_positions": {
                "results": [
                    {
                        "chain_symbol": "AAPL",
                        "expiration_date": EXPIRY.strftime("%Y-%m-%d"),
                        "strike_price": "195.0",
                        "option_type": "call",
                        "quantity": "2",
                        "type": "long",
                        "average_price": "11.50",
                        "option_id": "inst-call",
                    }
                ]
            },
            "review_option_order": {"alerts": [], "estimated_cost": 1220.0},
            "place_option_order": {
                "id": "order-1",
                "state": "filled",
                "average_price": "12.15",
                "filled_quantity": 1,
            },
        },
    )
    return RobinhoodMcpBroker(caller=caller, dry_run=dry_run, **kwargs), caller


def make_quote() -> OptionQuote:
    contract = OptionContract("AAPL", EXPIRY, 195.0, "call")
    return OptionQuote(contract=contract, bid=12.0, ask=12.2, underlying_price=200.0)


def test_equity_and_buying_power():
    broker, _ = build_broker()
    assert broker.equity() == pytest.approx(30_000)
    assert broker.buying_power() == pytest.approx(12_000)


def test_explicit_per_share_average_price_is_preserved():
    broker, _ = build_broker()
    positions = broker.positions()
    assert len(positions) == 1
    # The adapter fixture explicitly declares per-share prices.
    assert positions[0].entry_price == pytest.approx(11.50)
    assert positions[0].quantity == 2


def test_every_order_is_reviewed_before_it_is_placed():
    broker, caller = build_broker()
    fill = broker.buy_to_open(make_quote(), 1, 12.15)
    assert fill is not None
    assert fill.order_id == "order-1"
    names = [name for name, _ in caller.calls]
    assert names.index("review_option_order") < names.index("place_option_order")


def test_dry_run_reviews_but_never_places():
    broker, caller = build_broker(dry_run=True)
    assert broker.buy_to_open(make_quote(), 1, 12.15) is None
    assert caller.calls_to("review_option_order")
    assert caller.calls_to("place_option_order") == []


def test_blocking_review_prevents_placement():
    broker, caller = build_broker()
    caller.responses["review_option_order"] = {"error": "insufficient buying power"}
    assert broker.buy_to_open(make_quote(), 1, 12.15) is None
    assert caller.calls_to("place_option_order") == []


def test_approval_mode_requires_a_yes():
    broker, caller = build_broker(require_approval=True, approval_callback=lambda p, r: False)
    assert broker.buy_to_open(make_quote(), 1, 12.15) is None
    assert caller.calls_to("place_option_order") == []

    broker2, caller2 = build_broker(require_approval=True, approval_callback=lambda p, r: True)
    assert broker2.buy_to_open(make_quote(), 1, 12.15) is not None
    assert caller2.calls_to("place_option_order")


def test_accepted_but_unfilled_order_is_not_reported_as_a_fill():
    broker, caller = build_broker()
    caller.responses["place_option_order"] = {"id": "order-2", "state": "queued"}
    assert broker.buy_to_open(make_quote(), 1, 12.15) is None


def test_orders_are_limit_and_day_only():
    broker, caller = build_broker()
    broker.buy_to_open(make_quote(), 1, 12.15)
    payload = caller.calls_to("place_option_order")[0]
    assert payload["order_type"] == "limit"
    assert payload["time_in_force"] == "gfd"
    assert payload["position_effect"] == "open"


def test_sell_to_close_uses_the_close_effect():
    broker, caller = build_broker()
    position = broker.positions()[0]
    broker.sell_to_close(position, make_quote(), 12.0)
    payload = caller.calls_to("place_option_order")[0]
    assert payload["side"] == "sell"
    assert payload["position_effect"] == "close"


def test_missing_canned_tool_raises_rather_than_silently_passing():
    caller = FakeToolCaller(responses={})
    with pytest.raises(McpError):
        caller.call("place_option_order", {})


# ---- duplicate-order protection -----------------------------------------


def contract() -> OptionContract:
    return OptionContract("AAPL", EXPIRY, 195.0, "call")


def test_same_intent_produces_the_same_id():
    a = client_order_id(contract(), Side.BUY, 1)
    b = client_order_id(contract(), Side.BUY, 1)
    assert a == b


def test_different_intent_produces_a_different_id():
    base = client_order_id(contract(), Side.BUY, 1)
    assert client_order_id(contract(), Side.BUY, 2) != base
    assert client_order_id(contract(), Side.SELL, 1) != base


def test_a_reserved_order_is_a_duplicate(tmp_path):
    reg = OrderRegistry(state_dir=tmp_path)
    oid = client_order_id(contract(), Side.BUY, 1)
    assert not reg.is_duplicate(oid)
    reg.reserve(oid, contract(), Side.BUY, 1, 12.15)
    assert reg.is_duplicate(oid)


def test_a_failed_order_may_be_retried(tmp_path):
    reg = OrderRegistry(state_dir=tmp_path)
    oid = client_order_id(contract(), Side.BUY, 1)
    reg.reserve(oid, contract(), Side.BUY, 1, 12.15)
    reg.mark(oid, OrderState.FAILED)
    assert not reg.is_duplicate(oid)


def test_the_registry_survives_a_restart(tmp_path):
    oid = client_order_id(contract(), Side.BUY, 1)
    OrderRegistry(state_dir=tmp_path).reserve(oid, contract(), Side.BUY, 1, 12.15)
    # The whole point: a crash between submission and response must not let the
    # next process buy the same contract again.
    assert OrderRegistry(state_dir=tmp_path).is_duplicate(oid)


def test_a_stale_pending_order_keeps_blocking(tmp_path):
    reg = OrderRegistry(state_dir=tmp_path, pending_ttl_minutes=0)
    oid = client_order_id(contract(), Side.BUY, 1)
    reg.reserve(oid, contract(), Side.BUY, 1, 12.15)
    # Age cannot prove an uncertain order was canceled.
    assert reg.is_duplicate(oid)
    assert reg.has_pending_for(contract().occ_symbol)


def test_pending_order_blocks_a_second_position_in_the_same_contract(tmp_path):
    reg = OrderRegistry(state_dir=tmp_path)
    reg.reserve(client_order_id(contract(), Side.BUY, 1), contract(), Side.BUY, 1, 12.15)
    assert reg.has_pending_for(contract().occ_symbol)
    assert not reg.has_pending_for("NOTHING")


def test_corrupt_registry_blocks_startup(tmp_path):
    (tmp_path / "orders.json").write_text("{ this is not json")
    with pytest.raises(RuntimeError, match="corrupt"):
        OrderRegistry(state_dir=tmp_path)


def test_position_reconstruction_skips_unparseable_rows():
    broker, caller = build_broker()
    caller.responses["get_option_positions"] = {"results": [{"chain_symbol": "AAPL"}]}
    assert broker.positions() == []


def test_short_positions_are_ignored():
    broker, caller = build_broker()
    caller.responses["get_option_positions"] = {
        "results": [
            {
                "chain_symbol": "AAPL",
                "expiration_date": EXPIRY.strftime("%Y-%m-%d"),
                "strike_price": "195.0",
                "option_type": "call",
                "quantity": "1",
                "type": "short",
                "average_price": "500.00",
            }
        ]
    }
    assert broker.positions() == []


def test_describe_order_is_human_readable():
    broker, _ = build_broker()
    payload = broker._order_payload(contract(), Side.BUY, 2, 12.15)
    text = RobinhoodMcpBroker.describe_order(payload, {"estimated_cost": 2430.0})
    assert "PROPOSED ORDER" in text
    assert "BUY 2x AAPL" in text
    assert "$2,430" in text


def test_engine_suppresses_a_duplicate_entry(tmp_path):
    """The end-to-end guarantee: a retried decision does not double the position."""
    from optionsagent.brokers.paper import PaperBroker
    from optionsagent.config import Config
    from optionsagent.engine import TradingEngine
    from optionsagent.marketdata.synthetic import SyntheticMarketData
    from optionsagent.portfolio import Portfolio

    cfg = Config(state_dir=str(tmp_path))
    market = SyntheticMarketData(seed=3)
    broker = PaperBroker(starting_equity=50_000, seed=2, miss_probability=0.0)
    engine = TradingEngine(
        config=cfg, data=market, broker=broker, portfolio=Portfolio(state_dir=tmp_path)
    )
    quote = next(
        q for q in market.option_chain("AAPL", 30, 45) if q.contract.right == "call" and q.mid > 2
    )

    first = engine._buy_with_reprice(quote, 1)
    second = engine._buy_with_reprice(quote, 1)
    assert first is not None
    assert second is None


def test_repricing_is_one_intent_not_several(tmp_path):
    """Walking the ladder must not trip the agent's own duplicate guard."""
    from optionsagent.brokers.paper import PaperBroker
    from optionsagent.config import Config
    from optionsagent.engine import TradingEngine
    from optionsagent.marketdata.synthetic import SyntheticMarketData
    from optionsagent.portfolio import Portfolio

    cfg = Config(state_dir=str(tmp_path))
    market = SyntheticMarketData(seed=3)
    engine = TradingEngine(
        config=cfg,
        data=market,
        broker=PaperBroker(starting_equity=50_000, seed=2, miss_probability=0.0),
        portfolio=Portfolio(state_dir=tmp_path),
    )
    quote = next(
        q for q in market.option_chain("AAPL", 30, 45) if q.contract.right == "call" and q.mid > 2
    )
    fill = engine._buy_with_reprice(quote, 1)
    # The first rung bids below the paper broker's fill price, so a fill here
    # proves a later rung ran rather than being suppressed as a duplicate.
    assert fill is not None
    assert fill.price > engine._entry_limit(quote)


def test_a_ladder_that_never_fills_is_released_for_the_next_loop(tmp_path):
    from optionsagent.brokers.paper import PaperBroker
    from optionsagent.config import Config
    from optionsagent.engine import TradingEngine
    from optionsagent.marketdata.synthetic import SyntheticMarketData
    from optionsagent.portfolio import Portfolio

    cfg = Config(state_dir=str(tmp_path))
    cfg.execution.max_slippage_pct = 0.0
    market = SyntheticMarketData(seed=3)
    engine = TradingEngine(
        config=cfg,
        data=market,
        broker=PaperBroker(starting_equity=50_000, seed=2, miss_probability=1.0),
        portfolio=Portfolio(state_dir=tmp_path),
    )
    quote = next(
        q for q in market.option_chain("AAPL", 30, 45) if q.contract.right == "call" and q.mid > 2
    )
    assert engine._buy_with_reprice(quote, 1) is None
    # The paper broker can prove nothing is working, so the reservation clears
    # rather than locking the contract out until the TTL expires.
    assert not engine.orders.has_pending_for(quote.contract.occ_symbol)


def test_reconcile_mismatch_halts_new_entries(tmp_path):
    from optionsagent.brokers.paper import PaperBroker
    from optionsagent.config import Config
    from optionsagent.engine import TradingEngine
    from optionsagent.marketdata.synthetic import SyntheticMarketData
    from optionsagent.portfolio import Portfolio

    cfg = Config(state_dir=str(tmp_path))
    portfolio = Portfolio(state_dir=tmp_path)
    portfolio.add(Position(contract=contract(), quantity=1, entry_price=10.0))
    engine = TradingEngine(
        config=cfg,
        data=SyntheticMarketData(seed=3),
        broker=PaperBroker(starting_equity=50_000, seed=2),
        portfolio=portfolio,
    )
    report = engine.run_once()
    assert "ledger-only" in report.blocked_reason
    assert report.opened == []
