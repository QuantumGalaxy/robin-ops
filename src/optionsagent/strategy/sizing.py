"""Position sizing.

Sizing is derived from the stop, not picked by feel. With a -50% stop, risking
2% of equity per trade means committing 4% of equity in premium, so a full book
of six positions has roughly 24% of the account exposed and a simultaneous
stop-out on all of them costs about 12%. Survivable, which is the whole point.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SizingConfig
from ..greeks import CONTRACT_MULTIPLIER
from ..models import OptionQuote


@dataclass
class SizingDecision:
    contracts: int
    premium: float
    risk_dollars: float
    reason: str = ""


def size_position(
    quote: OptionQuote,
    equity: float,
    buying_power: float,
    open_premium: float,
    cfg: SizingConfig,
    stop_loss_pct: float = -0.50,
) -> SizingDecision:
    price_per_contract = quote.ask * CONTRACT_MULTIPLIER
    if price_per_contract <= 0 or equity <= 0:
        return SizingDecision(0, 0.0, 0.0, "no price or no equity")

    risk_budget = equity * cfg.risk_per_trade_pct
    loss_fraction = abs(stop_loss_pct)
    # Dollars of premium such that hitting the stop costs exactly the risk budget.
    target_premium = risk_budget / max(loss_fraction, 1e-6)

    premium_cap = equity * cfg.max_portfolio_premium_pct - open_premium
    if premium_cap <= 0:
        return SizingDecision(0, 0.0, 0.0, "portfolio premium cap reached")

    cash_cap = buying_power - equity * cfg.cash_reserve_pct
    if cash_cap <= 0:
        return SizingDecision(0, 0.0, 0.0, "cash reserve floor reached")

    allowed = min(target_premium, premium_cap, cash_cap)
    contracts = int(allowed // price_per_contract)
    contracts = min(contracts, cfg.max_contracts)

    if contracts < cfg.min_contracts:
        return SizingDecision(
            0,
            0.0,
            0.0,
            f"one contract costs ${price_per_contract:,.0f}, above the "
            f"${allowed:,.0f} allowed for this trade",
        )

    premium = contracts * price_per_contract
    return SizingDecision(
        contracts=contracts,
        premium=premium,
        risk_dollars=premium * loss_fraction,
        reason=f"{contracts} contract(s), ${premium:,.0f} premium, "
        f"${premium * loss_fraction:,.0f} at risk to the stop",
    )
