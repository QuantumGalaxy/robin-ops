import csv
import sqlite3
from datetime import UTC, date, datetime

import pytest

from optionsagent.iv_history import DATABASE, history_status, import_history
from optionsagent.reference_feed import IVArchive, calendar

NOW = datetime(2026, 9, 7, 16, tzinfo=UTC)


def write_csv(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "date", "iv"])
        writer.writerows(rows)
    return path


def test_import_quarantines_bad_dates_and_does_not_populate_execution(tmp_path):
    path = write_csv(
        tmp_path / "source.csv",
        [
            ["MA", "2026-09-03", 0.25],
            ["MA", "2026-01-01", 0.3],
            ["MA", "2026-09-08", 0.3],
            ["MA", "2026-09-02", "nan"],
            ["QQQ", "2026-09-03", 0.2],
        ],
    )
    result = import_history(path, tmp_path, ["MA"], NOW)
    assert (result["accepted"], result["rejected"]) == (1, 4)
    assert not (tmp_path / "iv.sqlite3").exists()
    assert result["execution_enabled"] is False
    status = history_status(tmp_path, ["MA"], NOW)
    assert status["symbols"][0]["latest"] == "2026-09-03"
    assert not status["symbols"][0]["fresh"]
    assert "2026-09-04" in status["symbols"][0]["missing_sessions"]


def test_repeat_overlap_and_conflicts_preserve_original(tmp_path):
    path = write_csv(tmp_path / "one.csv", [["MA", "2026-09-03", 0.25]])
    import_history(path, tmp_path, ["MA"], NOW)
    assert import_history(path, tmp_path, ["MA"], NOW)["already_imported"]
    other = write_csv(
        tmp_path / "two.csv",
        [
            ["MA", "2026-09-03", 0.25],
            ["MA", "2026-09-03", 0.4],
            ["MA", "2026-09-04", 0.26],
        ],
    )
    report = import_history(other, tmp_path, ["MA"], NOW)
    assert (report["accepted"], report["duplicates"], report["rejected"]) == (1, 1, 1)
    with sqlite3.connect(tmp_path / DATABASE) as db:
        assert (
            db.execute("SELECT iv FROM observations WHERE day='2026-09-03'").fetchone()[0] == 0.25
        )
    assert history_status(tmp_path, ["MA"], NOW)["symbols"][0]["fresh"]


def test_bad_header_does_not_create_archive(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("ticker,price\nMA,50\n")
    with pytest.raises(ValueError, match="columns"):
        import_history(path, tmp_path, ["MA"], NOW)
    assert not (tmp_path / DATABASE).exists()


def test_excel_dates_and_formula_rejection(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["act_symbol", "date", "iv_current"])
    sheet.append(["MA", datetime(2026, 9, 3), 0.25])
    sheet.append(["MA", datetime(2026, 9, 4), "=1/4"])
    path = tmp_path / "history.xlsx"
    book.save(path)
    report = import_history(path, tmp_path, ["MA"], NOW)
    assert (report["accepted"], report["rejected"]) == (1, 1)


def test_old_history_cannot_satisfy_execution_minimum(tmp_path):
    archive = IVArchive(tmp_path / "iv.sqlite3")
    days = calendar().sessions_in_range("2023-01-03", "2023-12-29")[:210]
    with sqlite3.connect(archive.path) as db:
        db.executemany(
            "INSERT INTO iv VALUES (?,?,?,?)",
            [("MA", d.date().isoformat(), 0.2 + i / 1000, "fixture") for i, d in enumerate(days)],
        )
    archive.add("MA", date(2026, 9, 4), 0.3, "fixture", NOW)
    assert archive.rank("MA", date(2026, 9, 4)) == (None, 1)


def seed_history(tmp_path, latest_value=0.3):
    from optionsagent.iv_history import SOURCE, connect

    days = calendar().sessions_in_range("2025-01-01", "2026-09-04")[-220:]
    with connect(tmp_path / DATABASE) as db:
        db.executemany(
            "INSERT INTO observations VALUES (?,?,?,?,?)",
            [
                ("MA", d.date().isoformat(), 0.2 + i / 1000, SOURCE, "fixture")
                for i, d in enumerate(days)
            ],
        )
        db.execute("UPDATE observations SET iv=? WHERE day='2026-09-04'", (latest_value,))


def test_rank_uses_same_source_latest_day_and_rejects_stale_or_mixed(tmp_path):
    from optionsagent.iv_history import paper_iv_rank

    seed_history(tmp_path)
    rank = paper_iv_rank(tmp_path, "MA", NOW)
    assert rank["rank"] == pytest.approx((0.3 - 0.2) / (0.418 - 0.2))
    assert rank["days"] == 220
    assert paper_iv_rank(tmp_path, "MA", datetime(2026, 9, 9, 14, tzinfo=UTC))["rank"] is None
    with sqlite3.connect(tmp_path / DATABASE) as db:
        db.execute("UPDATE observations SET source='other' WHERE day='2026-09-04'")
    assert paper_iv_rank(tmp_path, "MA", NOW)["rank"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "live_auto"},
        {"mode": "live_approval"},
        {"mode": "scan"},
        {"data_provider": "synthetic"},
        {"entry": {"require_iv_rank": False}},
        {"broker": {"kind": "robinhood_mcp"}},
    ],
)
def test_experimental_iv_cannot_be_enabled_outside_paper(overrides):
    from optionsagent.config import Config

    with pytest.raises(ValueError):
        Config(
            **{"data_provider": "robinhood_mcp", "paper_iv_history_experiment": True, **overrides}
        )


