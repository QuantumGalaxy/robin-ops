"""Robinhood order routing via ``robin_stocks``.

Safety properties this adapter is built around:

* ``dry_run`` defaults to True. Nothing is submitted until it is explicitly
  turned off, and every would-be order is logged in full first.
* Only limit orders. There is no code path that sends a market order.
* Only ``buy_to_open`` and ``sell_to_close``. The agent cannot open a short
  option position, so the worst case on any single trade is the premium paid.
* Orders are submitted ``gfd`` (good for day) rather than ``gtc``, so a stale
  order cannot fill days later against a thesis that no longer holds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..models import Fill, OptionContract, OptionQuote, Position, Side
from .base import Broker

log = logging.getLogger(__name__)


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RobinhoodBroker(Broker):
    dry_run: bool = True
    time_in_force: str = "gfd"

    def __post_init__(self) -> None:
        try:
            import robin_stocks.robinhood as rh
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "robin_stocks is not installed. Install with: uv pip install -e '.[robinhood]'"
            ) from exc
        self._rh = rh
        if self.dry_run:
            log.warning("RobinhoodBroker is in DRY RUN: orders will be logged, not submitted")

    # ---- account ---------------------------------------------------------

    def equity(self) -> float:
        profile = self._rh.profiles.load_portfolio_profile() or {}
        return _f(profile.get("equity") or profile.get("extended_hours_equity"))

    def buying_power(self) -> float:
        account = self._rh.profiles.load_account_profile() or {}
        return _f(account.get("buying_power") or account.get("portfolio_cash"))

    def day_trades_used(self) -> int:
        try:
            calls = self._rh.account.get_day_trades() or {}
            return int(calls.get("day_trade_count", 0))
        except Exception:  # pragma: no cover - endpoint is not always present
            log.warning("could not read day-trade count; assuming worst case")
            return 3

    def positions(self) -> list[Position]:
        out: list[Position] = []
        for row in self._rh.options.get_open_option_positions() or []:
            qty = int(_f(row.get("quantity")))
            if qty <= 0 or row.get("type") == "short":
                continue
            instrument_id = row.get("option_id") or ""
            meta = self._rh.options.get_option_instrument_data_by_id(instrument_id) or {}
            try:
                expiry = datetime.strptime(meta["expiration_date"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            contract = OptionContract(
                symbol=row.get("chain_symbol", ""),
                expiry=expiry,
                strike=_f(meta.get("strike_price")),
                right=meta.get("type", "call"),
                broker_id=instrument_id,
            )
            out.append(
                Position(
                    contract=contract,
                    quantity=qty,
                    entry_price=_f(row.get("average_price")) / 100.0,
                )
            )
        return out

    # ---- execution -------------------------------------------------------

    def _submit(self, side: Side, contract: OptionContract, quantity: int, price: float):
        payload = {
            "side": side.value,
            "symbol": contract.symbol,
            "expiration": contract.expiry.strftime("%Y-%m-%d"),
            "strike": contract.strike,
            "right": contract.right,
            "quantity": quantity,
            "limit_price": round(price, 2),
            "tif": self.time_in_force,
        }
        if self.dry_run:
            log.info("DRY RUN order (not submitted): %s", payload)
            return None
        raise RuntimeError("Legacy live execution is disabled; use simulation mode")

    def buy_to_open(self, quote: OptionQuote, quantity: int, limit_price: float) -> Fill | None:
        result = self._submit(Side.BUY, quote.contract, quantity, limit_price)
        if not result or "id" not in result:
            return None
        return Fill(
            quote.contract, Side.BUY, quantity, limit_price, order_id=str(result.get("id", ""))
        )

    def sell_to_close(
        self, position: Position, quote: OptionQuote, limit_price: float, urgent: bool = False
    ) -> Fill | None:
        # For urgent exits, cross to the bid so the order is marketable rather
        # than resting while the position keeps bleeding.
        price = quote.bid if urgent else limit_price
        result = self._submit(Side.SELL, position.contract, position.quantity, price)
        if not result or "id" not in result:
            return None
        return Fill(
            position.contract,
            Side.SELL,
            position.quantity,
            price,
            order_id=str(result.get("id", "")),
        )

    def cancel_all(self) -> None:
        if self.dry_run:
            log.info("DRY RUN: would cancel all open option orders")
            return
        raise RuntimeError("Legacy live execution is disabled")
