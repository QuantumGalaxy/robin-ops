"""Monte Carlo evaluation of a rule set.

The point of this module is to stop the parameters from being a matter of
opinion. It runs the real engine — the same screener, the same exit rules, the
same fill model — across many independent synthetic markets and reports the
distribution of outcomes.

It is *not* a backtest against history and must not be read as one. The
synthetic market has no macro regimes, no correlated sell-offs, and a
momentum signal with no real predictive power. What it does measure honestly is
the mechanical behaviour of the rule set: how often each exit fires, what the
average win and loss look like after spread costs, and whether the
take-profit / stop-loss pair is arithmetically survivable. That is exactly the
question the brief raises, so that is what it answers.
"""

from __future__ import annotations

import math
import statistics
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone  # noqa: F401
from pathlib import Path

from .brokers.paper import PaperBroker
from .config import Config
from .engine import TradingEngine
from .marketdata.synthetic import SyntheticMarketData
from .portfolio import Portfolio
from .strategy.signals import MomentumSignal, PriceHistory


def breakeven_win_rate(take_profit: float, stop_loss: float) -> float:
    """Win rate required for a fixed take-profit / stop-loss pair to break even.

    With a +10% target and a -50% stop, wins must outnumber losses five to one:

        p * 0.10 = (1 - p) * 0.50   =>   p = 0.833

    Every trade that expires or times out somewhere in between shifts this
    around, but the headline number is the right first sanity check on any
    "small profit, large stop" rule set.
    """
    tp = abs(take_profit)
    sl = abs(stop_loss)
    if tp + sl == 0:
        return 0.0
    return sl / (tp + sl)


