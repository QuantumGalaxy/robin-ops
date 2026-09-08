from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from optionsagent.config import Config
from optionsagent.engine import LoopReport
from optionsagent.models import OptionContract, OptionQuote
from optionsagent.paper_lab import PaperLab, SharedReadings, comparison_status, experiment_configs
from optionsagent.strategy.sizing import size_position


def base(tmp_path):
    return Config(
        data_provider="robinhood_mcp",
        state_dir=str(tmp_path),
        reference_data_file=str(tmp_path / "reference.json"),
        universe={"symbols": ["AAPL", "SPY"]},
        risk={"kill_switch_file": str(tmp_path / "KILL")},
    )


class Feed:
    def historical_closes(self, symbol, days=90):
        return [100 + i * 0.5 + (i % 3) * 0.1 for i in range(90)]

    def is_market_open(self):
        return False


def test_isolation_restart_shared_pause_and_same_premium_budget(tmp_path):
    cfg = base(tmp_path)
    configs = experiment_configs(cfg)
    assert len({c.state_dir for c in configs.values()}) == 3
    assert all(c.risk.kill_switch_file == cfg.risk.kill_switch_file for c in configs.values())
    assert all(c.iv_history_state_dir == str(tmp_path) for c in configs.values())
    q = OptionQuote(
        OptionContract("AAPL", date(2026, 9, 25), 195, "call"),
        bid=4.8,
        ask=4.9,
        underlying_price=200,
    )
    sizes = [
        size_position(q, 25000, 25000, 0, c.sizing, c.exit.stop_loss_pct).contracts
        for c in configs.values()
    ]
    assert sizes == [2, 2, 2]
    lab = PaperLab(cfg, Feed())
    try:
        lab.engines["stop25-v1"].broker.cash = 24500
        lab.run_once(datetime(2026, 9, 7, 18, tzinfo=UTC))
        assert lab.engines["control-v1"].broker.cash == 25000
        with pytest.raises(RuntimeError):
            PaperLab(cfg, Feed())
        Path(cfg.risk.kill_switch_file).touch()
        assert all(e.risk.kill_switch_engaged() for e in lab.engines.values())
    finally:
        lab.close()
    assert not (tmp_path / "runtime.sqlite3").exists()  # Original account untouched.
    restored = PaperLab(cfg, Feed())
    try:
        assert restored.engines["stop25-v1"].broker.cash == 24500
        result = comparison_status(tmp_path)
        assert len(result["profiles"]) == 3
        stop = next(p for p in result["profiles"] if p["key"] == "stop25-v1")
        assert stop["equity"] == 24500 and stop["max_drawdown"] == pytest.approx(0.02)
        assert stop["expectancy"] is None and stop["profit_factor"] is None
    finally:
        restored.close()
    changed = cfg.model_copy(deep=True)
    changed.exit.stop_loss_pct = -0.4
    with pytest.raises(ValueError, match="rules changed"):
        PaperLab(changed, Feed())


@pytest.mark.parametrize("market", ["put", None])
def test_market_alignment_blocks_opposite_or_missing_market_signal(tmp_path, market):
    lab = PaperLab(base(tmp_path), Feed())
    try:
        e = lab.engines["market-v1"]
        e.signal = Mock()
        e.signal.direction.side_effect = lambda symbol, history: (
            ("call", 0.5) if symbol == "AAPL" else (market, 0.5)
        )
        report = LoopReport(at=datetime.now(UTC), equity=25000)
        e._scan_symbol("AAPL", datetime.now(UTC), report, False, 25000)
        assert "SPY direction does not confirm" in report.skipped[0]
    finally:
        lab.close()


def test_quote_cache_returns_independent_values(tmp_path):
    provider = Mock()
    provider.historical_closes.return_value = [1, 2, 3]
    shared = SharedReadings(provider, tmp_path)
    shared.historical_closes("AAPL").append(99)
    assert shared.historical_closes("AAPL") == [1, 2, 3]
    assert provider.historical_closes.call_count == 1
    shared.clear()
    shared.historical_closes("AAPL")
    assert provider.historical_closes.call_count == 2


def test_lab_rejects_nonpaper_and_missing_market_symbol(tmp_path):
    with pytest.raises(ValueError):
        experiment_configs(Config())
    with pytest.raises(ValueError, match="SPY"):
        Config(entry={"require_market_alignment": True}, universe={"symbols": ["AAPL"]})
    cfg = base(tmp_path).model_copy(update={"mode": "live_auto"})
    with pytest.raises(ValueError):
        experiment_configs(cfg)


def test_comparison_closed_trade_metrics(tmp_path):
    import json
    import sqlite3

    lab = PaperLab(base(tmp_path), Feed())
    lab.close()
    path = tmp_path / "experiments/control-v1/runtime.sqlite3"
    with sqlite3.connect(path) as db:
        body = json.loads(db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()[0])
        body["trades"] = [{"pnl": 100}, {"pnl": -50}, {"pnl": 0}]
        body["realized_pnl"] = 50
        db.execute("UPDATE checkpoint SET body=? WHERE id=1", (json.dumps(body),))
    control = comparison_status(tmp_path)["profiles"][0]
    assert control["closed_trades"] == 3
    assert control["win_rate"] == pytest.approx(1 / 3)
    assert control["average_win"] == 100
    assert control["average_loss"] == -50
    assert control["expectancy"] == pytest.approx(50 / 3)
    assert control["profit_factor"] == 2
