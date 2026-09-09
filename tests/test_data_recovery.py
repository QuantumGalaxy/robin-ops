import json
from datetime import UTC, datetime
from unittest.mock import Mock

from optionsagent.mcp.client import HttpToolCaller
from optionsagent.reference_feed import refresh_reference


def test_failed_refresh_preserves_verified_history_without_refreshing_date(tmp_path):
    target = tmp_path / "reference.json"
    target.write_text(
        json.dumps(
            {
                "as_of": "2026-09-04T21:00:00Z",
                "source": "fixture",
                "symbols": {
                    "AAPL": {
                        "daily_closes": [200, 201],
                        "daily_closes_as_of": "2026-09-04",
                        "earnings_checked": True,
                        "earnings": "2026-10-20",
                    }
                },
            }
        )
    )
    caller = Mock()
    caller.call.side_effect = RuntimeError("unavailable")
    issues = refresh_reference(
        caller,
        ["AAPL"],
        target,
        tmp_path / "iv.sqlite3",
        datetime(2026, 9, 8, 21, tzinfo=UTC),
        collect_iv=False,
    )
    row = json.loads(target.read_text())["symbols"]["AAPL"]
    assert issues and row["daily_closes"] == [200, 201]
    assert row["daily_closes_as_of"] == "2026-09-04"
    assert not row["earnings_checked"]


def test_read_only_retry_is_bounded_and_never_retries_order_review():
    import pytest

    from optionsagent.mcp.client import McpError

    caller = HttpToolCaller(token="test-only")
    caller._ensure_initialized = Mock()
    caller._request = Mock(side_effect=[{"isError": True}, {"content": []}])
    caller.call("get_option_quotes", {"instrument_ids": ["x"]})
    assert caller._request.call_count == 2
    caller._request = Mock(return_value={"isError": True})
    with pytest.raises(McpError):
        caller.call("get_option_quotes", {})
    assert caller._request.call_count == 2
    caller._request.reset_mock()
    with pytest.raises(McpError):
        caller.call("review_option_order", {})
    assert caller._request.call_count == 1


def test_symbol_scan_failure_does_not_skip_later_stocks(tmp_path):
    from optionsagent.brokers.paper import PaperBroker
    from optionsagent.config import Config
    from optionsagent.engine import LoopReport, TradingEngine
    from optionsagent.portfolio import Portfolio

    cfg = Config(state_dir=str(tmp_path), universe={"symbols": ["AAPL", "MSFT"]})
    engine = TradingEngine(cfg, Mock(), PaperBroker(), Portfolio(tmp_path))
    engine._scan_symbol = Mock(side_effect=[RuntimeError("quote unavailable"), None])
    report = LoopReport(datetime.now(UTC), 25000)
    engine._scan_entries(datetime.now(UTC), report, dry=True)
    assert engine._scan_symbol.call_count == 2
    assert report.errors == ["AAPL: entry scan failed"]


def test_interpolated_latest_bar_does_not_erase_real_history(tmp_path):
    from optionsagent.mcp.client import FakeToolCaller

    caller = FakeToolCaller(
        responses={
            "get_equity_historicals": {
                "data": {
                    "results": [
                        {
                            "symbol": "AAPL",
                            "bars": [
                                {"begins_at": "2026-09-04T00:00:00Z", "close_price": "200"},
                                {
                                    "begins_at": "2026-09-08T00:00:00Z",
                                    "close_price": "200",
                                    "interpolated": True,
                                },
                            ],
                        }
                    ]
                }
            }
        }
    )
    path = tmp_path / "reference.json"
    issues = refresh_reference(
        caller,
        ["AAPL"],
        path,
        tmp_path / "iv.sqlite3",
        datetime(2026, 9, 8, 22, tzinfo=UTC),
        collect_iv=False,
    )
    row = json.loads(path.read_text())["symbols"]["AAPL"]
    assert row["daily_closes"] == [200]
    assert row["daily_closes_as_of"] == "2026-09-04"
    assert any("daily bar pending" in i for i in issues)


def test_completed_today_is_accepted_but_stale_and_future_bars_are_not(monkeypatch):
    from datetime import date

    from optionsagent.marketdata.robinhood_mcp import RobinhoodMcpMarketData

    monkeypatch.setattr("optionsagent.reference_feed.last_completed", lambda now: date(2026, 9, 8))
    data = object.__new__(RobinhoodMcpMarketData)
    row = {"daily_closes": [200], "daily_closes_as_of": "2026-09-08"}
    data._reference = lambda: {"symbols": {"AAPL": row}}
    assert data.historical_closes("AAPL") == [200]
    for day in ["2026-09-04", "2026-09-09"]:
        row["daily_closes_as_of"] = day
        assert data.historical_closes("AAPL") == []