def expectancy(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Expected return per trade as a fraction of premium risked."""
    return win_rate * abs(avg_win) - (1.0 - win_rate) * abs(avg_loss)


@dataclass
class SimResult:
    label: str
    worlds: int
    days: int
    final_equities: list[float] = field(default_factory=list)
    starting_equity: float = 25_000.0
    trades: int = 0
    wins: int = 0
    sum_win_pct: float = 0.0
    sum_loss_pct: float = 0.0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    max_drawdowns: list[float] = field(default_factory=list)

    @property
    def returns(self) -> list[float]:
        return [e / self.starting_equity - 1.0 for e in self.final_equities]

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_win_pct(self) -> float:
        return self.sum_win_pct / self.wins if self.wins else 0.0

    @property
    def avg_loss_pct(self) -> float:
        losses = self.trades - self.wins
        return self.sum_loss_pct / losses if losses else 0.0

    @property
    def profit_factor(self) -> float | None:
        return self.gross_win / self.gross_loss if self.gross_loss > 0 else None

    @property
    def expectancy_per_trade(self) -> float:
        return expectancy(self.win_rate, self.avg_win_pct, self.avg_loss_pct)

    def percentile(self, p: float) -> float:
        if not self.final_equities:
            return 0.0
        vals = sorted(self.returns)
        idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
        return vals[idx]

    def as_dict(self) -> dict:
        rets = self.returns
        return {
            "label": self.label,
            "worlds": self.worlds,
            "days": self.days,
            "trades": self.trades,
            "trades_per_world": round(self.trades / self.worlds, 1) if self.worlds else 0.0,
            "win_rate": round(self.win_rate, 4),
            "avg_win_pct": round(self.avg_win_pct, 4),
            "avg_loss_pct": round(self.avg_loss_pct, 4),
            "expectancy_per_trade": round(self.expectancy_per_trade, 4),
            "profit_factor": round(self.profit_factor, 3) if self.profit_factor else None,
            "mean_return": round(statistics.fmean(rets), 4) if rets else 0.0,
            "median_return": round(statistics.median(rets), 4) if rets else 0.0,
            "p05_return": round(self.percentile(0.05), 4),
            "p95_return": round(self.percentile(0.95), 4),
            "prob_profit": (round(sum(1 for r in rets if r > 0) / len(rets), 4) if rets else 0.0),
            "avg_max_drawdown": (
                round(statistics.fmean(self.max_drawdowns), 4) if self.max_drawdowns else 0.0
            ),
            "exit_reasons": dict(sorted(self.exit_reasons.items(), key=lambda kv: -kv[1])),
        }


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5


def run_world(
    config: Config, days: int, seed: int, start: date, steps_per_day: int = 4
) -> tuple[float, list, float]:
    """Run one synthetic market to completion. Returns (final equity, trades, max drawdown).

    ``steps_per_day`` controls how often the agent gets to look at the market.
    Anything below about 4 makes take-profit rules look far better than they
    are, because the position gaps past the target between observations and
    "fills" at the gapped price rather than the target.
    """
    market = SyntheticMarketData(
        symbols=config.universe.symbols,
        seed=seed,
        today=start,
        risk_free_rate=config.market.risk_free_rate,
    )
    history = PriceHistory()

    # Warm up the signal's price history before any trading starts, otherwise
    # the first month is spent with no direction and no trades.
    for _ in range(45):
        market.step(1)
        for sym in config.universe.symbols:
            price = market.underlying_price(sym)
            if price:
                history.push(sym, price)

    intraday_step = 1.0 / steps_per_day
    slot = {"i": 0}

    def clock() -> datetime:
        # Spread the intraday polls across the 6.5-hour session so that
        # ``days_held`` and the time stop advance smoothly.
        minutes = int(390 * (slot["i"] / steps_per_day))
        return datetime.combine(market.today, time(9, 30), tzinfo=UTC) + timedelta(minutes=minutes)

    broker = PaperBroker(
        starting_equity=config.broker.starting_equity, seed=seed * 31 + 7, clock=clock
    )
    with tempfile.TemporaryDirectory() as tmp:
        portfolio = Portfolio(state_dir=Path(tmp))
        engine = TradingEngine(
            config=config,
            data=market,
            broker=broker,
            portfolio=portfolio,
            signal=MomentumSignal(),
            history=history,
        )

        peak = broker.equity()
        max_dd = 0.0
        for _ in range(days):
            if not _is_trading_day(market.today):
                market.step(1)
                continue
            for i in range(steps_per_day):
                slot["i"] = i
                market.step(intraday_step)
                engine.run_once(as_of=clock())
            eq = broker.equity()
            peak = max(peak, eq)
            max_dd = min(max_dd, eq / peak - 1.0)
        return broker.equity(), list(portfolio.trades), max_dd


def run_simulation(
    config: Config,
    *,
    label: str = "strategy",
    worlds: int = 40,
    days: int = 180,
    seed: int = 1234,
    start: date | None = None,
    steps_per_day: int = 4,
) -> SimResult:
    start = start or date(2025, 1, 3)
    result = SimResult(
        label=label, worlds=worlds, days=days, starting_equity=config.broker.starting_equity
    )
    for i in range(worlds):
        equity, trades, max_dd = run_world(config, days, seed + i * 101, start, steps_per_day)
        result.final_equities.append(equity)
        result.max_drawdowns.append(max_dd)
        for t in trades:
            result.trades += 1
            result.exit_reasons[t.reason.value] = result.exit_reasons.get(t.reason.value, 0) + 1
            if t.pnl > 0:
                result.wins += 1
                result.sum_win_pct += t.return_pct
                result.gross_win += t.pnl
            else:
                result.sum_loss_pct += t.return_pct
                result.gross_loss += abs(t.pnl)
    return result


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Kelly-optimal fraction of capital, floored at zero.

    Included as a reality check on sizing: when expectancy is negative Kelly
    returns 0, i.e. the correct bet size for the rule set is nothing at all.
    """
    b = abs(avg_win) / abs(avg_loss) if avg_loss else math.inf
    if not math.isfinite(b) or b <= 0:
        return 0.0
    f = (win_rate * (b + 1) - 1) / b
    return max(0.0, f)
