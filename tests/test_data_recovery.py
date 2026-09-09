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


def test_intraday_fallback_requires_every_completed_session_bar():
    from datetime import timedelta

    import pytest

    from optionsagent.reference_feed import completed_intraday_close

    opening = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
    now = datetime(2026, 9, 8, 21, tzinfo=UTC)
    bars = [
        dict(
            begins_at=(opening + timedelta(minutes=5 * i)).isoformat(),
            open_price=200,
            close_price=201,
            high_price=202,
            low_price=199,
            volume=100,
            session="reg",
        )
        for i in range(78)
    ]
    caller = Mock()
    caller.call.return_value = {"data": {"results": [{"symbol": "AAPL", "bars": bars}]}}
    assert completed_intraday_close(caller, "AAPL", now.date(), now) == 201
    bars[-1]["interpolated"] = True
    with pytest.raises(ValueError, match="Incomplete"):
        completed_intraday_close(caller, "AAPL", now.date(), now)
    bars[-1].pop("interpolated")
    bars.pop(20)
    with pytest.raises(ValueError, match="Incomplete"):
        completed_intraday_close(caller, "AAPL", now.date(), now)
    with pytest.raises(ValueError, match="not completed"):
        completed_intraday_close(caller, "AAPL", now.date(), opening)


def test_history_alert_is_not_an_exit_failure_and_recovery_keeps_real_errors(tmp_path):
    from optionsagent.health import Alerts

    a = Alerts(tmp_path)
    a.monitoring_result(["AAPL: completed daily bars unavailable"])
    active = {r["key"] for r in a.list() if r["active"]}
    assert "entry_history" in active and "monitoring" not in active
    a.set("monitoring", "NVDA: exit quote unavailable", "critical")
    a.reference_recovered()
    assert any(r["key"] == "monitoring" and r["active"] for r in a.list())
    a.set("monitoring", "AAPL: completed daily bars unavailable", "critical")
    a.reference_recovered()
    assert not any(r["active"] for r in a.list())


def test_pending_dolthub_is_explicit_and_partial_update_stays_pending(tmp_path):
    from optionsagent.iv_history import refresh_latest

    now = datetime(2026, 9, 8, 21, tzinfo=UTC)
    pending = refresh_latest(tmp_path, ["AAPL", "MA"], now, fetcher=lambda *a: {"rows": []})
    assert pending["status"] == "pending" and pending["missing_symbols"] == ["AAPL", "MA"]
    partial = refresh_latest(
        tmp_path,
        ["AAPL", "MA"],
        now,
        fetcher=lambda *a: {
            "rows": [{"act_symbol": "AAPL", "date": "2026-09-08", "iv_current": "0.25"}]
        },
    )
    assert partial["status"] == "pending" and partial["missing_symbols"] == ["MA"]
