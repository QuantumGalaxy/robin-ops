"""Robinhood market data via the unofficial ``robin_stocks`` client.

Read this before wiring it up to a funded account:

* Robinhood publishes no supported retail options API. ``robin_stocks`` drives
  the same private endpoints the mobile app uses. That is a grey area under
  their terms of service and the endpoints change without notice.
* Quotes are snapshots, not a stream. Polling every symbol in a 20-name universe
  costs real wall-clock time and will get rate limited if you poll aggressively.
  The engine's default 60-second loop is deliberately conservative.
* Greeks returned by the endpoint are frequently null or stale. This adapter
  ignores them entirely and recomputes from the mid price with
  :mod:`optionsagent.greeks`, which is also what keeps the paper and live paths
  numerically identical.
* Auth requires MFA. Supply a TOTP secret via ``ROBINHOOD_MFA_SECRET`` rather
  than typing codes, otherwise the agent cannot re-authenticate unattended.

If any of that is unacceptable, the same strategy runs unchanged against a
broker with a documented options API; only this file and the matching broker
adapter need replacing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime

from ..greeks import compute_greeks, implied_vol, years_to_expiry
from ..models import OptionContract, OptionQuote
from .base import MarketDataProvider

log = logging.getLogger(__name__)


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RobinhoodMarketData(MarketDataProvider):
    risk_free_rate: float = 0.042
    dividend_yield: float = 0.0
    username: str | None = None
    password: str | None = None
    mfa_secret: str | None = None
    _iv_history: dict[str, list[float]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._rh = self._login()

    def _login(self):
        try:
            import robin_stocks.robinhood as rh
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "robin_stocks is not installed. Install the optional extra:\n"
                "    uv pip install -e '.[robinhood]'"
            ) from exc

        user = self.username or os.environ.get("ROBINHOOD_USERNAME")
        pwd = self.password or os.environ.get("ROBINHOOD_PASSWORD")
        secret = self.mfa_secret or os.environ.get("ROBINHOOD_MFA_SECRET")
        if not user or not pwd:
            raise RuntimeError(
                "Set ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD (and ROBINHOOD_MFA_SECRET "
                "for unattended login) before using the robinhood provider."
            )

        mfa_code = None
        if secret:
            import pyotp

            mfa_code = pyotp.TOTP(secret).now()
        rh.login(user, pwd, mfa_code=mfa_code, store_session=True)
        log.info("authenticated to Robinhood as %s", user)
        return rh

    # ---- provider interface ---------------------------------------------

    def underlying_price(self, symbol: str) -> float | None:
        prices = self._rh.stocks.get_latest_price(symbol)
        if not prices or prices[0] is None:
            return None
        return _f(prices[0]) or None

    def _quote_from_payload(
        self, contract: OptionContract, payload: dict, spot: float
    ) -> OptionQuote | None:
        bid = _f(payload.get("bid_price"))
        ask = _f(payload.get("ask_price"))
        if bid <= 0 or ask <= 0:
            return None
        quote = OptionQuote(
            contract=contract,
            bid=bid,
            ask=ask,
            underlying_price=spot,
            open_interest=int(_f(payload.get("open_interest"))),
            volume=int(_f(payload.get("volume"))),
        )
        dte = contract.days_to_expiry(self._today())
        T = years_to_expiry(dte)
        iv = implied_vol(
            quote.mid, spot, contract.strike, T, self.risk_free_rate,
            self.dividend_yield, contract.right,
        )
        if iv is None:
            # Fall back to the broker's own IV rather than dropping the contract;
            # the screener will reject it later if the number is unusable.
            iv = _f(payload.get("implied_volatility"))
        if iv > 0:
            quote.greeks = compute_greeks(
                spot, contract.strike, T, self.risk_free_rate, iv,
                self.dividend_yield, contract.right,
            )
            self._iv_history.setdefault(contract.symbol, []).append(iv)
        return quote

    @staticmethod
    def _today() -> date:
        return datetime.now().date()

    def option_chain(self, symbol: str, min_dte: int, max_dte: int) -> list[OptionQuote]:
        spot = self.underlying_price(symbol)
        if not spot:
            return []
        today = self._today()
        chain = self._rh.options.get_chains(symbol) or {}
        expiries = []
        for raw in chain.get("expiration_dates", []) or []:
            try:
                exp = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            if min_dte <= (exp - today).days <= max_dte:
                expiries.append(exp)

        quotes: list[OptionQuote] = []
        for exp in expiries:
            for right in ("call", "put"):
                rows = (
                    self._rh.options.find_options_by_expiration(
                        symbol, expirationDate=exp.strftime("%Y-%m-%d"), optionType=right
                    )
                    or []
                )
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    strike = _f(row.get("strike_price"))
                    if strike <= 0:
                        continue
                    contract = OptionContract(
                        symbol=symbol,
                        expiry=exp,
                        strike=strike,
                        right=right,  # type: ignore[arg-type]
                        broker_id=str(row.get("id", "")),
                    )
                    q = self._quote_from_payload(contract, row, spot)
                    if q is not None:
                        quotes.append(q)
        return quotes

    def quote(self, symbol: str, expiry: date, strike: float, right: str) -> OptionQuote | None:
        spot = self.underlying_price(symbol)
        if not spot:
            return None
        rows = self._rh.options.get_option_market_data(
            symbol, expiry.strftime("%Y-%m-%d"), str(strike), right
        )
        payload = None
        if rows:
            first = rows[0]
            payload = first[0] if isinstance(first, list) and first else first
        if not isinstance(payload, dict):
            return None
        contract = OptionContract(symbol=symbol, expiry=expiry, strike=strike, right=right)  # type: ignore[arg-type]
        return self._quote_from_payload(contract, payload, spot)

    def historical_closes(self, symbol: str, days: int = 90) -> list[float]:
        span = "year" if days > 90 else "3month"
        rows = (
            self._rh.stocks.get_stock_historicals(
                symbol, interval="day", span=span, bounds="regular"
            )
            or []
        )
        closes = [_f(r.get("close_price")) for r in rows if isinstance(r, dict)]
        return [c for c in closes if c > 0][-days:]

    def iv_rank(self, symbol: str) -> float:
        """Approximate IV rank from IVs observed during this process's lifetime.

        This is a placeholder: a real deployment should persist 52 weeks of
        at-the-money IV per symbol (or pull it from a data vendor). Until that
        history exists the value trends toward 0.5, which makes the IV filter
        permissive rather than wrongly restrictive.
        """
        hist = self._iv_history.get(symbol, [])
        if len(hist) < 30:
            return 0.5
        lo, hi = min(hist), max(hist)
        if hi <= lo:
            return 0.5
        return (hist[-1] - lo) / (hi - lo)

    def is_market_open(self) -> bool:
        try:
            hours = self._rh.markets.get_market_today_hours("XNYS")
            return bool(hours and hours.get("is_open"))
        except Exception:  # pragma: no cover - network dependent
            log.warning("could not determine market hours; assuming closed")
            return False

    def close(self) -> None:
        try:
            self._rh.logout()
        except Exception:  # pragma: no cover
            pass
