"""Order routing through the official Robinhood Trading MCP.

Every order goes through ``review_option_order`` before ``place_option_order``.
That is not a nicety: review is the broker's own pre-trade simulation and returns
alerts this code could not compute for itself — buying power shortfalls, options
level restrictions, contract-specific warnings. If review blocks, nothing is sent.

Safety properties:

* ``dry_run`` defaults to True. Reviews still run, so you see exactly what the
  broker would say, but nothing is placed.
* ``require_approval`` supports the propose-then-confirm mode: the agent prepares
  and reviews the order, then hands it to a callback for a human decision.
* Only ``buy_to_open`` and ``sell_to_close`` exist. There is no code path that
  opens a short option position.
* Limit orders only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..mcp.client import ToolCaller
from ..mcp.robinhood import RobinhoodMcp, as_date, as_float, as_int, pick, review_blocks_order
from ..models import Fill, OptionContract, OptionQuote, Position, Side
from .base import Broker

log = logging.getLogger(__name__)

ApprovalCallback = Callable[[dict[str, Any], dict[str, Any]], bool]
"""Given the order payload and the broker's review response, return True to submit."""


@dataclass
class RobinhoodMcpBroker(Broker):
    caller: ToolCaller
    dry_run: bool = True
    require_approval: bool = False
    approval_callback: ApprovalCallback | None = None
    time_in_force: str = "gfd"
    """Good for day. A resting GTC order can fill days later against a thesis that
    no longer holds, which is precisely what an unattended agent must not allow."""

    _last_review: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.api = RobinhoodMcp(self.caller)
        if self.dry_run:
            log.warning("RobinhoodMcpBroker is in DRY RUN: orders are reviewed but never placed")

    # ---- account ---------------------------------------------------------

    def equity(self) -> float:
        p = self.api.portfolio()
        return as_float(pick(p, "equity", "total_equity", "market_value", "portfolio_value"))

    def buying_power(self) -> float:
        p = self.api.portfolio()
        value = as_float(pick(p, "buying_power", "cash_available_for_trading", "cash"))
        if value:
            return value
        for account in self.api.accounts():
            value = as_float(pick(account, "buying_power", "cash", "portfolio_cash"))
            if value:
                return value
        return 0.0

    def positions(self) -> list[Position]:
        out: list[Position] = []
        for row in self.api.option_positions():
            quantity = as_int(pick(row, "quantity", "contracts", "size"))
            direction = str(pick(row, "type", "direction", "position_type", default="long")).lower()
            if quantity <= 0 or direction == "short":
                continue
            expiry = as_date(pick(row, "expiration_date", "expiration", "expires_at"))
            strike = as_float(pick(row, "strike_price", "strike"))
            right = str(pick(row, "option_type", "type", "contract_type", default="call")).lower()
            symbol = str(pick(row, "chain_symbol", "symbol", "underlying_symbol", default=""))
            if not expiry or strike <= 0 or not symbol or right not in ("call", "put"):
                log.warning("skipping unparseable option position: %s", row)
                continue

            # Average price is quoted per contract by some endpoints and per share
            # by others. Normalise to per share, which is what Position expects.
            avg = as_float(pick(row, "average_price", "average_open_price", "avg_price"))
            if avg > strike:
                avg /= 100.0
            out.append(
                Position(
                    contract=OptionContract(
                        symbol,
                        expiry,
                        strike,
                        right,
                        broker_id=str(pick(row, "option_id", "instrument_id", "id", default="")),
                    ),
                    quantity=quantity,
                    entry_price=avg,
                )
            )
        return out

    def day_trades_used(self) -> int:
        """Day-trade count is not exposed by the option tool surface.

        Returning the limit is the safe direction to be wrong in: PDT protection
        will hold back entries rather than spend a day trade the account may not
        have. Set ``risk.pdt_protection: false`` if the account is above $25k.
        """
        return 3

    # ---- execution -------------------------------------------------------

    def _order_payload(
        self, contract: OptionContract, side: Side, quantity: int, limit_price: float
    ) -> dict[str, Any]:
        return {
            "symbol": contract.symbol,
            "expiration_date": contract.expiry.strftime("%Y-%m-%d"),
            "strike_price": contract.strike,
            "option_type": contract.right,
            "side": side.value,
            "position_effect": "open" if side is Side.BUY else "close",
            "quantity": quantity,
            "order_type": "limit",
            "limit_price": round(limit_price, 2),
            "time_in_force": self.time_in_force,
        }

    def _submit(
        self, contract: OptionContract, side: Side, quantity: int, limit_price: float
    ) -> Fill | None:
        payload = self._order_payload(contract, side, quantity, limit_price)

        review = self.api.review_order(**payload)
        self._last_review = review
        blocked, reason = review_blocks_order(review)
        if blocked:
            log.error("broker review blocked %s %s: %s", side.value, contract, reason)
            return None

        if self.dry_run:
            log.info("DRY RUN (reviewed, not placed): %s", payload)
            return None

        if self.require_approval:
            if self.approval_callback is None:
                log.error("require_approval is set but no approval callback was provided")
                return None
            if not self.approval_callback(payload, review):
                log.info("order rejected at approval: %s", payload)
                return None

        result = self.api.place_order(**payload)
        order_id = str(pick(result, "id", "order_id", default=""))
        if not order_id:
            log.error("place_option_order returned no order id: %s", result)
            return None

        # An accepted order is not a filled order. Report the fill price the broker
        # gives us and fall back to the limit only when it has not filled yet.
        state = str(pick(result, "state", "status", default="")).lower()
        fill_price = as_float(pick(result, "average_price", "price"), limit_price)
        if fill_price > contract.strike:
            fill_price /= 100.0
        if state and state not in ("filled", "partially_filled"):
            log.info("order %s accepted in state '%s'; awaiting fill", order_id, state)
            return None

        filled = as_int(pick(result, "quantity", "filled_quantity"), quantity)
        return Fill(contract, side, filled or quantity, fill_price, order_id=order_id)

    def buy_to_open(self, quote: OptionQuote, quantity: int, limit_price: float) -> Fill | None:
        return self._submit(quote.contract, Side.BUY, quantity, limit_price)

    def sell_to_close(
        self, position: Position, quote: OptionQuote, limit_price: float, urgent: bool = False
    ) -> Fill | None:
        price = quote.bid if urgent else limit_price
        return self._submit(position.contract, Side.SELL, position.quantity, price)

    def settle_expiration(self, position: Position, intrinsic_per_share: float) -> float:
        """The broker settles expirations itself; nothing to do here."""
        return max(0.0, intrinsic_per_share) * 100 * position.quantity

    def has_open_order(self, occ_symbol: str) -> bool | None:
        try:
            orders = self.open_orders()
        except Exception:
            log.warning("could not list open orders; treating the contract as possibly working")
            return None
        symbol = occ_symbol[:6].rstrip()
        return any(symbol in str(pick(row, "chain_symbol", "symbol", default="")) for row in orders)

    def open_orders(self) -> list[dict[str, Any]]:
        """Live orders, used to detect a duplicate the registry does not know about."""
        live = ("queued", "confirmed", "unconfirmed", "partially_filled", "pending")
        return [
            row
            for row in self.api.option_orders()
            if str(pick(row, "state", "status", default="")).lower() in live
        ]

    def cancel_all(self) -> None:
        for row in self.open_orders():
            order_id = str(pick(row, "id", "order_id", default=""))
            if not order_id:
                continue
            if self.dry_run:
                log.info("DRY RUN: would cancel order %s", order_id)
                continue
            self.api.cancel_order(order_id)

    @property
    def last_review(self) -> dict[str, Any]:
        return self._last_review

    @staticmethod
    def describe_order(payload: dict[str, Any], review: dict[str, Any]) -> str:
        """Human-readable proposal, for the approval prompt and notifications."""
        alerts = review.get("alerts") or review.get("warnings") or []
        lines = [
            "PROPOSED ORDER",
            f"  {payload['side'].upper()} {payload['quantity']}x {payload['symbol']} "
            f"{payload['expiration_date']} {payload['strike_price']:g} "
            f"{payload['option_type'].upper()}",
            f"  Limit ${payload['limit_price']:.2f} per share "
            f"(${payload['limit_price'] * 100 * payload['quantity']:,.0f} total)",
            f"  Time in force: {payload['time_in_force']}",
        ]
        estimated = as_float(pick(review, "estimated_cost", "total_cost", "debit"))
        if estimated:
            lines.append(f"  Broker estimate: ${estimated:,.2f}")
        if alerts:
            lines.append(f"  Broker alerts: {alerts}")
        lines.append(f"  Prepared at {datetime.now():%Y-%m-%d %H:%M:%S}")
        return "\n".join(lines)
