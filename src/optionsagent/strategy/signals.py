"""Directional signal.

The exit rules in the brief are risk management, not an edge. Buying a call or a
put with no view is a negative-expectancy bet: you pay the spread twice and pay
theta the whole way. Something has to decide *which* direction to buy and
*whether to buy at all*, and this module is where that decision is isolated so
it can be replaced without touching the rest of the agent.

The default is a plain trend/momentum filter, chosen because it is transparent
and hard to overfit rather than because it is the best available signal. Treat
it as a slot to fill, not as the finished answer: swap in whatever edge you can
actually demonstrate out of sample.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["call", "put", "none"]


@dataclass
class PriceHistory:
    """Rolling per-symbol close history used by the signals below."""

    maxlen: int = 120
    _series: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=120))
    )

    def push(self, symbol: str, price: float) -> None:
        self._series[symbol].append(price)

    def get(self, symbol: str) -> list[float]:
        return list(self._series[symbol])

    def ready(self, symbol: str, n: int) -> bool:
        return len(self._series[symbol]) >= n


@dataclass
class MomentumSignal:
    """Buy calls in confirmed uptrends, puts in confirmed downtrends, else stand aside.

    Two conditions must agree: price on the correct side of a slow moving
    average (regime), and a lookback return large enough relative to the
    symbol's own noise (impulse). Requiring both keeps the agent out of chop,
    which is where a premium-buying strategy bleeds to death.
    """

    lookback: int = 10
    trend_window: int = 30
    min_z: float = 0.6
    """Minimum move in standard deviations before a direction is taken."""

    allow_puts: bool = True

    def direction(self, symbol: str, history: PriceHistory) -> tuple[Direction, float]:
        """Return the direction to trade and a 0..1 confidence."""
        prices = history.get(symbol)
        need = max(self.trend_window, self.lookback + 2)
        if len(prices) < need:
            return "none", 0.0

        recent = prices[-self.lookback - 1 :]
        rets = [
            math.log(recent[i + 1] / recent[i])
            for i in range(len(recent) - 1)
            if recent[i] > 0 and recent[i + 1] > 0
        ]
        if len(rets) < 3:
            return "none", 0.0

        window = prices[-self.trend_window :]
        sma = statistics.fmean(window)
        daily_rets = [
            math.log(window[i + 1] / window[i])
            for i in range(len(window) - 1)
            if window[i] > 0 and window[i + 1] > 0
        ]
        sigma = statistics.pstdev(daily_rets) if len(daily_rets) > 2 else 0.0
        if sigma <= 0:
            return "none", 0.0

        cumulative = sum(rets)
        z = cumulative / (sigma * math.sqrt(len(rets)))
        last = prices[-1]
        confidence = min(1.0, abs(z) / 3.0)

        if z >= self.min_z and last > sma:
            return "call", confidence
        if self.allow_puts and z <= -self.min_z and last < sma:
            return "put", confidence
        return "none", 0.0