def test_public_refresh_imports_current_day_and_then_uses_cache(tmp_path):
    from optionsagent.iv_history import refresh_latest

    calls = []

    def fetch(symbols, completed):
        calls.append((symbols, completed))
        return {
            "query_execution_status": "Success",
            "rows": [
                {"act_symbol": "MA", "date": "2026-09-04", "iv_current": "0.25"},
            ],
        }

    refresh_latest(tmp_path, ["MA"], NOW, fetcher=fetch)
    refresh_latest(tmp_path, ["MA"], NOW, fetcher=fetch)
    assert len(calls) == 1
    assert history_status(tmp_path, ["MA"], NOW)["symbols"][0]["fresh"]


@pytest.mark.parametrize("status", ["Error", "RowLimit"])
def test_public_api_partial_results_are_rejected(monkeypatch, status):
    import io
    import json

    from optionsagent.iv_history import fetch_latest

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: io.BytesIO(
            json.dumps(
                {
                    "query_execution_status": status,
                    "rows": [
                        {"act_symbol": "MA", "date": "2026-09-04", "iv_current": "0.25"},
                    ],
                }
            ).encode()
        ),
    )
    with pytest.raises(ValueError, match="incomplete"):
        fetch_latest(["MA"], date(2026, 9, 4))


@pytest.mark.parametrize("latest,expect_screen", [(0.3, True), (0.8, False)])
def test_paper_engine_actually_applies_experimental_filter(tmp_path, latest, expect_screen):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from optionsagent.brokers.paper import PaperBroker
    from optionsagent.config import Config
    from optionsagent.engine import LoopReport, TradingEngine
    from optionsagent.portfolio import Portfolio

    seed_history(tmp_path, latest)
    cfg = Config(
        state_dir=str(tmp_path), data_provider="robinhood_mcp", paper_iv_history_experiment=True
    )
    data = Mock()
    data.option_chain.return_value = [object()]
    data.earnings_known.return_value = True
    data.next_earnings_date.return_value = None
    screener = Mock()
    screener.screen.return_value = []
    engine = TradingEngine(
        cfg,
        data,
        PaperBroker(),
        Portfolio(tmp_path),
        screener=screener,
        signal=SimpleNamespace(direction=lambda *args: ("call", 0.7)),
    )
    engine._usable = lambda *args: True
    data.option_chain.return_value = [SimpleNamespace(contract=SimpleNamespace(symbol="MA"))]
    report = LoopReport(NOW, 25000)
    engine._scan_symbol("MA", NOW, report, True, 25000)
    assert screener.screen.called == expect_screen
    if expect_screen:
        assert screener.screen.call_args.kwargs["iv_rank"] == pytest.approx(0.1 / 0.218)
    else:
        assert "exceeds" in report.skipped[0]
    data.iv_rank.assert_not_called()
    assert not engine.portfolio.positions
