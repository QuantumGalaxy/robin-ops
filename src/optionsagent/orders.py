"""Duplicate-order protection.

The failure this prevents: the agent decides to buy, submits, and the response is
lost to a timeout. On the next loop it sees no position, decides to buy again, and
now holds two contracts it sized for one. Retries and restarts both cause this, and
it is silent — nothing errors, you simply own twice the risk you intended.

The defence is a deterministic client order ID plus a persisted registry. The ID is
derived from the trade's *intent* (contract, side, quantity, and the minute the
decision was made), so a retry of the same decision produces the same ID and is
recognised as a duplicate, while a genuinely new decision a minute later does not
collide with it.

The registry is written to disk before the order is submitted, never after. If the
process dies mid-submission, the record of the attempt survives and the next run
reconciles against the broker rather than resubmitting.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from .models import OptionContract, Side, utcnow

log = logging.getLogger(__name__)


class OrderState(StrEnum):
    PENDING = "pending"
    """Submitted, outcome unknown. Blocks a second attempt at the same intent."""
    FILLED = "filled"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class OrderRecord:
    client_order_id: str
    occ_symbol: str
    side: str
    quantity: int
    limit_price: float
    state: str = OrderState.PENDING.value
    broker_order_id: str = ""
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: utcnow().isoformat())
    detail: str = ""

    @property
    def created(self) -> datetime:
        return datetime.fromisoformat(self.created_at)


def client_order_id(
    contract: OptionContract, side: Side, quantity: int, at: datetime | None = None
) -> str:
    """Deterministic ID for one trading intent.

    Truncated to the minute: a retry inside the same minute is the same intent, a
    fresh decision in a later minute is a different one. That is a deliberate
    trade-off — it cannot distinguish two genuinely independent orders for the same
    contract within one minute, which this strategy never wants to place anyway
    because it holds at most one position per underlying.
    """
    stamp = (at or utcnow()).strftime("%Y%m%d%H%M")
    raw = f"{contract.occ_symbol}|{side.value}|{quantity}|{stamp}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class OrderRegistry:
    """Persisted record of every order the agent has attempted."""

    state_dir: Path = Path("state")
    orders: dict[str, OrderRecord] = field(default_factory=dict)
    pending_ttl_minutes: int = 30  # Compatibility only: age never resolves an uncertain order.

    persist: bool = True
    """Set False for backtests. There is no crash to recover from in a simulated
    run, and writing to a shared state directory from parallel workers corrupts
    the file. The duplicate logic itself still applies in memory."""

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        if self.persist:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    @property
    def path(self) -> Path:
        return self.state_dir / "orders.json"

    # ---- duplicate detection --------------------------------------------

    def is_duplicate(self, order_id: str) -> bool:
        record = self.orders.get(order_id)
        if record is None:
            return False
        return record.state in (OrderState.PENDING.value, OrderState.FILLED.value)

    def has_pending_for(self, occ_symbol: str) -> bool:
        """Any live order on this contract, regardless of intent.

        Broader than :meth:`is_duplicate` and used before opening: it stops a
        second position being built up in a contract that already has an order
        working.
        """
        return any(
            r.occ_symbol == occ_symbol and r.state == OrderState.PENDING.value
            for r in self.orders.values()
        )

    # ---- lifecycle -------------------------------------------------------

    def reserve(
        self, order_id: str, contract: OptionContract, side: Side, quantity: int, limit_price: float
    ) -> OrderRecord:
        """Record the intent to trade, before anything is sent."""
        record = OrderRecord(
            client_order_id=order_id,
            occ_symbol=contract.occ_symbol,
            side=side.value,
            quantity=quantity,
            limit_price=limit_price,
        )
        self.orders[order_id] = record
        self.save()
        return record

    def mark(
        self, order_id: str, state: OrderState, broker_order_id: str = "", detail: str = ""
    ) -> None:
        record = self.orders.get(order_id)
        if record is None:
            return
        record.state = state.value
        record.updated_at = utcnow().isoformat()
        if broker_order_id:
            record.broker_order_id = broker_order_id
        if detail:
            record.detail = detail
        self.save()

    def pending(self) -> list[OrderRecord]:
        return [r for r in self.orders.values() if r.state == OrderState.PENDING.value]

    # ---- persistence -----------------------------------------------------

    def save(self) -> None:
        if not self.persist:
            return
        # Written before submission, so a crash leaves evidence of the attempt.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self.orders.items()}, indent=2))
        tmp.replace(self.path)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            raise RuntimeError(f"Order registry is corrupt: {self.path}; trading blocked") from None
        for key, row in data.items():
            self.orders[key] = OrderRecord(**row)
        if self.pending():
            log.warning(
                "%d order(s) were still pending at shutdown; reconcile before trading",
                len(self.pending()),
            )
