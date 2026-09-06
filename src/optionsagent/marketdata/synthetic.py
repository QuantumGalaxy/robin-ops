"""A synthetic options market.

This exists for two reasons. First, it lets the agent run end to end with no
brokerage credentials, so the logic can be exercised and reviewed safely.
Second, it is the engine behind ``optionsagent simulate``, which is how the
exit rules get stress-tested across thousands of paths before any real money is
involved.

The model is deliberately simple but not naive:

* underlyings follow geometric Brownian motion with a fat-tailed jump component,
  because equity options are priced off a distribution with far more 3-sigma
  days than a pure lognormal;
* implied volatility mean-reverts and is negatively correlated with returns, so
  a long call loses vega exactly when it also loses delta (the real reason
  buying calls into a rally so often disappoints);
* a volatility smile makes wings more expensive than the at-the-money strike;
* spreads widen for low-delta, low-DTE, and low-liquidity contracts.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..config import DEFAULT_UNIVERSE
from ..greeks import compute_greeks, years_to_expiry
from ..models import OptionContract, OptionQuote, Right
from .base import MarketDataProvider

# Rough starting prices and annualised vols. Only relative magnitudes matter for
# simulation, but realistic levels keep contract prices in a plausible range.
_DEFAULT_SPEC: dict[str, tuple[float, float]] = {
    "AAPL": (228.0, 0.26),
    "MSFT": (415.0, 0.24),
    "NVDA": (128.0, 0.48),
    "AMZN": (186.0, 0.30),
    "GOOGL": (168.0, 0.28),
    "META": (520.0, 0.33),
    "AVGO": (168.0, 0.40),
    "TSLA": (245.0, 0.55),
    "JPM": (215.0, 0.22),
    "V": (280.0, 0.19),
    "UNH": (580.0, 0.24),
    "XOM": (118.0, 0.23),
    "COST": (890.0, 0.20),
    "HD": (390.0, 0.22),
    "LLY": (900.0, 0.30),
    "AMD": (155.0, 0.45),
    "NFLX": (700.0, 0.32),
    "CRM": (255.0, 0.31),
    "QQQ": (480.0, 0.18),
    "SPY": (560.0, 0.14),
}


@dataclass
class SymbolState:
    symbol: str
    spot: float
    base_vol: float
    iv: float
    drift: float = 0.07
    iv_low: float = 0.0
    iv_high: float = 0.0
    earnings: date | None = None

    def __post_init__(self) -> None:
        if self.iv_low == 0.0:
            self.iv_low = self.base_vol * 0.6
        if self.iv_high == 0.0:
            self.iv_high = self.base_vol * 1.9

    @property
    def iv_rank(self) -> float:
        span = self.iv_high - self.iv_low
        if span <= 0:
            return 0.5
        return min(1.0, max(0.0, (self.iv - self.iv_low) / span))


@dataclass
class SyntheticMarketData(MarketDataProvider):
    """Deterministic-by-seed synthetic chain generator."""

    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    seed: int = 7
    today: date = field(default_factory=date.today)
    risk_free_rate: float = 0.042
    jump_prob: float = 0.02
    """Probability per day of a gap move. Roughly one 'surprise' per two months."""

    jump_scale: float = 3.0
    vol_of_vol: float = 0.9
    iv_beta: float = -4.0
    """Sensitivity of IV to underlying returns. Negative: IV rises when spot falls."""

    smile_curvature: float = 0.55

    variance_risk_premium: float = 1.12
    """Ratio of implied to realised volatility.

    This is the most consequential assumption in the whole simulator. Index and
    large-cap options have historically traded at an implied vol roughly 10-15%
    *above* subsequently realised vol, because option sellers demand to be paid
    for carrying tail risk. Set this to 1.0 and options become a fair bet, which
    makes any premium-buying strategy look good and makes the simulation
    worthless as a test. At 1.12 the buyer starts every trade behind, which is
    the honest baseline a long-options agent has to overcome.
    """

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self._day_fraction = 0.0
        self.state: dict[str, SymbolState] = {}
        for i, sym in enumerate(self.symbols):
            spot, vol = _DEFAULT_SPEC.get(sym, (100.0 + 10 * i, 0.30))
            self.state[sym] = SymbolState(
                symbol=sym,
                spot=spot,
                base_vol=vol,
                iv=vol * self.rng.uniform(0.85, 1.25),
                earnings=self.today + timedelta(days=self.rng.randint(5, 80)),
            )

    # ---- clock -----------------------------------------------------------

    def step(self, days: float = 1.0) -> None:
        """Advance the world by ``days`` calendar days.

        Accepts fractional days so a simulation can poll intraday. That matters
        more than it sounds: with one step per day a +10% profit target
        routinely "fills" at +50% because the mark gapped straight past it
        overnight, which flatters any take-profit rule enormously.
        """
        if not math.isfinite(days) or days < 0:
            raise ValueError("Synthetic time must advance by a finite nonnegative amount")
        dt = days / 365.0
        for st in self.state.values():
            # Prices realise less volatility than options imply. Contracts are
            # quoted off st.iv, but the path is generated with realised vol.
            realised = st.iv / self.variance_risk_premium
            z = self.rng.gauss(0.0, 1.0)
            shock = 0.0
            if self.rng.random() < self.jump_prob * days:
                shock = self.rng.gauss(0.0, 1.0) * self.jump_scale * realised * math.sqrt(dt)
            ret = (st.drift - 0.5 * realised**2) * dt + realised * math.sqrt(dt) * z + shock
            st.spot = max(1.0, st.spot * math.exp(ret))

            # IV mean-reverts to base vol but jumps on down moves.
            pull = 0.15 * days * (st.base_vol - st.iv)
            leverage = self.iv_beta * ret * realised
            noise = self.vol_of_vol * st.iv * math.sqrt(dt) * self.rng.gauss(0.0, 1.0) * 0.3
            st.iv = min(3.0, max(0.05, st.iv + pull + leverage + noise))

            # The long-run vol level itself wanders. Without this, IV reverts to
            # a level the strategy can predict, handing a long-vega position a
            # free edge that does not exist in a real market.
            st.base_vol = min(
                2.0, max(0.05, st.base_vol * math.exp(0.35 * math.sqrt(dt) * self.rng.gauss(0, 1)))
            )

        self._day_fraction += days
        whole = int(self._day_fraction)
        if whole:
            self.today += timedelta(days=whole)
            self._day_fraction -= whole

    # ---- provider interface ---------------------------------------------

    def underlying_price(self, symbol: str) -> float | None:
        st = self.state.get(symbol)
        return st.spot if st else None

    def iv_rank(self, symbol: str) -> float:
        st = self.state.get(symbol)
        return st.iv_rank if st else 0.5

    def historical_closes(self, symbol: str, days: int = 90) -> list[float]:
        """Fabricate a plausible past path that ends at the current spot.

        Generated backwards from today so the series is consistent with the
        live price. Seeded per symbol, so repeated calls are stable.
        """
        st = self.state.get(symbol)
        if st is None:
            return []
        rng = random.Random(
            int.from_bytes(hashlib.sha256(f"{self.seed}|{symbol}".encode()).digest()[:8])
        )
        dt = 1 / 252.0
        path = [st.spot]
        price = st.spot
        for _ in range(days):
            ret = (st.drift - 0.5 * st.base_vol**2) * dt + st.base_vol * math.sqrt(dt) * rng.gauss(
                0.0, 1.0
            )
            price = max(1.0, price / math.exp(ret))
            path.append(price)
        return list(reversed(path))

    def earnings_known(self, symbol: str) -> bool:
        return True

    def next_earnings_date(self, symbol: str) -> date | None:
        st = self.state.get(symbol)
        return st.earnings if st else None

    def _expiries(self, min_dte: int, max_dte: int) -> list[date]:
        """Fridays inside the DTE window, which is how listed expiries work."""
        out = []
        for d in range(max(min_dte, 0), max_dte + 1):
            cand = self.today + timedelta(days=d)
            if cand.weekday() == 4:
                out.append(cand)
        return out

    def _strikes(self, spot: float) -> list[float]:
        step = self._strike_step(spot)
        atm = round(spot / step) * step
        return [atm + step * k for k in range(-10, 11) if atm + step * k > 0]

    @staticmethod
    def _strike_step(spot: float) -> float:
        if spot < 25:
            return 1.0
        if spot < 100:
            return 2.5
        if spot < 250:
            return 5.0
        if spot < 600:
            return 10.0
        return 20.0

    def _smile_iv(self, st: SymbolState, strike: float, T: float) -> float:
        """Vol as a function of moneyness. Downside strikes bid up, as in reality."""
        m = math.log(strike / st.spot)
        skew = -0.9 * m
        curve = self.smile_curvature * m * m
        term = 1.0 + 0.12 * (math.sqrt(max(T, 1 / 365)) - math.sqrt(30 / 365))
        return max(0.05, (st.iv + skew + curve) * term)

    def _spread_pct(self, delta: float, dte: int, price: float, liquid: bool) -> float:
        """Wider spreads for cheap, far-OTM, or short-dated contracts."""
        base = 0.012 if liquid else 0.03
        otm_penalty = 0.10 * max(0.0, 0.5 - abs(delta)) ** 1.3
        cheap_penalty = 0.06 / max(price, 0.2)
        dte_penalty = 0.02 if dte <= 5 else 0.0
        return min(0.5, base + otm_penalty + cheap_penalty * 0.1 + dte_penalty)

    def _build_quote(
        self, st: SymbolState, expiry: date, strike: float, right: Right
    ) -> OptionQuote | None:
        dte = (expiry - self.today).days
        if dte < 0:
            return None
        T = years_to_expiry(dte)
        sigma = self._smile_iv(st, strike, T)
        g = compute_greeks(st.spot, strike, T, self.risk_free_rate, sigma, 0.0, right)
        if g.price < 0.05:
            return None
        liquid = st.symbol in ("SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "AMZN")
        spread_pct = self._spread_pct(g.delta, dte, g.price, liquid)
        half = g.price * spread_pct / 2.0
        tick = 0.05 if g.price >= 3.0 else 0.01
        bid = max(tick, round((g.price - half) / tick) * tick)
        ask = max(bid + tick, round((g.price + half) / tick) * tick)
        moneyness = abs(math.log(strike / st.spot))
        oi = int(max(20, 9000 * math.exp(-14 * moneyness) * (1.6 if liquid else 0.6)))
        return OptionQuote(
            contract=OptionContract(st.symbol, expiry, strike, right),
            bid=round(bid, 2),
            ask=round(ask, 2),
            underlying_price=st.spot,
            open_interest=oi,
            volume=int(oi * 0.12),
            greeks=g,
        )

    def option_chain(self, symbol: str, min_dte: int, max_dte: int) -> list[OptionQuote]:
        st = self.state.get(symbol)
        if st is None:
            return []
        quotes: list[OptionQuote] = []
        for expiry in self._expiries(min_dte, max_dte):
            for strike in self._strikes(st.spot):
                for right in ("call", "put"):
                    q = self._build_quote(st, expiry, strike, right)  # type: ignore[arg-type]
                    if q is not None:
                        quotes.append(q)
        return quotes

    def quote(self, symbol: str, expiry: date, strike: float, right: str) -> OptionQuote | None:
        st = self.state.get(symbol)
        if st is None:
            return None
        return self._build_quote(st, expiry, strike, right)  # type: ignore[arg-type]
