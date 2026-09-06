"""Market data through the official Robinhood Trading MCP.

Preferred over :mod:`optionsagent.marketdata.robinhood`, which drives the private
mobile endpoints via ``robin_stocks``. This path is supported, authenticates with
OAuth rather than a scraped password, and exposes options data as a documented
tool surface.

One deliberate choice: even when the API returns Greeks, the agent recomputes them
from the mid price with :mod:`optionsagent.greeks`. A null or stale delta from the
server would otherwise flow straight into position sizing, and keeping one
implementation means the paper and live paths cannot diverge numerically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from ..greeks import compute_greeks, implied_vol, years_to_expiry
from ..mcp.client import McpError, ToolCaller
from ..mcp.robinhood import RobinhoodMcp, as_date, as_float, as_int, pick
from ..models import OptionContract, OptionQuote
from .base import MarketDataProvider

log = logging.getLogger(__name__)

QUOTE_BATCH_SIZE = 50


@dataclass
class RobinhoodMcpMarketData(MarketDataProvider):
    caller: ToolCaller
    risk_free_rate: float = 0.042
    dividend_yield: float = 0.0
    _instruments: dict[str, list[dict]] = field(default_factory=dict, init=False)
    _iv_history: dict[str, list[float]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.api = RobinhoodMcp(self.caller)
        missing = self.api.verify_tools()
        if missing:
            log.error(
                "the connected account does not expose: %s. Options tools require level 2 "
                "approval; call get_option_level_upgrade_info for the application link.",
                ", ".join(missing),
            )

    @staticmethod
    def _today() -> date:
        return datetime.now().date()

    # ---- underlying ------------------------------------------------------

    def underlying_price(self, symbol: str) -> float | None:
        row = self.api.equity_quote(symbol)
        price = as_float(pick(row, "last_trade_price", "last_price", "price", "mark_price", "mark"))
        if price <= 0:
            bid = as_float(pick(row, "bid_price", "bid"))
            ask = as_float(pick(row, "ask_price", "ask"))
            price = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        return price or None

    def historical_closes(self, symbol: str, days: int = 90) -> list[float]:
        """Daily closes for the signal warm-up.

        Equity historicals are not part of the documented option tool surface, so
        this is attempted opportunistically and degrades to an empty list. When it
        comes back empty the agent simply accumulates closes as it runs, which
        means roughly a month of loops before the trend filter produces a
        direction. Point a real data source at this method if that matters.
        """
        for tool in ("get_equity_historicals", "get_historicals", "get_stock_historicals"):
            try:
                payload = self.caller.call(
                    tool, {"symbol": symbol, "interval": "day", "span": "3month"}
                )
            except McpError:
                continue
            from ..mcp.robinhood import rows

            closes = [as_float(pick(r, "close_price", "close", "c")) for r in rows(payload)]
            closes = [c for c in closes if c > 0]
            if closes:
                return closes[-days:]
        log.debug("no equity historicals tool available; signal will warm up from live polls")
        return []

    # ---- option chain ----------------------------------------------------

    def _expirations(self, symbol: str, min_dte: int, max_dte: int) -> list[date]:
        chain = self.api.option_chains(symbol)
        raw = pick(chain, "expiration_dates", "expirations", "expiration_date", default=[])
        if isinstance(raw, str):
            raw = [raw]
        today = self._today()
        out = []
        for item in raw or []:
            exp = as_date(item if isinstance(item, str) else pick(item, "date", "expiration_date"))
            if exp and min_dte <= (exp - today).days <= max_dte:
                out.append(exp)
        return sorted(out)

    def _instrument_rows(self, symbol: str, expiry: date, right: str) -> list[dict]:
        key = f"{symbol}|{expiry:%Y-%m-%d}|{right}"
        if key not in self._instruments:
            self._instruments[key] = self.api.option_instruments(symbol, expiry, right)
        return self._instruments[key]

    def _build_quote(self, row: dict, contract: OptionContract, spot: float) -> OptionQuote | None:
        bid = as_float(pick(row, "bid_price", "bid"))
        ask = as_float(pick(row, "ask_price", "ask"))
        if bid <= 0 or ask <= 0 or ask < bid:
            return None

        quote = OptionQuote(
            contract=contract,
            bid=bid,
            ask=ask,
            underlying_price=spot,
            open_interest=as_int(pick(row, "open_interest", "openInterest")),
            volume=as_int(pick(row, "volume")),
        )
        T = years_to_expiry(contract.days_to_expiry(self._today()))
        iv = implied_vol(
            quote.mid,
            spot,
            contract.strike,
            T,
            self.risk_free_rate,
            self.dividend_yield,
            contract.right,
        )
        if iv is None:
            iv = as_float(pick(row, "implied_volatility", "iv"))
        if iv > 0:
            quote.greeks = compute_greeks(
                spot,
                contract.strike,
                T,
                self.risk_free_rate,
                iv,
                self.dividend_yield,
                contract.right,
            )
            self._iv_history.setdefault(contract.symbol, []).append(iv)
        return quote

    def option_chain(self, symbol: str, min_dte: int, max_dte: int) -> list[OptionQuote]:
        spot = self.underlying_price(symbol)
        if not spot:
            return []

        by_id: dict[str, OptionContract] = {}
        for expiry in self._expirations(symbol, min_dte, max_dte):
            for right in ("call", "put"):
                for row in self._instrument_rows(symbol, expiry, right):
                    strike = as_float(pick(row, "strike_price", "strike"))
                    instrument_id = str(pick(row, "id", "instrument_id", "option_id", default=""))
                    if strike <= 0 or not instrument_id:
                        continue
                    by_id[instrument_id] = OptionContract(
                        symbol=symbol,
                        expiry=expiry,
                        strike=strike,
                        right=right,
                        broker_id=instrument_id,
                    )

        quotes: list[OptionQuote] = []
        ids = list(by_id)
        for start in range(0, len(ids), QUOTE_BATCH_SIZE):
            batch = ids[start : start + QUOTE_BATCH_SIZE]
            for row in self.api.option_quotes(batch):
                instrument_id = (
                    str(pick(row, "instrument_id", "id", "option_id", "instrument", default=""))
                    .rstrip("/")
                    .rsplit("/", 1)[-1]
                )
                contract = by_id.get(instrument_id)
                if contract is None:
                    continue
                q = self._build_quote(row, contract, spot)
                if q is not None:
                    quotes.append(q)
        return quotes

    def quote(self, symbol: str, expiry: date, strike: float, right: str) -> OptionQuote | None:
        spot = self.underlying_price(symbol)
        if not spot:
            return None
        for row in self._instrument_rows(symbol, expiry, right):
            if abs(as_float(pick(row, "strike_price", "strike")) - strike) > 1e-6:
                continue
            instrument_id = str(pick(row, "id", "instrument_id", "option_id", default=""))
            if not instrument_id:
                continue
            contract = OptionContract(
                symbol=symbol, expiry=expiry, strike=strike, right=right, broker_id=instrument_id
            )
            quotes = self.api.option_quotes([instrument_id])
            if quotes:
                return self._build_quote(quotes[0], contract, spot)
        return None

    def iv_rank(self, symbol: str) -> float:
        """Approximate IV rank from IVs seen during this process's lifetime.

        A placeholder, exactly as in the unofficial adapter: a real deployment
        needs 52 weeks of at-the-money IV per symbol. Until that history exists
        this trends to 0.5, making the IV filter permissive rather than wrongly
        restrictive.
        """
        hist = self._iv_history.get(symbol, [])
        if len(hist) < 30:
            return 0.5
        lo, hi = min(hist), max(hist)
        return (hist[-1] - lo) / (hi - lo) if hi > lo else 0.5
