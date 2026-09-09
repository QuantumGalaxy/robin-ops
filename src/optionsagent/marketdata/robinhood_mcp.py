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
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..greeks import compute_greeks, implied_vol, years_to_expiry
from ..mcp.client import ToolCaller
from ..mcp.robinhood import RobinhoodMcp, as_date, as_float, as_int, pick
from ..models import OptionContract, OptionQuote
from .base import MarketDataProvider
from .reference import ReferenceSnapshot

log = logging.getLogger(__name__)

QUOTE_BATCH_SIZE = 20


@dataclass
class RobinhoodMcpMarketData(MarketDataProvider):
    caller: ToolCaller
    reference_data_file: str | None = None
    risk_free_rate: float = 0.042
    dividend_yield: float = 0.0
    _instruments: dict[str, list[dict]] = field(default_factory=dict, init=False)
    _spot_times: dict[str, datetime] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.api = RobinhoodMcp(self.caller)
        missing = self.api.verify_tools(
            (
                "get_equity_quotes",
                "get_option_chains",
                "get_option_instruments",
                "get_option_quotes",
            )
        )
        if missing:
            raise RuntimeError("Missing market-data MCP tools: " + ", ".join(missing))

    @staticmethod
    def _today() -> date:
        return datetime.now(ZoneInfo("America/New_York")).date()

    # ---- underlying ------------------------------------------------------

    def underlying_price(self, symbol: str) -> float | None:
        row = self.api.equity_quote(symbol)
        if row.get("has_traded") is False or row.get("state", "active") != "active":
            return None
        # Use the regular-session trade timestamp paired with its price.
        try:
            stamp = datetime.fromisoformat(
                str(pick(row, "venue_last_trade_time", "updated_at", "as_of", "timestamp")).replace(
                    "Z", "+00:00"
                )
            )
            if stamp.tzinfo is None or not -5 <= (datetime.now(UTC) - stamp).total_seconds() <= 120:
                return None
        except (ValueError, TypeError):
            return None
        self._spot_times[symbol] = stamp
        price = as_float(pick(row, "last_trade_price", "last_price", "price", "mark_price", "mark"))
        if price <= 0:
            bid = as_float(pick(row, "bid_price", "bid"))
            ask = as_float(pick(row, "ask_price", "ask"))
            price = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        return price or None

    def historical_closes(self, symbol: str, days: int = 90) -> list[float]:
        """Completed daily bars from the timestamped reference feed."""
        # Timestamped reference feed contains completed daily bars only.
        reference = self._reference().get("symbols", {}).get(symbol, {})
        closes = reference.get("daily_closes", [])
        last_date = as_date(reference.get("daily_closes_as_of"))
        from ..reference_feed import last_completed

        required = last_completed(datetime.now(ZoneInfo("America/New_York")))
        if closes and last_date == required:
            return list(closes[-days:])
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
            raw = self.api.option_instruments(symbol, expiry, right)
            self._instruments[key] = [
                r
                for r in raw
                if r.get("chain_symbol", symbol) == symbol
                and r.get("type", right) == right
                and r.get("expiration_date", expiry.isoformat()) == expiry.isoformat()
                and r.get("state", "active") == "active"
                and r.get("tradability", "tradable") == "tradable"
                and as_float(r.get("trade_value_multiplier", 100)) == 100
                and r.get("underlying_type", "equity") == "equity"
            ]
        return self._instruments[key]

    def _build_quote(self, row: dict, contract: OptionContract, spot: float) -> OptionQuote | None:
        bid = as_float(pick(row, "bid_price", "bid"))
        ask = as_float(pick(row, "ask_price", "ask"))
        if (
            not all(math.isfinite(v) for v in (bid, ask, spot))
            or bid <= 0
            or spot <= 0
            or ask < bid
        ):
            return None

        stamp = pick(row, "updated_at", "as_of", "timestamp")
        try:
            at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if at.tzinfo is None or (at - datetime.now(UTC)).total_seconds() > 5:
            return None
        at = min(at, self._spot_times.get(contract.symbol, at))
        quote = OptionQuote(
            as_of=at,
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
        if iv is not None and math.isfinite(iv) and iv > 0:
            quote.greeks = compute_greeks(
                spot,
                contract.strike,
                T,
                self.risk_free_rate,
                iv,
                self.dividend_yield,
                contract.right,
            )
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
            for item in quotes:
                returned_id = (
                    str(pick(item, "instrument_id", "id", "option_id", "instrument", default=""))
                    .rstrip("/")
                    .rsplit("/", 1)[-1]
                )
                if returned_id == instrument_id:
                    return self._build_quote(item, contract, spot)
        return None

    def _reference(self) -> dict:
        """A current, sourced snapshot from a separate data feed; unknown fails closed."""
        if not self.reference_data_file:
            return {}
        try:
            data = ReferenceSnapshot.model_validate_json(
                Path(self.reference_data_file).read_text()
            ).model_dump(mode="json")
            stamp = datetime.fromisoformat(data["as_of"].replace("Z", "+00:00"))
            age = (datetime.now(UTC) - stamp).total_seconds()
            if not 0 <= age <= 86400 or not data.get("source"):
                return {}
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def iv_rank(self, symbol: str) -> float | None:
        row = self._reference().get("symbols", {}).get(symbol, {})
        value = row.get("iv_rank")
        # Caller must provide trailing daily ATM IV rank, never cross-strike IVs.
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            return None
        if row.get("iv_history_days", 0) < 200:
            return None
        return float(value)

    def earnings_known(self, symbol: str) -> bool:
        row = self._reference().get("symbols", {}).get(symbol, {})
        if row.get("earnings_checked") is not True or "earnings" not in row:
            return False
        return row["earnings"] is None or (as_date(row["earnings"]) or date.min) >= self._today()

    def next_earnings_date(self, symbol: str) -> date | None:
        return as_date(self._reference().get("symbols", {}).get(symbol, {}).get("earnings"))

    def is_market_open(self) -> bool:
        session = self._reference().get("session", {})
        try:
            opened = datetime.fromisoformat(session["open"].replace("Z", "+00:00"))
            closed = datetime.fromisoformat(session["close"].replace("Z", "+00:00"))
            return opened <= datetime.now(UTC) < closed
        except (KeyError, TypeError, ValueError):
            return False
