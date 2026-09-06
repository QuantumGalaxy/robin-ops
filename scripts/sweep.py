"""Run the experiment matrix from sections 42-44 of the design brief.

The brief asks the right question — "the final rule should come from evidence" —
so this answers it with the Monte Carlo simulator rather than with an opinion.

Caveat that applies to every number below: these are synthetic worlds with a
variance risk premium baked in, not historical data. They are useful for ranking
rules against each other and for showing which knobs matter, and they are not a
forecast of what any rule will earn.

    python scripts/sweep.py
"""

from __future__ import annotations

import copy
import logging
import sys
import time
from multiprocessing import Pool

from optionsagent.config import Config
from optionsagent.simulate import run_simulation

# The agent narrates its risk decisions, which is right in production and noise
# across thousands of simulated days.
logging.getLogger("optionsagent").setLevel(logging.CRITICAL)

WORLDS = 40
DAYS = 250


def _run(job: tuple[str, Config]) -> dict:
    label, cfg = job
    logging.getLogger("optionsagent").setLevel(logging.CRITICAL)
    started = time.monotonic()
    result = run_simulation(cfg, label=label, worlds=WORLDS, days=DAYS).as_dict()
    print(f"  {label:<26} {time.monotonic() - started:5.1f}s", file=sys.stderr, flush=True)
    return result


def evaluate_all(jobs: list[tuple[str, Config]]) -> list[dict]:
    """Variants are independent, so run them across cores."""
    with Pool(processes=4) as pool:
        return pool.map(_run, jobs)


def show(title: str, rows: list[dict]) -> None:
    print(f"\n{title}")
    print("-" * 100)
    print(
        f"{'variant':<26}{'trades':>8}{'win%':>8}{'expect':>9}{'PF':>7}"
        f"{'median':>10}{'5th pct':>10}{'95th pct':>10}{'avg DD':>10}"
    )
    for r in rows:
        pf = r["profit_factor"]
        print(
            f"{r['label']:<26}{r['trades']:>8}{r['win_rate']:>7.1%}"
            f"{r['expectancy_per_trade']:>9.2%}{(f'{pf:.2f}' if pf else '-'):>7}"
            f"{r['median_return']:>10.1%}{r['p05_return']:>10.1%}"
            f"{r['p95_return']:>10.1%}{r['avg_max_drawdown']:>10.1%}"
        )


def build_jobs() -> dict[str, list[tuple[str, Config]]]:
    base = Config()
    groups: dict[str, list[tuple[str, Config]]] = {}

    # Section 43: stop-loss experiments.
    stops = []
    for stop in (-0.15, -0.20, -0.25, -0.30, -0.50):
        cfg = copy.deepcopy(base)
        cfg.exit.stop_loss_pct = stop
        stops.append((f"stop {stop:.0%}", cfg))
    groups["Section 43 - stop-loss"] = stops

    # Section 42: profit taking. Fixed targets, then the two trailing families.
    profits = []
    for target in (0.10, 0.15, 0.20):
        cfg = copy.deepcopy(base)
        cfg.exit.trailing_enabled = False
        cfg.exit.take_profit_pct = target
        profits.append((f"fixed +{target:.0%}", cfg))
    for trail in (0.05, 0.075, 0.10):
        cfg = copy.deepcopy(base)
        cfg.exit.trailing_mode = "pct_of_peak_value"
        cfg.exit.trailing_giveback_pct = trail
        profits.append((f"+10% then {trail:.1%} trail", cfg))
    for giveback in (0.25, 0.40, 0.60):
        cfg = copy.deepcopy(base)
        cfg.exit.trailing_mode = "giveback_of_gain"
        cfg.exit.trailing_giveback_pct = giveback
        profits.append((f"+10% then {giveback:.0%} giveback", cfg))
    groups["Section 42 - profit taking"] = profits

    # Section 44: contract selection.
    deltas = []
    for lo, hi in ((0.30, 0.45), (0.45, 0.60), (0.55, 0.70), (0.60, 0.75)):
        cfg = copy.deepcopy(base)
        cfg.entry.min_abs_delta, cfg.entry.max_abs_delta = lo, hi
        deltas.append((f"delta {lo:.2f}-{hi:.2f}", cfg))
    groups["Section 44 - delta band"] = deltas

    dtes = []
    for lo, hi in ((7, 10), (12, 18), (21, 30), (30, 45), (45, 60)):
        cfg = copy.deepcopy(base)
        cfg.entry.min_dte, cfg.entry.max_dte = lo, hi
        cfg.exit.expiry_guard_dte = min(3, max(1, lo - 4))
        cfg.exit.max_hold_days = min(cfg.exit.max_hold_days, max(3, hi - 4))
        dtes.append((f"dte {lo}-{hi}", cfg))
    groups["Section 44 - days to expiry"] = dtes

    return groups


def main() -> int:
    groups = build_jobs()
    flat = [job for jobs in groups.values() for job in jobs]
    print(f"running {len(flat)} variants x {WORLDS} worlds x {DAYS} days", file=sys.stderr)

    results = evaluate_all(flat)
    by_label = {r["label"]: r for r in results}
    for title, jobs in groups.items():
        show(title, [by_label[label] for label, _ in jobs])
    return 0


if __name__ == "__main__":
    sys.exit(main())
