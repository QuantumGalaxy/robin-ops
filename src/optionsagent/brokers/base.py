"""Broker interface.

Kept narrow on purpose: the strategy only ever needs to read equity, read
positions, and send a limit order in one of two directions. Anything more
specific belongs in the adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Fill, OptionQuote, Position


class Broker(ABC):
    @abstractmethod
    def equity(self) -> float:
        """Total account value, used for position sizing and risk limits."""

    @abstractmethod
    def buying_power(self) -> float:
        """Cash available to open new long option positions."""

    @abstractmethod
    def positions(self) -> list[Position]:
        """Open long option positions as the broker sees them.

        The engine reconciles its own ledger against this on every loop, so a
        manual close in the app does not leave a phantom position behind.
        """

    @abstractmethod
    def buy_to_open(self, quote: OptionQuote, quantity: int, limit_price: float) -> Fill | None:
        """Submit a buy-to-open limit order. Returns ``None`` if it did not fill."""

    @abstractmethod
    def sell_to_close(
        self, position: Position, quote: OptionQuote, limit_price: float, urgent: bool = False
    ) -> Fill | None:
        """Submit a sell-to-close limit order."""

    def settle_expiration(self, position: Position, intrinsic_per_share: float) -> float:
        """Book an expired contract at intrinsic value. Real brokers do this for us."""
        return max(0.0, intrinsic_per_share) * 100 * position.quantity

    def day_trades_used(self) -> int:
        """Day trades in the trailing 5 business days, for PDT protection."""
        return 0

    def has_open_order(self, occ_symbol: str) -> bool | None:
        """Is an order working on this contract?

        Returns None when the adapter cannot tell. Callers must treat None as
        "possibly yes" — an ambiguous submission assumed to be dead is exactly
        how an agent ends up holding twice the position it sized for.
        """
        return None

    def cancel_all(self) -> None:
        return None
