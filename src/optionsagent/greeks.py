"""Black-Scholes pricing, Greeks, and implied-volatility solving.

Robinhood's option chain does not expose a reliable, consistently-populated set of
Greeks, so the agent derives its own from the mid price. Everything here is
closed-form and dependency-free apart from ``math``.

Conventions used throughout the codebase:

* ``T`` is time to expiry in **years** (calendar days / 365).
* ``sigma`` is annualised implied volatility as a decimal (0.35 == 35%).
* ``theta`` is returned as **dollars per contract per calendar day** (already
  multiplied by the 100x contract multiplier and divided by 365).
* ``vega`` is returned as **dollars per contract per 1 volatility point**.
* ``delta`` and ``gamma`` are per-share (the textbook values), because that is
  how traders quote them. Multiply by 100 for per-contract exposure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Right = Literal["call", "put"]

CONTRACT_MULTIPLIER = 100
DAYS_PER_YEAR = 365.0

# Numerical floors. An option with 0 days or 0 vol has no meaningful Greeks, but
# we still want the functions to return something finite rather than blow up.
_MIN_T = 1e-6
_MIN_SIGMA = 1e-6


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float) -> tuple[float, float]:
    T = max(T, _MIN_T)
    sigma = max(sigma, _MIN_SIGMA)
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def intrinsic(S: float, K: float, right: Right) -> float:
    return max(0.0, S - K) if right == "call" else max(0.0, K - S)


def european_lower_bound(
    S: float, K: float, T: float, r: float, q: float = 0.0, right: Right = "call"
) -> float:
    """Arbitrage floor for a European option, using discounted forwards."""
    disc_k = K * math.exp(-r * T)
    disc_s = S * math.exp(-q * T)
    return max(0.0, disc_s - disc_k) if right == "call" else max(0.0, disc_k - disc_s)


def bs_price(
    S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0, right: Right = "call"
) -> float:
    """Black-Scholes-Merton price of one share of the option (not the contract)."""
    if T <= _MIN_T or sigma <= _MIN_SIGMA:
        return intrinsic(S, K, right)
    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if right == "call":
        return S * disc_q * norm_cdf(d1) - K * disc_r * norm_cdf(d2)
    return K * disc_r * norm_cdf(-d2) - S * disc_q * norm_cdf(-d1)


@dataclass(frozen=True)
class Greeks:
    """Risk sensitivities of a single option contract.

    ``delta``/``gamma`` are per-share; ``theta``/``vega`` are per-contract dollars.
    """

    price: float
    """Theoretical price per share."""
    delta: float
    """Change in option price per $1 move in the underlying. 0.60 == moves 60c per $1."""
    gamma: float
    """Change in delta per $1 move. High gamma means delta moves fast against you."""
    theta: float
    """Dollars the contract bleeds per calendar day, all else equal. Negative when long."""
    vega: float
    """Dollars the contract gains per +1 point of implied volatility."""
    rho: float
    """Dollars gained per +1 percentage point of the risk-free rate. Mostly ignorable."""
    iv: float
    """The implied volatility the rest of these numbers were computed at."""

    @property
    def theta_pct_per_day(self) -> float:
        """Daily decay as a fraction of the contract's own value.

        This is the number that actually matters for a premium-buying strategy:
        a contract bleeding 3%/day needs a >3%/day move in its favour just to
        stay flat, which is why short-dated long options are so punishing.
        """
        contract_value = self.price * CONTRACT_MULTIPLIER
        if contract_value <= 0:
            return 0.0
        return abs(self.theta) / contract_value


def compute_greeks(
    S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0, right: Right = "call"
) -> Greeks:
    price = bs_price(S, K, T, r, sigma, q, right)
    if T <= _MIN_T or sigma <= _MIN_SIGMA:
        itm = intrinsic(S, K, right) > 0
        delta = (1.0 if right == "call" else -1.0) if itm else 0.0
        return Greeks(price=price, delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0, iv=sigma)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    sqrt_t = math.sqrt(T)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    pdf_d1 = norm_pdf(d1)

    gamma = disc_q * pdf_d1 / (S * sigma * sqrt_t)
    vega_per_share = S * disc_q * pdf_d1 * sqrt_t  # per 1.0 of vol
    common_theta = -(S * disc_q * pdf_d1 * sigma) / (2 * sqrt_t)

    if right == "call":
        delta = disc_q * norm_cdf(d1)
        theta_per_year = (
            common_theta - r * K * disc_r * norm_cdf(d2) + q * S * disc_q * norm_cdf(d1)
        )
        rho_per_share = K * T * disc_r * norm_cdf(d2)
    else:
        delta = -disc_q * norm_cdf(-d1)
        theta_per_year = (
            common_theta + r * K * disc_r * norm_cdf(-d2) - q * S * disc_q * norm_cdf(-d1)
        )
        rho_per_share = -K * T * disc_r * norm_cdf(-d2)

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        theta=theta_per_year / DAYS_PER_YEAR * CONTRACT_MULTIPLIER,
        vega=vega_per_share / 100.0 * CONTRACT_MULTIPLIER,
        rho=rho_per_share / 100.0 * CONTRACT_MULTIPLIER,
        iv=sigma,
    )


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float = 0.0,
    right: Right = "call",
    *,
    lo: float = 1e-4,
    hi: float = 5.0,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float | None:
    """Solve for the volatility that reproduces ``market_price``.

    Uses bisection rather than Newton-Raphson: vega collapses to nearly zero for
    deep in- or out-of-the-money contracts, and Newton diverges badly there.
    Bisection is slower but cannot run away, which matters when this runs
    unattended against live quotes.

    Returns ``None`` when the price is outside the no-arbitrage band (stale or
    crossed quotes), which the screener treats as "skip this contract".
    """
    if market_price <= 0 or T <= _MIN_T:
        return None
    # The no-arbitrage floor for a European option is the *discounted* payoff,
    # not the intrinsic value. A deep in-the-money European put legitimately
    # trades below K - S when rates are positive, and rejecting those quotes
    # would silently drop every deep ITM put from the screener.
    if market_price < european_lower_bound(S, K, T, r, q, right) - tol:
        return None
    if bs_price(S, K, T, r, hi, q, right) < market_price:
        return None

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        diff = bs_price(S, K, T, r, mid, q, right) - market_price
        if abs(diff) < tol:
            return mid
        if diff < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def years_to_expiry(days: float) -> float:
    return max(days, 0.0) / DAYS_PER_YEAR
