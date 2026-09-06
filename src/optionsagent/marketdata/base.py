"""Market data provider interface.

Two implementations ship with the agent: a synthetic one used for simulation and
offline development, and a Robinhood one. The strategy code depends only on this
protocol, so swapping in a real vendor feed (Polygon, Tradier, CBOE) later means
writing one class and changing one config line.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..models import OptionQuote


class MarketDataProvider(ABC):
    @abstractmethod
    def underlying_price(self, symbol: str) -> float | None:
        """Last trade / mid price of the underlying."""

    @abstractmethod
    def option_chain(self, symbol: str, min_dte: int, max_dte: int) -> list[OptionQuote]:
        """All tradeable contracts for ``symbol`` expiring in the DTE window."""

    @abstractmethod
    def quote(self, symbol: str, expiry: date, strike: float, right: str) -> OptionQuote | None:
        """Refresh a single contract. Used to mark open positions."""

    def historical_closes(self, symbol: str, days: int = 90) -> list[float]:
        """Daily closes, oldest first, used to warm up the directional signal.

        Without this the agent is blind for its first ~30 sessions after every
        restart, because the momentum signal has no history to measure against.
        """
        return []

    def iv_rank(self, symbol: str) -> float:
        """Where current IV sits in its trailing 52-week range, 0.0 to 1.0.

        Defaults to 0.5 (neutral) when a provider cannot supply history, which
        makes the IV-rank filter a no-op rather than silently rejecting
        everything.
        """
        return 0.5

    def next_earnings_date(self, symbol: str) -> date | None:
        """Next confirmed or estimated earnings date, if known."""
        return None

    def earnings_known(self, symbol: str) -> bool:
        return False

    def is_market_open(self) -> bool:
        return False

    def close(self) -> None:
        return None
