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
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
        if isinstance(payload.get("data"), dict):
            return rows(payload["data"])
        for key in (
            "results",
            "data",
            "items",
            "options",
            "positions",
            "orders",
            "quotes",
            "chains",
            "instruments",
        ):
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
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    number = as_float(value, default)
    return int(number) if float(number).is_integer() else default


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

    def verify_tools(self, required=REQUIRED_TOOLS) -> list[str]:
        """Return the tools this agent needs that the account does not expose.

        Worth calling at startup: options access is a per-account entitlement, so
        a missing ``place_option_order`` means level 2 approval is pending rather
        than that something is broken.
        """
        try:
            available = {t.get("name") for t in self.caller.list_tools()}
        except Exception:
            raise RuntimeError("Cannot verify MCP capabilities; connection blocked") from None
        return [name for name in required if name not in available]

    # ---- account ---------------------------------------------------------

    def portfolio(self) -> dict[str, Any]:
        result = rows(self.caller.call(GET_PORTFOLIO, {}))
        return result[0] if result else {}

    def accounts(self) -> list[dict[str, Any]]:
        return rows(self.caller.call(GET_ACCOUNTS, {}))

    def equity_quote(self, symbol: str) -> dict[str, Any]:
        result = rows(self.caller.call(GET_EQUITY_QUOTES, {"symbols": [symbol]}))
        for row in result:
            quote = row.get("quote", row)
            if isinstance(quote, dict) and quote.get("symbol", symbol) == symbol:
                return quote
        return {}

    # ---- options ---------------------------------------------------------

    def option_chains(self, symbol: str) -> dict[str, Any]:
        result = rows(self.caller.call(GET_OPTION_CHAINS, {"underlying_symbol": symbol}))
        valid = [
            r
            for r in result
            if r.get("symbol", symbol) == symbol
            and as_float(r.get("trade_value_multiplier", 100)) == 100
            and not r.get("cash_component")
            and not r.get("settle_on_open", False)
        ]
        if len(valid) != 1:
            return {}
        return valid[0]

    def option_instruments(
        self, symbol: str, expiration: date | None = None, option_type: str | None = None
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {
            "chain_symbol": symbol,
            "state": "active",
            "tradability": "tradable",
        }
        if expiration:
            args["expiration_dates"] = expiration.strftime("%Y-%m-%d")
        if option_type:
            args["type"] = option_type
        return self.paginated(GET_OPTION_INSTRUMENTS, args)

    def option_quotes(self, instrument_ids: list[str]) -> list[dict[str, Any]]:
        if not instrument_ids:
            return []
        return [
            q
            for r in rows(self.caller.call(GET_OPTION_QUOTES, {"instrument_ids": instrument_ids}))
            if isinstance(q := r.get("quote", r), dict)
        ]

    def paginated(self, name, args):
        out, seen = [], set()
        args = dict(args)
        for _ in range(100):
            response = self.caller.call(name, args)
            out.extend(rows(response))
            data = response.get("data", response) if isinstance(response, dict) else {}
            next_url = data.get("next")
            if not next_url:
                return out
            cursor = parse_qs(urlsplit(next_url).query).get("cursor", [])
            if len(cursor) != 1 or cursor[0] in seen:
                raise ValueError("Invalid or repeated pagination cursor")
            seen.add(cursor[0])
            args["cursor"] = cursor[0]
        raise ValueError("Pagination limit exceeded")

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
