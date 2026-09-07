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
