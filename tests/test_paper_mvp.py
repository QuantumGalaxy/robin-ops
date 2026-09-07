"""Integration regressions for real schema adapters, feeds, recovery and local UI."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_safety_regressions import NOW, engine, quote

from optionsagent.health import Alerts
from optionsagent.lifecycle import Lifecycle, limit_tick
from optionsagent.mcp.client import FakeToolCaller
from optionsagent.mcp.robinhood import RobinhoodMcp
from optionsagent.models import Side
from optionsagent.recovery import repair
from optionsagent.reference_feed import IVArchive, last_completed, refresh_reference, session
from optionsagent.replay import ReplayData, load_frames
from optionsagent.runtime import RuntimeStore
from optionsagent.webserver import state


def test_actual_nested_quotes_and_instrument_parameters():
    caller = FakeToolCaller(
        responses={
            "get_equity_quotes": {
                "data": {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "200"}}]}
            },
            "get_option_quotes": lambda args: {
                "data": {"results": [{"quote": {"instrument_id": args["instrument_ids"][0]}}]}
            },
            "get_option_instruments": {"data": {"instruments": [{"id": "one"}]}},
        }
    )
    api = RobinhoodMcp(caller)
    assert api.equity_quote("AAPL")["last_trade_price"] == "200"
    assert api.option_quotes(["one"]) == [{"instrument_id": "one"}]
    api.option_instruments("AAPL", date(2026, 9, 18), "call")
    args = caller.calls_to("get_option_instruments")[0]
    assert args["chain_symbol"] == "AAPL" and args["expiration_dates"] == "2026-09-18"
    assert "symbol" not in args


def test_pagination_follows_cursor_without_fetching_arbitrary_next_url():
    c = FakeToolCaller(
        responses={
            "get_option_instruments": lambda a: {
                "data": {
                    "instruments": [{"id": a.get("cursor", "first")}],
                    **(
                        {}
                        if a.get("cursor")
                        else {"next": "https://untrusted.invalid/path?cursor=second"}
                    ),
                }
            }
        }
    )
    api = RobinhoodMcp(c)
    assert [r["id"] for r in api.option_instruments("AAPL")] == ["first", "second"]


def test_repeated_cursor_fails_closed():
    api = RobinhoodMcp(
        FakeToolCaller(
            responses={
                "get_option_instruments": {
                    "data": {"instruments": [], "next": "https://example.com/?cursor=loop"}
                }
            }
        )
    )
    with pytest.raises(ValueError, match="cursor"):
        api.option_instruments("AAPL")


def test_ambiguous_or_adjusted_chains_not_selected():
    for chains in [
        [{"symbol": "AAPL", "trade_value_multiplier": "10"}],
        [{"symbol": "AAPL"}, {"symbol": "AAPL"}],
    ]:
        c = FakeToolCaller(responses={"get_option_chains": {"data": {"chains": chains}}})
        assert RobinhoodMcp(c).option_chains("AAPL") == {}


def test_holiday_and_shortened_session_calendar():
    assert session(date(2026, 9, 7)) is None
    assert last_completed(datetime(2026, 9, 7, 16, tzinfo=UTC)) == date(2026, 9, 4)
    close = datetime.fromisoformat(session(date(2026, 11, 27))["close"])
    assert close.hour == 18  # 13:00 Eastern


def test_insufficient_or_future_iv_is_never_fabricated(tmp_path):
    archive = IVArchive(tmp_path / "iv.sqlite3")
    archive.add("AAPL", date(2026, 9, 4), 0.3, "fixture", NOW)
    assert archive.rank("AAPL", date(2026, 9, 4)) == (None, 1)
    with pytest.raises(ValueError):
        archive.add("AAPL", date(2026, 9, 9), 0.3, "fixture", NOW)


def test_feed_keeps_unknown_iv_and_collects_verified_bars(tmp_path):
    caller = FakeToolCaller(
        responses={
            "get_equity_historicals": {
                "data": {
                    "results": [
                        {
                            "symbol": "AAPL",
                            "bars": [
                                {
                                    "begins_at": "2026-09-04T13:30:00Z",
                                    "close_price": "200",
                                    "interpolated": False,
                                }
                            ],
                        }
                    ]
                }
            },
            "get_earnings_results": {"data": {"results": [{"report": {"date": "2026-10-29"}}]}},
            "get_option_chains": {"data": {"chains": []}},
            "get_equity_quotes": {
                "data": {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "200"}}]}
            },
        }
    )
    target = tmp_path / "reference.json"
    issues = refresh_reference(caller, ["AAPL"], target, tmp_path / "iv.sqlite3", NOW)
    data = json.loads(target.read_text())["symbols"]["AAPL"]
    assert data["daily_closes"] == [200] and data["daily_closes_as_of"] == "2026-09-04"
    assert data["earnings_checked"] and data["iv_rank"] is None
    assert any("IV history" in i for i in issues)


def test_cumulative_executions_are_idempotent_and_late_fill_retained(tmp_path):
    life = Lifecycle(tmp_path / "orders.sqlite3")
    assert life.reserve("intent", "explicit-account", "contract", "buy", 2)
    assert not life.reserve("intent", "explicit-account", "contract", "buy", 2)
    fill = {"id": "execution-1", "quantity": 1, "price": 5, "fee": 0.06}
    kw = dict(
        account="explicit-account", broker_id="broker-id", executions=[fill], source="fixture"
    )
    assert life.apply("intent", state="partially_filled", **kw)["filled"] == 1
    assert life.apply("intent", state="pending_cancelled", **kw)["remaining"] == 1
    assert life.apply("intent", state="cancelled", **kw)["state"] == "cancelled"
    kw["executions"].append({"id": "execution-2", "quantity": 1, "price": 5.1, "fee": 0.06})
    assert life.apply("intent", state="filled", **kw) == {
        "state": "filled",
        "filled": 2,
        "remaining": 0,
    }


def test_account_and_fill_mismatch_block_updates(tmp_path):
    life = Lifecycle(tmp_path / "orders.sqlite3")
    life.reserve("i", "account", "c", "buy", 1)
    for account, qty in [("wrong", 1), ("account", 2)]:
        with pytest.raises(ValueError):
            life.apply(
                "i",
                account=account,
                broker_id="b",
                state="filled",
                executions=[{"id": "e", "quantity": qty, "price": 5, "fee": 0.1}],
                source="test",
            )
    assert (
        life.apply(
            "i", account="account", broker_id="b", state="queued", executions=[], source="test"
        )["filled"]
        == 0
    )


def test_ticks_round_protectively():
    assert limit_tick(1.03, 0.05, "buy") == 1
    assert limit_tick(1.03, 0.05, "sell") == 1.05


def test_alerts_deduplicate_and_acknowledge(tmp_path):
    a = Alerts(tmp_path)
    assert a.set("key", "message")
    assert not a.set("key", "message")
    a.ack("key")
    assert a.list()[0]["acknowledged"] == 1
    a.set("key", "new issue")
    assert a.list()[0]["acknowledged"] == 0
    a.clear("key")
    assert a.list()[0]["active"] == 0


def test_repair_preview_does_not_mutate_and_cannot_clear_pending(tmp_path):
    e = engine(tmp_path)
    store = RuntimeStore(tmp_path)
    q = quote()
    e.orders.reserve("pending", q.contract, Side.BUY, 1, 5)
    store.save(e)
    evidence = tmp_path / "review.json"
    evidence.write_text(
        json.dumps(
            {
                "kind": "resolve_order",
                "source": "verified paper journal",
                "reason": "no execution",
                "order_id": "pending",
                "resolution": "verified_no_execution",
                "executed_quantity": 0,
                "working": False,
            }
        )
    )
    before = store.read()
    assert repair(tmp_path, evidence)["applied"] is False
    assert store.read() == before
    assert repair(tmp_path, evidence, True)["applied"]
    assert store.read()["orders"]["pending"]["state"] == "failed"


def test_settlement_repair_requires_matched_expired_contract(tmp_path):
    e = engine(tmp_path)
    store = RuntimeStore(tmp_path)
    store.save(e)
    p = tmp_path / "review.json"
    p.write_text(
        json.dumps(
            {
                "kind": "settlement",
                "source": "official close",
                "reason": "missed expiry exit",
                "contract": "missing",
                "settlement_per_share": 5,
                "expiry": "2025-01-01",
            }
        )
    )
    with pytest.raises(ValueError, match="Matched"):
        repair(tmp_path, p, True)
    assert store.read()["trades"] == []


def test_replay_rejects_future_reference_and_unsorted_frames(tmp_path):
    frame = {
        "at": NOW.isoformat(),
        "source": "fixture",
        "reference": {
            "as_of": (NOW + timedelta(days=1)).isoformat(),
            "source": "fixture",
            "symbols": {},
        },
        "quotes": [],
    }
    with pytest.raises(ValueError, match="Future"):
        ReplayData().advance(frame)
    p = tmp_path / "data.jsonl"
    p.write_text(json.dumps(frame) + "\n" + json.dumps(frame))
    with pytest.raises(ValueError, match="increasing"):
        load_frames(p)


def test_dashboard_state_has_no_credentials_and_reports_pause(tmp_path):
    e = engine(tmp_path)
    store = RuntimeStore(tmp_path)
    store.save(e)
    Path(e.config.risk.kill_switch_file).touch()
    result = state(e.config)
    assert result["paused"] and result["pending"] == 0
    assert "access_token" not in json.dumps(result)


def test_repair_rejects_newer_order_journal(tmp_path):
    e = engine(tmp_path)
    RuntimeStore(tmp_path).save(e)
    e.orders.reserve("uncertain", quote().contract, Side.BUY, 1, 5)
    p = tmp_path / "review.json"
    p.write_text(json.dumps({"kind": "clear_reconcile", "source": "test", "reason": "review"}))
    with pytest.raises(ValueError, match="newer"):
        repair(tmp_path, p, True)


def test_private_dashboard_auth_origin_and_controls(tmp_path):
    import http.client
    from threading import Thread

    from optionsagent.webserver import make_server

    e = engine(tmp_path)
    RuntimeStore(tmp_path).save(e)
    server, token = make_server(e.config, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request("GET", "/api/state")
        response = client.getresponse()
        assert response.status == 403
        response.read()
        auth = {"Authorization": "Bearer " + token}
        client.request("GET", "/api/state", headers={**auth, "Origin": "https://evil.example"})
        response = client.getresponse()
        assert response.status == 403
        response.read()
        client.request("GET", "/api/state", headers=auth)
        response = client.getresponse()
        assert response.status == 200 and json.loads(response.read())["pending"] == 0
        for action, paused in [("pause", True), ("resume", False)]:
            client.request("POST", "/api/control", json.dumps({"action": action}), auth)
            response = client.getresponse()
            assert response.status == 200
            response.read()
            assert Path(e.config.risk.kill_switch_file).exists() is paused
        client.request("POST", "/api/control", "[]", auth)
        response = client.getresponse()
        assert response.status == 400
        response.read()
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_verified_paper_settlement_accounts_for_costs(tmp_path):
    from optionsagent.models import OptionContract, Position

    e = engine(tmp_path)
    contract = OptionContract("AAPL", date(2025, 1, 17), 100, "call")
    holding = Position(contract, 1, 5, entry_fees=0.1)
    e.portfolio.add(holding)
    e.broker._positions[contract.occ_symbol] = holding
    e.broker.cash -= 500.1
    store = RuntimeStore(tmp_path)
    store.save(e)
    before = store.read()["paper"]["cash"]
    p = tmp_path / "review.json"
    p.write_text(
        json.dumps(
            {
                "kind": "settlement",
                "source": "verified fixture",
                "reason": "expired holding",
                "contract": contract.occ_symbol,
                "expiry": "2025-01-17",
                "settlement_per_share": 3,
            }
        )
    )
    repair(tmp_path, p, True)
    saved = store.read()
    assert saved["paper"]["cash"] == pytest.approx(before + 300)
    assert saved["realized_pnl"] == pytest.approx(-200.1)
    assert not saved["positions"] and not saved["paper"]["positions"]


def test_quote_capture_includes_held_contract_beyond_entry_window(tmp_path):
    from dataclasses import replace

    from optionsagent.models import Position
    from optionsagent.replay import capture

    e = engine(tmp_path)
    held = replace(
        quote(), contract=replace(quote().contract, expiry=NOW.date() + timedelta(days=5))
    )
    e.portfolio.add(Position(held.contract, 1, 5))
    RuntimeStore(tmp_path).save(e)
    ref = tmp_path / "reference.json"
    ref.write_text(json.dumps({"as_of": NOW.isoformat(), "source": "fixture", "symbols": {}}))
    e.config.reference_data_file = str(ref)

    class Data:
        def option_chain(self, *args):
            return []

        def quote(self, *args):
            return held

    target = tmp_path / "capture.jsonl"
    assert capture(Data(), e.config, target, NOW) == 1
    assert (
        json.loads(target.read_text())["quotes"][0]["contract"]["occ_symbol"]
        == held.contract.occ_symbol
    )
