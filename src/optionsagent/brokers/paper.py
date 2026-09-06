"""Paper broker with a realistic fill model.

The fill model is the part that matters. A paper broker that fills every order
at the mid will make almost any options strategy look profitable, because it
hands you half the spread for free on both entry and exit. This one assumes you
pay part of the spread in both directions and occasionally miss a fill
entirely, which is what actually happens with resting limit orders.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..greeks import CONTRACT_MULTIPLIER
from ..models import Fill, OptionQuote, Position, Side, utcnow
from .base import Broker

# Robinhood charges no per-contract commission, but regulatory pass-throughs
# (ORF/OCC/SEC/TAF) still apply and are roughly this per contract per side.
REGULATORY_FEE_PER_CONTRACT = 0.06


@dataclass
class PaperBroker(Broker):
    synchronous_fills = True

    starting_equity: float = 25_000.0
    seed: int = 11
    fill_aggression: float = 0.5
    """0.0 fills at mid, 1.0 fills at the far touch. 0.5 is a fair assumption
    for a limit order placed at mid that gets worked for a minute or two."""

    miss_probability: float = 0.10
    """Chance a passive limit order simply does not fill this loop."""

    clock: Callable[[], datetime] = utcnow
    """Injected so backtests advance simulated time instead of wall-clock time."""

    def __post_init__(self) -> None:
        self.cash = self.starting_equity
        self._positions: dict[str, Position] = {}
        self._rng = random.Random(self.seed)
        self._day_trades: list[date] = []
        self._marks: dict[str, float] = {}
        self.fills = []

    # ---- account ---------------------------------------------------------

    def equity(self) -> float:
        held = sum(
            p.market_value(self._marks.get(p.contract.occ_symbol, p.entry_price))
            for p in self._positions.values()
        )
        return self.cash + held

    def buying_power(self) -> float:
        return self.cash

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def mark(self, quote: OptionQuote) -> None:
        """Record the latest mark so ``equity()`` reflects open positions."""
        self._marks[quote.contract.occ_symbol] = quote.mid

    # ---- execution -------------------------------------------------------

    def _fill_price(self, quote: OptionQuote, side: Side, urgent: bool) -> float:
        """Where a limit order realistically fills, given the spread."""
        aggression = 1.0 if urgent else self.fill_aggression
        half = quote.spread / 2.0
        if side is Side.BUY:
            return round(quote.mid + half * aggression, 2)
        return round(max(0.01, quote.mid - half * aggression), 2)

    def buy_to_open(self, quote: OptionQuote, quantity: int, limit_price: float) -> Fill | None:
        if (
            not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity <= 0
            or not quote.is_tradeable()
            or not math.isfinite(limit_price)
            or limit_price <= 0
        ):
            return None
        if self._rng.random() < self.miss_probability:
            return None
        price = self._fill_price(quote, Side.BUY, urgent=False)
        if price > limit_price:
            # The limit protects us: no fill rather than a bad fill.
            return None
        fees = REGULATORY_FEE_PER_CONTRACT * quantity
        cost = price * CONTRACT_MULTIPLIER * quantity + fees
        if cost > self.cash:
            return None
        self.cash -= cost

        key = quote.contract.occ_symbol
        existing = self._positions.get(key)
        if existing:
            total = existing.quantity + quantity
            existing.entry_price = (
                existing.entry_price * existing.quantity + price * quantity
            ) / total
            existing.quantity = total
        else:
            g = quote.greeks
            self._positions[key] = Position(
                contract=quote.contract,
                quantity=quantity,
                entry_price=price,
                entry_underlying=quote.underlying_price,
                entry_iv=g.iv if g else 0.0,
                entry_delta=g.delta if g else 0.0,
                last_mark=price,
                opened_at=self.clock(),
            )
        self._marks[key] = quote.mid
        fill = Fill(quote.contract, Side.BUY, quantity, price, fees=fees)
        self.fills.append(fill)
        return fill

    def sell_to_close(
        self, position: Position, quote: OptionQuote, limit_price: float, urgent: bool = False
    ) -> Fill | None:
        if (
            not quote.is_tradeable()
            or quote.contract.occ_symbol != position.contract.occ_symbol
            or not math.isfinite(limit_price)
            or limit_price <= 0
            or position.quantity <= 0
        ):
            return None
        key = position.contract.occ_symbol
        held = self._positions.get(key)
        if held is None:
            return None
        if not urgent and self._rng.random() < self.miss_probability:
            return None
        price = self._fill_price(quote, Side.SELL, urgent)
        if price < limit_price:
            return None
        qty = min(position.quantity, held.quantity)
        fees = REGULATORY_FEE_PER_CONTRACT * qty
        self.cash += price * CONTRACT_MULTIPLIER * qty - fees

        today = self.clock().date()
        if held.opened_at.date() == today:
            self._day_trades.append(today)
        held.quantity -= qty
        if held.quantity <= 0:
            self._positions.pop(key, None)
            self._marks.pop(key, None)
        fill = Fill(position.contract, Side.SELL, qty, price, fees=fees)
        self.fills.append(fill)
        return fill

    def settle_expiration(self, position: Position, intrinsic_per_share: float) -> float:
        """Settle an expired contract at intrinsic value and remove it.

        Synthetic-only cash settlement convention. This does not model real
        exercise, assignment, share delivery, or broker liquidation policy.
        """
        key = position.contract.occ_symbol
        held = self._positions.pop(key, None)
        self._marks.pop(key, None)
        if held is None:
            return 0.0
        proceeds = max(0.0, intrinsic_per_share) * CONTRACT_MULTIPLIER * held.quantity
        self.cash += proceeds
        return proceeds

    def day_trades_used(self) -> int:
        cutoff = self.clock().date() - timedelta(days=7)
        self._day_trades = [d for d in self._day_trades if d >= cutoff]
        return len(self._day_trades)

    def has_open_order(self, occ_symbol: str) -> bool:
        """Fills here are synchronous, so nothing is ever left working."""
        return False
