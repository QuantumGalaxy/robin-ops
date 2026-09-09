"""Read-only daily review of durable paper audit records, grouped by New York date."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .paper_lab import PROFILES

NY = ZoneInfo("America/New_York")


def daily_report(root, day):
    day = date.fromisoformat(day)
    start = datetime.combine(day, time(), NY).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), NY).astimezone(UTC)
    result = {
        "date": day.isoformat(),
        "timezone": "America/New_York",
        "generated_at": datetime.now(UTC).isoformat(),
        "accounts": [],
    }
    folders = [("Original paper account", Path(root))] + [
        (name, Path(root) / "experiments" / key) for key, (name, _) in PROFILES.items()
    ]
    lines = [
        f"# Paper trading review — {day}",
        "",
        "All times are New York time. Simulated orders only.",
        "Intraday reports are provisional. Missing activity is not proof the agent ran.",
        "",
    ]
    for name, folder in folders:
        path = folder / "runtime.sqlite3"
        if not path.exists():
            lines += [f"## {name}", "No saved account available.", ""]
            continue
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT id,at,kind,body FROM audit WHERE at>=? AND at<? ORDER BY id",
                (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
            ).fetchall()
            checkpoint = db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()
        events = [
            dict(id=i, at=at, kind=kind, detail=json.loads(body)) for i, at, kind, body in rows
        ]
        loops = [e for e in events if e["kind"] == "loop"]
        skipped, blocked, errors = Counter(), Counter(), Counter()
        for e in loops:
            b = e["detail"]
            skipped.update(b.get("skipped", []))
            errors.update(b.get("errors", []))
            if b.get("blocked_reason"):
                blocked[b["blocked_reason"]] += 1
        s = json.loads(checkpoint[0]) if checkpoint else {}
        trades = [
            t for t in s.get("trades", []) if start <= datetime.fromisoformat(t["closed_at"]) < end
        ]
        account = dict(
            name=name,
            loops=len(loops),
            skipped=dict(skipped),
            blocked=dict(blocked),
            errors=dict(errors),
            closed_trades=trades,
            events=events,
        )
        result["accounts"].append(account)
        lines += [
            f"## {name}",
            f"- Completed scans: {len(loops)}",
            f"- Closed fills: {len(trades)}; "
            f"net realized P&L: ${sum(t['pnl'] for t in trades):,.2f}",
        ]
        if loops:
            first, last = loops[0]["detail"], loops[-1]["detail"]
            lines += [
                f"- First / last observed equity: ${first['equity']:,.2f} / ${last['equity']:,.2f}",
                "- This is sampled equity, not necessarily opening/closing account value.",
                "- Positions at last scan: " + (", ".join(last.get("held", [])) or "None"),
            ]
        else:
            lines += ["- No completed scans recorded: review service availability."]
        for title, values in [
            ("Entry blocks", blocked),
            ("Skipped trades", skipped),
            ("Errors", errors),
        ]:
            lines += ["", f"### {title}"]
            lines += [
                f"- {reason} — {count} scan(s)" for reason, count in values.most_common()
            ] or ["- None recorded."]
        lines += ["", "### Decisions and fills"]
        timeline = []
        for e in events:
            b = e["detail"]
            at = (
                datetime.fromisoformat(e["at"])
                .replace(tzinfo=UTC)
                .astimezone(NY)
                .strftime("%H:%M:%S")
            )
            if e["kind"] == "entry_decision":
                timeline += [
                    f"- {at} BUY considered: {b['contract']} ×{b['quantity']}; "
                    f"bid ${b['bid']:.2f}, ask ${b['ask']:.2f}. " + "; ".join(b.get("reasons", []))
                ]
                if b.get("greeks"):
                    rank = b.get("iv_rank")
                    rank_text = f"{rank:.1%}" if rank is not None else "unavailable"
                    inputs = ", ".join(
                        f"{k}={v:.4f}"
                        for k, v in b["greeks"].items()
                        if isinstance(v, (int, float))
                    )
                    timeline += [
                        f"  Direction: {b.get('direction')}; IV rank: {rank_text}. {inputs}"
                    ]
            elif e["kind"] == "exit_decision" and b.get("exit"):
                timeline += [f"- {at} SELL considered: {b['contract']}; {b['reason']}."]
            elif e["kind"] == "loop":
                timeline += [f"- {at} BUY filled: {v}" for v in b.get("opened", [])]
                timeline += [f"- {at} SELL filled: {v}" for v in b.get("closed", [])]
        lines += timeline or ["- No entry/exit actions recorded."]
        lines += ["", "### Closed-fill accounting"]
        lines += [
            f"- {t['contract']}: net ${t['pnl']:,.2f}; "
            f"fees ${t.get('fees', 0):.2f}; reason {t['reason']}."
            for t in trades
        ] or ["- No closed fills."]
        lines += [""]
    from .stock_paper import report as stock_report

    account, stock_lines = stock_report(root, start, end)
    lines += stock_lines
    if account:
        result["accounts"].append(account)
    result["markdown"] = "\n".join(lines)
    return result


def archive_reports(root):
    folder = Path(root) / "reports"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    today = datetime.now(NY).date()
    # Rebuild yesterday too: late completion/restart can add records after midnight.
    for day in (today - timedelta(days=1), today):
        report = daily_report(root, day.isoformat())
        for suffix, content in [("md", report["markdown"]), ("json", json.dumps(report, indent=2))]:
            target = folder / f"{day}.{suffix}"
            temp = target.with_suffix(".tmp")
            temp.write_text(content)
            temp.chmod(0o600)
            temp.replace(target)
