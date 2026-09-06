"""Typed facade over the Robinhood Trading MCP tool surface.

Robinhood documents the *names* of the option tools but not their response
schemas, and the server is still in beta, so field names can move. Everything
here therefore does two things deliberately:

* reads fields through :func:`pick`, which accepts several plausible key names
  and returns a default rather than raising on a missing one;
* normalises any of the shapes a tool might answer with — a bare list, a bare
  dict, ``{"results": [...]}``, ``{"data": [...]}`` — into a list of rows.

Run ``optionsagent mcp-probe`` against your own account before trusting any of
it: it writes a dated snapshot of the live tool schemas so you can diff what the
server actually offers against what this module assumes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .client import ToolCaller

log = logging.getLogger(__name__)

# Tool names as published by Robinhood. Kept as constants so a rename shows up in
# one place rather than scattered through the adapters.
GET_ACCOUNTS = "get_accounts"
GET_PORTFOLIO = "get_portfolio"
GET_EQUITY_QUOTES = "get_equity_quotes"
GET_OPTION_CHAINS = "get_option_chains"
GET_OPTION_INSTRUMENTS = "get_option_instruments"
GET_OPTION_QUOTES = "get_option_quotes"
GET_OPTION_POSITIONS = "get_option_positions"
GET_OPTION_ORDERS = "get_option_orders"
GET_OPTION_HISTORICALS = "get_option_historicals"
REVIEW_OPTION_ORDER = "review_option_order"
PLACE_OPTION_ORDER = "place_option_order"
CANCEL_OPTION_ORDER = "cancel_option_order"
GET_OPTION_LEVEL_UPGRADE_INFO = "get_option_level_upgrade_info"

REQUIRED_TOOLS = (
    GET_OPTION_CHAINS,
    GET_OPTION_INSTRUMENTS,
    GET_OPTION_QUOTES,
    GET_OPTION_POSITIONS,
    REVIEW_OPTION_ORDER,
    PLACE_OPTION_ORDER,
)


def rows(payload: Any) -> list[dict[str, Any]]:
    """Coerce a tool result into a list of dict rows."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items", "options", "positions", "orders", "quotes"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
        return [payload]
    return []


def pick(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    """First present, non-null value among ``names``."""
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    return int(as_float(value, default))


def as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class RobinhoodMcp:
    """Thin wrapper that names the tools and normalises their results."""

    caller: ToolCaller

    def verify_tools(self) -> list[str]:
        """Return the tools this agent needs that the account does not expose.

        Worth calling at startup: options access is a per-account entitlement, so
        a missing ``place_option_order`` means level 2 approval is pending rather
        than that something is broken.
        """
        try:
            available = {t.get("name") for t in self.caller.list_tools()}
        except Exception:
            raise RuntimeError("Cannot verify MCP capabilities; connection blocked") from None
        return [name for name in REQUIRED_TOOLS if name not in available]

    # ---- account ---------------------------------------------------------

    def portfolio(self) -> dict[str, Any]:
        result = rows(self.caller.call(GET_PORTFOLIO, {}))
        return result[0] if result else {}

    def accounts(self) -> list[dict[str, Any]]:
        return rows(self.caller.call(GET_ACCOUNTS, {}))

    def equity_quote(self, symbol: str) -> dict[str, Any]:
        result = rows(self.caller.call(GET_EQUITY_QUOTES, {"symbols": [symbol]}))
        return result[0] if result else {}

    # ---- options ---------------------------------------------------------

    def option_chains(self, symbol: str) -> dict[str, Any]:
        result = rows(self.caller.call(GET_OPTION_CHAINS, {"symbol": symbol}))
        return result[0] if result else {}

    def option_instruments(
        self, symbol: str, expiration: date | None = None, option_type: str | None = None
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"symbol": symbol}
        if expiration:
            args["expiration_date"] = expiration.strftime("%Y-%m-%d")
        if option_type:
            args["type"] = option_type
        return rows(self.caller.call(GET_OPTION_INSTRUMENTS, args))

    def option_quotes(self, instrument_ids: list[str]) -> list[dict[str, Any]]:
        if not instrument_ids:
            return []
        return rows(self.caller.call(GET_OPTION_QUOTES, {"ids": instrument_ids}))

    def option_positions(self) -> list[dict[str, Any]]:
        return rows(self.caller.call(GET_OPTION_POSITIONS, {"status": "open"}))

    def option_orders(self) -> list[dict[str, Any]]:
        return rows(self.caller.call(GET_OPTION_ORDERS, {}))

    def review_order(self, **order: Any) -> dict[str, Any]:
        """Ask the broker to simulate the order and return its pre-trade alerts."""
        result = rows(self.caller.call(REVIEW_OPTION_ORDER, order))
        return result[0] if result else {}

    def place_order(self, **order: Any) -> dict[str, Any]:
        result = rows(self.caller.call(PLACE_OPTION_ORDER, order))
        return result[0] if result else {}

    def cancel_order(self, order_id: str) -> Any:
        return self.caller.call(CANCEL_OPTION_ORDER, {"id": order_id})


def review_blocks_order(review: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a ``review_option_order`` response should stop the trade.

    Robinhood returns pre-trade alerts here. Anything the broker itself flags as
    an error or a rejection is treated as blocking; softer warnings are logged and
    allowed through, because the client-side risk engine has already approved the
    trade on its own terms.
    """
    if not review:
        return True, "review returned nothing; refusing to place the order blind"

    for key in ("error", "reject_reason", "rejection_reason"):
        value = review.get(key)
        if value:
            return True, str(value)

    blocking: list[str] = []
    warnings: list[str] = []
    for alert in review.get("alerts", []) or review.get("warnings", []) or []:
        if isinstance(alert, dict):
            text = str(pick(alert, "message", "text", "title", default=alert))
            severity = str(pick(alert, "severity", "level", "type", default="warning")).lower()
        else:
            text, severity = str(alert), "warning"
        (blocking if severity in ("error", "blocking", "critical") else warnings).append(text)

    if blocking:
        return True, "; ".join(blocking)
    if warnings:
        log.info("broker pre-trade warnings (non-blocking): %s", "; ".join(warnings))
    return False, ""
