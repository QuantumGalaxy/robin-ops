import json

from optionsagent.daily_report import archive_reports, daily_report
from optionsagent.runtime import RuntimeStore


def test_new_york_day_boundaries_and_decision_vs_fill(tmp_path):
    store = RuntimeStore(tmp_path)
    with store.connect() as db:
        for at, kind, body in [
            ("2026-09-08 03:59:59", "loop", {"equity": 25000, "skipped": ["excluded"]}),
            (
                "2026-09-08 04:00:00",
                "entry_decision",
                {
                    "contract": "AAPL call",
                    "quantity": 1,
                    "bid": 4,
                    "ask": 4.1,
                    "reasons": ["IV accepted"],
                    "greeks": {"theta": -0.05},
                    "iv_rank": 0.4,
                },
            ),
            (
                "2026-09-08 15:00:00",
                "loop",
                {"equity": 25000, "skipped": ["SPY: neutral"], "opened": ["AAPL call x1 @ $4.05"]},
            ),
            (
                "2026-09-09 03:59:59",
                "loop",
                {"equity": 25050, "skipped": ["SPY: neutral"], "errors": ["quote stale"]},
            ),
            ("2026-09-09 04:00:00", "loop", {"equity": 25000, "skipped": ["excluded"]}),
        ]:
            db.execute(
                "INSERT INTO audit(at,kind,body) VALUES (?,?,?)", (at, kind, json.dumps(body))
            )
    report = daily_report(tmp_path, "2026-09-08")
    assert report["accounts"][0]["loops"] == 2
    assert report["accounts"][0]["skipped"] == {"SPY: neutral": 2}
    assert "BUY considered" in report["markdown"] and "BUY filled" in report["markdown"]
    assert "00:00:00" in report["markdown"]
    assert "excluded" not in report["markdown"]
    assert "quote stale" in report["markdown"]
    assert "No saved account" in report["markdown"]


def test_archive_atomic_private_and_no_activity(tmp_path):
    RuntimeStore(tmp_path)
    archive_reports(tmp_path)
    files = list((tmp_path / "reports").glob("*"))
    assert len(files) == 4
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in files)
    assert all("No completed scans" in p.read_text() for p in files)
    archive_reports(tmp_path)
    assert len(list((tmp_path / "reports").glob("*"))) == 4
