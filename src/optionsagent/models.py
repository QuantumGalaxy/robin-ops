"""Core domain objects shared by the data layer, strategy, and broker adapters."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from .greeks import CONTRACT_MULTIPLIER, Greeks, Right


def utcnow() -> datetime:
    return datetime.now(UTC)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ExitReason(StrEnum):
    """Why a position was closed. Persisted so the trade log can be analysed later."""

    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"
    EXPIRY_GUARD = "expiry_guard"
    EXPIRED = "expired"
    THETA_BLEED = "theta_bleed"
    KILL_SWITCH = "kill_switch"
    MANUAL = "manual"


@dataclass(frozen=True)
class OptionContract:
    """Identity of a single listed option series."""

    symbol: str
    expiry: date
    strike: float
    right: Right
    occ_symbol: str = ""
    broker_id: str = ""

    def __post_init__(self) -> None:
        if not self.occ_symbol:
            object.__setattr__(self, "occ_symbol", self.build_occ_symbol())

    def build_occ_symbol(self) -> str:
        """OCC 21-character option symbol, e.g. ``AAPL  260918C00230000``."""
        root = self.symbol.ljust(6)
        ymd = self.expiry.strftime("%y%m%d")
        cp = "C" if self.right == "call" else "P"
        strike = f"{int(round(self.strike * 1000)):08d}"
        return f"{root}{ymd}{cp}{strike}"

    def days_to_expiry(self, as_of: date | None = None) -> int:
        return (self.expiry - (as_of or utcnow().date())).days

    @property
    def short(self) -> str:
        """Compact form for tables, e.g. ``NVDA 12/18 125C``."""
        return f"{self.symbol} {self.expiry:%m/%d} {self.strike:g}{self.right[0].upper()}"

    def __str__(self) -> str:
        return f"{self.symbol} {self.expiry:%Y-%m-%d} {self.strike:g}{self.right[0].upper()}"


@dataclass
class OptionQuote:
    """A point-in-time NBBO snapshot plus everything derived from it."""

    contract: OptionContract
    bid: float
    ask: float
    underlying_price: float
    as_of: datetime = field(default_factory=utcnow)
    open_interest: int = 0
    volume: int = 0
    greeks: Greeks | None = None

    @property
    def mid(self) -> float:
        """Mid price per share. Positions are always marked here, never at last-trade.

        Last-trade prints on options are frequently minutes stale and can sit
        outside the current spread, which would fire take-profit and stop-loss
        rules on phantom moves.
        """
        if self.bid <= 0:
            return self.ask
        if self.ask <= 0:
            return self.bid
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as a fraction of mid.

        This is the single most important liquidity filter in the whole system.
        A 12% spread means a round trip costs ~12% of premium, so a 10%
        take-profit target is mathematically unreachable on that contract.
        """
        if self.mid <= 0:
            return math.inf
        return self.spread / self.mid

    @property
    def mid_contract_price(self) -> float:
        return self.mid * CONTRACT_MULTIPLIER

    def is_tradeable(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


@dataclass
class Position:
    """An open long option position and its running high-water mark."""

    contract: OptionContract
    quantity: int
    entry_price: float
    """Per-share fill price. Multiply by 100 for the per-contract cost."""
    opened_at: datetime = field(default_factory=utcnow)
    entry_underlying: float = 0.0
    entry_iv: float = 0.0
    entry_delta: float = 0.0
    peak_return: float = 0.0
    """Best unrealised return ever seen, as a fraction. Drives the trailing stop."""
    trailing_armed: bool = False
    last_mark: float = 0.0
    broker_order_id: str = ""
    notes: str = ""

    @property
    def cost_basis(self) -> float:
        return self.entry_price * CONTRACT_MULTIPLIER * self.quantity

    def market_value(self, mark: float) -> float:
        return mark * CONTRACT_MULTIPLIER * self.quantity

    def unrealized_return(self, mark: float) -> float:
        """Return as a fraction of cost basis: 0.10 == the +10% target."""
        if self.entry_price <= 0:
            return 0.0
        return (mark - self.entry_price) / self.entry_price

    def unrealized_pnl(self, mark: float) -> float:
        return (mark - self.entry_price) * CONTRACT_MULTIPLIER * self.quantity

    def days_held(self, as_of: datetime | None = None) -> float:
        return ((as_of or utcnow()) - self.opened_at).total_seconds() / 86400.0


@dataclass
class Candidate:
    """A contract that passed screening, with the score used to rank it."""

    quote: OptionQuote
    score: float
    delta: float
    theta_pct_per_day: float
    gamma: float
    vega: float
    iv: float
    iv_rank: float
    dte: int
    reasons: list[str] = field(default_factory=list)

    @property
    def contract(self) -> OptionContract:
        return self.quote.contract


@dataclass
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    detail: str = ""
    urgency: str = "normal"
    """``normal`` prices at mid; ``urgent`` crosses toward the bid to guarantee a fill."""


@dataclass
class Fill:
    contract: OptionContract
    side: Side
    quantity: int
    price: float
    at: datetime = field(default_factory=utcnow)
    order_id: str = ""
    fees: float = 0.0

    @property
    def notional(self) -> float:
        return self.price * CONTRACT_MULTIPLIER * self.quantity


@dataclass
class TradeRecord:
    """A completed round trip, appended to the trade log for post-hoc analysis."""

    contract: str
    quantity: int
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    reason: ExitReason
    pnl: float
    return_pct: float
    fees: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "reason": self.reason.value,
            "pnl": round(self.pnl, 2),
            "return_pct": round(self.return_pct, 4),
            "fees": round(self.fees, 2),
        }
