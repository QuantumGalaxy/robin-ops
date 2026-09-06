"""Contract selection.

Given a direction from :mod:`optionsagent.strategy.signals`, this picks *which*
contract to buy. The filters are mostly about making the +10% target physically
reachable: the wrong strike or the wrong expiry can make a 10% gain require a
move the underlying almost never produces in two weeks, or make the round-trip
cost of crossing the spread larger than the target itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from ..config import EntryConfig
from ..greeks import bs_price, years_to_expiry
from ..models import Candidate, OptionQuote
from .signals import Direction


def required_underlying_move(
    quote: OptionQuote,
    target_return: float,
    horizon_days: float,
    dte: int,
    risk_free_rate: float = 0.042,
) -> float:
    """Percentage move in the underlying needed to gain ``target_return`` on premium.

    Reprices the contract in full at the end of the holding period and solves for
    the underlying price that gets it to the target, rather than extrapolating
    from delta, gamma, and theta. The Taylor expansion is badly wrong here: over
    a two-week horizon on a short-dated contract the Greeks themselves move so
    much that the approximation can be off by a factor of two or more.

    Volatility is held constant, so this measures the *directional* move needed
    and deliberately says nothing about IV expanding or collapsing. Returns
    ``inf`` when no plausible move gets there before decay does.
    """
    g = quote.greeks
    S = quote.underlying_price
    if g is None or S <= 0 or g.price <= 0 or g.iv <= 0:
        return math.inf

    right = quote.contract.right
    target_price = (1.0 + target_return) * g.price
    # Time left on the contract once the holding period is over.
    T_future = years_to_expiry(max(dte - horizon_days, 0.0))

    def value_at(spot: float) -> float:
        return bs_price(spot, quote.contract.strike, T_future, risk_free_rate, g.iv, 0.0, right)

    # Search the favourable direction: up for calls, down for puts.
    lo, hi = (S, S * 4.0) if right == "call" else (S * 0.05, S)
    extreme = hi if right == "call" else lo
    if value_at(extreme) < target_price:
        return math.inf

    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if (value_at(mid) < target_price) == (right == "call"):
            lo = mid
        else:
            hi = mid
    return abs(0.5 * (lo + hi) - S) / S


def move_in_sigmas(quote: OptionQuote, move_pct: float, horizon_days: float) -> float:
    """Express a required move as a multiple of the expected move over the horizon.

    2.0 sigma means the market is pricing roughly a 1-in-20 chance of getting
    there. That is the honest way to read a "+10% target": it is only modest
    when the contract is chosen so that 10% of premium is a small move in the
    stock.
    """
    g = quote.greeks
    if g is None or g.iv <= 0 or not math.isfinite(move_pct):
        return math.inf
    expected = g.iv * math.sqrt(max(horizon_days, 1.0) / 365.0)
    if expected <= 0:
        return math.inf
    return move_pct / expected


@dataclass
class EntryScreener:
    cfg: EntryConfig
    target_return: float = 0.10
    horizon_days: float = 14.0
    max_required_sigma: float = 1.25
    """Reject contracts where hitting the target needs more than a 1.25-sigma
    move over the holding period. This single filter does more to make the
    strategy viable than any other."""

    def screen(
        self,
        quotes: list[OptionQuote],
        direction: Direction,
        *,
        iv_rank: float = 0.5,
        days_to_earnings: int | None = None,
        confidence: float = 0.5,
        as_of: date | None = None,
    ) -> list[Candidate]:
        if direction == "none" or direction not in self.cfg.allowed_rights:
            return []
        if (
            days_to_earnings is not None
            and 0 <= days_to_earnings <= self.cfg.avoid_earnings_within_days
        ):
            return []
        if iv_rank > self.cfg.max_iv_rank:
            return []

        out: list[Candidate] = []
        for q in quotes:
            cand = self._evaluate(q, direction, iv_rank, confidence, as_of)
            if cand is not None:
                out.append(cand)
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    def _evaluate(
        self,
        q: OptionQuote,
        direction: Direction,
        iv_rank: float,
        confidence: float,
        as_of: date | None = None,
    ) -> Candidate | None:
        c = q.contract
        if c.right != direction or not q.is_tradeable() or q.greeks is None:
            return None

        dte = c.days_to_expiry(as_of)
        if not (self.cfg.min_dte <= dte <= self.cfg.max_dte):
            return None
        if not (self.cfg.min_premium <= q.mid <= self.cfg.max_premium):
            return None
        if q.spread_pct > self.cfg.max_spread_pct:
            return None
        if q.open_interest < self.cfg.min_open_interest:
            return None
        if q.volume < self.cfg.min_volume:
            return None

        g = q.greeks
        abs_delta = abs(g.delta)
        if not (self.cfg.min_abs_delta <= abs_delta <= self.cfg.max_abs_delta):
            return None
        if g.theta_pct_per_day > self.cfg.max_theta_pct_per_day:
            return None
        if g.gamma * 0.01 * q.underlying_price > self.cfg.max_delta_change_per_1pct:
            return None

        move_pct = required_underlying_move(q, self.target_return, self.horizon_days, dte)
        sigmas = move_in_sigmas(q, move_pct, self.horizon_days)
        if not math.isfinite(sigmas) or sigmas > self.max_required_sigma:
            return None

        score = self._score(q, g, sigmas, iv_rank, confidence)
        reasons = [
            f"delta {g.delta:+.2f}",
            f"theta {g.theta_pct_per_day:.2%}/day",
            f"spread {q.spread_pct:.2%}",
            f"needs {move_pct:.2%} move ({sigmas:.2f} sigma) for +{self.target_return:.0%}",
            f"IV {g.iv:.1%} (rank {iv_rank:.0%})",
        ]
        return Candidate(
            quote=q,
            score=score,
            delta=g.delta,
            theta_pct_per_day=g.theta_pct_per_day,
            gamma=g.gamma,
            vega=g.vega,
            iv=g.iv,
            iv_rank=iv_rank,
            dte=dte,
            reasons=reasons,
        )

    def _score(self, q: OptionQuote, g, sigmas: float, iv_rank: float, confidence: float) -> float:
        """Rank survivors. Weights favour reachability and cost over everything else."""
        reachability = 1.0 / (1.0 + sigmas)
        cost = 1.0 - min(1.0, q.spread_pct / max(self.cfg.max_spread_pct, 1e-6))
        decay = 1.0 - min(1.0, g.theta_pct_per_day / max(self.cfg.max_theta_pct_per_day, 1e-6))
        vol_value = 1.0 - min(1.0, max(0.0, iv_rank))
        delta_fit = 1.0 - min(
            1.0,
            abs(abs(g.delta) - 0.60) / max(self.cfg.max_abs_delta - self.cfg.min_abs_delta, 0.1),
        )
        liquidity = min(1.0, math.log10(max(q.open_interest, 1)) / 4.0)
        return (
            0.34 * reachability
            + 0.20 * cost
            + 0.16 * decay
            + 0.12 * vol_value
            + 0.10 * delta_fit
            + 0.05 * liquidity
            + 0.03 * confidence
        )
