"""Second-review regressions: invalid input, crash recovery and transport failures."""

import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_mcp_and_orders import build_market_data
from test_safety_regressions import NOW, engine, quote

from optionsagent.config import Config
from optionsagent.engine import LoopReport
from optionsagent.marketdata.reference import ReferenceSnapshot
from optionsagent.mcp.client import HttpToolCaller, McpError, _read_sse
from optionsagent.models import OptionContract, Position, Side
from optionsagent.orders import OrderState
from optionsagent.runtime import RuntimeStore
from optionsagent.strategy.sizing import size_position


@pytest.mark.parametrize(
    "override",
    [
        {"sizing": {"risk_per_trade_pct": -0.1}},
        {"risk": {"daily_loss_limit_pct": 2}},
        {"execution": {"poll_interval_seconds": 0}},
        {"execution": {"entry_window_end": "99:99"}},
        {"exit": {"trailing_floor_pct": 0.2}},
        {"entry": {"min_volume": -1}},
        {"broker": {"starting_equity": float("nan")}},
        {"universe": {"symbols": []}},
        {"entry": {"misspelled_filter": 1}},
    ],
)
def test_invalid_configuration_cannot_start(override):
    with pytest.raises(ValidationError):
        Config(**override)


def test_fee_can_make_an_exact_budget_contract_unaffordable():
    q = quote()
    q.bid, q.ask = 9.9, 10
    assert size_position(q, 100000, 100000, 0, Config().sizing).contracts == 0


def test_paper_urgent_sell_still_obeys_limit_and_contract_identity(tmp_path):
    e = engine(tmp_path)
    q = quote()
    e.broker.buy_to_open(q, 1, q.ask)
    held = e.broker.positions()[0]
    cash = e.broker.cash
    assert e.broker.sell_to_close(held, q, q.ask + 1, urgent=True) is None
    q.contract = OptionContract("MSFT", q.contract.expiry, 195, "call")
    assert e.broker.sell_to_close(held, q, q.bid, urgent=True) is None
    assert e.broker.cash == cash


def test_bad_held_quote_blocks_entries_without_inventing_a_close(tmp_path):
    e = engine(tmp_path)
    q = quote()
    e.broker.buy_to_open(q, 1, q.ask)
    e.portfolio.add(Position(q.contract, 1, 5, opened_at=NOW))
    e._quote_for = lambda p: None
    e._scan_entries = lambda *a: pytest.fail("entries should be blocked")
    report = e.run_once(NOW)
    assert "held quote missing" in report.blocked_reason
    assert not e.portfolio.trades


def test_unverified_ledger_position_is_never_settled(tmp_path):
    e = engine(tmp_path)
    c = OptionContract("AAPL", NOW.date() - timedelta(days=1), 100, "call")
    e.portfolio.add(Position(c, 1, 5))
    report = e.run_once(NOW)
    assert "ledger-only" in report.blocked_reason
    assert c.occ_symbol in e.portfolio.positions
    assert not e.portfolio.trades


def test_real_quote_paper_expiry_needs_verified_settlement(tmp_path):
    e = engine(tmp_path)
    e.config.data_provider = "robinhood_mcp"
    c = OptionContract("AAPL", NOW.date() - timedelta(days=1), 100, "call")
    e.portfolio.add(Position(c, 1, 5))
    e.broker._positions[c.occ_symbol] = Position(c, 1, 5)
    e._settle_expirations(NOW, LoopReport(NOW, 25000))
    assert "verified expiration settlement" in e.reconcile_halt
    assert not e.portfolio.trades


@pytest.mark.parametrize("existing", [False, True])
def test_newer_crash_window_intent_survives_checkpoint_restore(tmp_path, existing):
    e = engine(tmp_path)
    q = quote()
    store = RuntimeStore(tmp_path)
    if existing:
        e.orders.reserve("intent", q.contract, Side.BUY, 1, 5)
        e.orders.mark("intent", OrderState.FAILED)
    store.save(e)
    e.orders.reserve("intent", q.contract, Side.BUY, 1, 5)
    e.orders.mark("intent", OrderState.FILLED, broker_order_id="uncheckpointed-fill")
    restarted = engine(tmp_path)
    store.restore(restarted)
    assert restarted.orders.pending()[0].client_order_id == "intent"
    assert "recovery required" in restarted.orders.pending()[0].detail
    assert not restarted.portfolio.positions


def test_missing_underlying_timestamp_does_not_become_fresh_quote():
    data, caller = build_market_data()
    caller.responses["get_equity_quotes"] = {"symbol": "AAPL", "last_trade_price": 200}
    assert data.underlying_price("AAPL") is None


def test_future_option_timestamp_is_not_masked_by_fresh_underlying():
    data, _ = build_market_data()
    data.underlying_price("AAPL")
    q = quote()
    row = {"bid": 5, "ask": 5.1, "updated_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    assert data._build_quote(row, q.contract, 200) is None


@pytest.mark.parametrize(
    "symbol",
    [
        {"daily_closes": [200, 201]},
        {"iv_rank": float("nan")},
        {"earnings_checked": "false"},
        {"earnings_checked": True},
        {"iv_history_days": 2.5},
    ],
)
def test_reference_feed_rejects_ambiguous_metadata(symbol):
    with pytest.raises(ValidationError):
        ReferenceSnapshot.model_validate(
            {"as_of": NOW, "source": "test", "symbols": {"AAPL": symbol}}
        )


def test_sse_stops_at_matching_response_without_waiting_for_disconnect():
    def stream():
        yield b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n'
        yield b"\n"
        yield b'data: {"jsonrpc":"2.0", "id":7,\n'
        yield b'data: "result":{"ok":true}}\n'
        yield b"\n"
        pytest.fail("client waited past the response")

    assert _read_sse(stream(), 7)["result"] == {"ok": True}


def test_sse_disconnect_does_not_invent_success():
    with pytest.raises(McpError, match="without the requested response"):
        _read_sse(io.BytesIO(b'data: {"id":8,"result":{}}\n\n'), 7)


def test_http_allowlist_blocks_alternative_write_names_and_hides_token():
    c = HttpToolCaller(token="secret-for-test")
    assert "secret-for-test" not in repr(c)
    for name in ("submit_order", "transfer_cash", "update_option_order"):
        with pytest.raises(McpError, match="disabled"):
            c.call(name, {})


def test_tool_list_follows_pagination():
    c = HttpToolCaller(token="test")
    c._initialized = True
    calls = []

    def request(method, params):
        calls.append(params)
        return (
            {"tools": [{"name": "b"}]}
            if params
            else {"tools": [{"name": "a"}], "nextCursor": "page2"}
        )

    c._request = request
    assert [t["name"] for t in c.list_tools()] == ["a", "b"]
    assert calls == [{}, {"cursor": "page2"}]


def test_http_checks_response_id_and_sends_negotiated_version(monkeypatch):
    c = HttpToolCaller(token="test")
    c._protocol_version = "2025-06-18"

    def open_request(req, timeout, **kwargs):
        assert req.get_header("Mcp-protocol-version") == "2025-06-18"
        response = io.BytesIO(json.dumps({"id": 99, "result": {}}).encode())
        response.headers = {"Content-Type": "application/json"}
        return response

    monkeypatch.setattr("optionsagent.mcp.client.open_mcp", open_request)
    with pytest.raises(McpError, match="ID does not match"):
        c._request("tools/list")
