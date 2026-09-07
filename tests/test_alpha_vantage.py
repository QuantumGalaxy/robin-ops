from datetime import UTC, date, datetime

import pytest

from optionsagent.alpha_vantage import download, select_iv

DAY = date(2026, 9, 4)


def payload(iv="0.3"):
    return {
        "message": "success",
        "data": [
            {
                "contractID": "AAPL261002C00200000",
                "symbol": "AAPL",
                "date": "2026-09-04",
                "expiration": "2026-10-02",
                "strike": "200",
                "type": "call",
                "implied_volatility": iv,
                "bid": "5",
                "ask": "5.2",
            }
        ],
    }


def test_select_dated_standard_contract():
    assert select_iv(payload(), "AAPL", DAY, 201) == 0.3
    for iv in [None, "NaN", "0", "-1"]:
        with pytest.raises(ValueError):
            select_iv(payload(iv), "AAPL", DAY, 201)
    wrong = payload()
    wrong["data"][0]["contractID"] = "AAPL1261002C00200000"
    with pytest.raises(ValueError):
        select_iv(wrong, "AAPL", DAY, 201)
    with pytest.raises(ValueError):
        select_iv(payload(), "AAPL", date(2026, 9, 3), 201)


def test_entitlement_error_is_not_empty_history():
    with pytest.raises(ValueError):
        select_iv({"Information": "premium required"}, "AAPL", DAY, 201)


def test_bounded_download_resumes_without_repeating_requests(tmp_path):
    class Client:
        calls = 0

        def get(self, function, **params):
            self.calls += 1
            if function == "TIME_SERIES_DAILY":
                return {"Time Series (Daily)": {"2026-09-04": {"4. close": "201"}}}
            return payload()

    client = Client()
    path = tmp_path / "history.csv"
    args = dict(days=1, max_requests=2, now=datetime(2026, 9, 7, 16, tzinfo=UTC))
    result = download(client, ["AAPL"], path, **args)
    assert result["complete"] and result["requests"] == 2
    assert download(client, ["AAPL"], path, **args)["requests"] == 0
    assert client.calls == 2
    assert "Alpha Vantage" in path.read_text()


def test_mixed_vendor_iv_definitions_fail_closed(tmp_path):
    import sqlite3
    from contextlib import closing

    from optionsagent.reference_feed import IVArchive, calendar

    archive = IVArchive(tmp_path / "iv.sqlite3")
    days = calendar().sessions_in_range("2025-01-01", DAY.isoformat())[-200:]
    with closing(sqlite3.connect(archive.path)) as db, db:
        db.executemany(
            "INSERT INTO iv VALUES (?,?,?,?)",
            [
                ("AAPL", d.date().isoformat(), 0.2 + i / 1000, "vendor-a")
                for i, d in enumerate(days)
            ],
        )
    assert archive.rank("AAPL", DAY)[0] is not None
    with closing(sqlite3.connect(archive.path)) as db, db:
        db.execute("UPDATE iv SET source='vendor-b' WHERE day=?", (DAY.isoformat(),))
    assert archive.rank("AAPL", DAY) == (None, 200)
