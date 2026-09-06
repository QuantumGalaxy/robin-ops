"""Exit rule engine.

This is the heart of the agent and a direct encoding of the brief:

* take profit at +10% on premium;
* once past +10%, keep riding as long as the position stays above +10%
  (implemented as a trailing stop with a hard floor at the target);
* cut anything down 50%;
* be flat before expiry week rather than holding into gamma risk;
* otherwise time out after two weeks.

Rules are evaluated in strict priority order, most protective first. The
function is pure apart from updating the position's high-water mark, which
makes it trivial to unit test and to replay over historical paths.
"""

from __future__ import annotations

from datetime import date, datetime

from ..config import ExitConfig
from ..models import ExitDecision, ExitReason, OptionQuote, Position, utcnow


def trailing_stop_level(peak_return: float, cfg: ExitConfig) -> float:
    """The return at which a trailing position gets closed.

    Under ``giveback_of_gain`` the stop surrenders ``trailing_giveback_pct`` of
    the peak gain: at a +30% peak with the default 40% giveback it sits at +18%.
    At a +12% peak the arithmetic would say +7.2%, so ``trailing_floor_pct``
    pins it at +10% instead, which is what makes this faithful to "sell if it
    drops back under 10%".

    Under ``pct_of_peak_value`` the stop surrenders a fraction of the option's
    price. A peak return of +30% means the contract is at 1.30x cost, so a 5%
    trail exits at 1.30 * 0.95 = 1.235x, i.e. +23.5%.
    """
    if cfg.trailing_mode == "pct_of_peak_value":
        level = (1.0 + peak_return) * (1.0 - cfg.trailing_giveback_pct) - 1.0
    else:
        level = peak_return * (1.0 - cfg.trailing_giveback_pct)
    return max(cfg.trailing_floor_pct, level)


def evaluate_exit(
    position: Position,
    quote: OptionQuote,
    cfg: ExitConfig,
    *,
    as_of: datetime | None = None,
    earnings_date: date | None = None,
    direction: str | None = None,
) -> ExitDecision:
    now = as_of or utcnow()
    mark = quote.bid if cfg.exit_mark == "bid" else quote.mid
    ret = position.unrealized_return(mark)
    dte = position.contract.days_to_expiry(now.date())

    if ret > position.peak_return:
        position.peak_return = ret
    if not position.trailing_armed and ret >= cfg.take_profit_pct:
        position.trailing_armed = True
    position.last_mark = mark

    # 1. Expiry guard. Nothing below this is worth arguing about: in the last
    #    few days gamma dominates, spreads widen, and an unattended loop cannot
    #    react fast enough to defend the position.
    if dte <= cfg.expiry_guard_dte:
        return ExitDecision(
            True,
            ExitReason.EXPIRY_GUARD,
            f"{dte}d to expiry (guard at {cfg.expiry_guard_dte}d), return {ret:+.1%}",
            urgency="urgent",
        )

    # 2. Hard stop loss, checked every loop regardless of time remaining.
    if ret <= cfg.stop_loss_pct:
        return ExitDecision(
            True,
            ExitReason.STOP_LOSS,
            f"return {ret:+.1%} at or below stop {cfg.stop_loss_pct:+.0%}",
            urgency="urgent",
        )

    # 3. Tighter stop as expiry approaches. A 30% loss with under a week left
    #    needs a large, fast move to recover, and theta is working against it.
    if dte <= cfg.expiry_guard_dte + 3 and ret <= cfg.expiry_guard_loss_pct:
        return ExitDecision(
            True,
            ExitReason.STOP_LOSS,
            f"return {ret:+.1%} with only {dte}d left (near-expiry stop "
            f"{cfg.expiry_guard_loss_pct:+.0%})",
            urgency="urgent",
        )

    # Direction is optional for offline rule replay; the live engine passes it.
    if (
        position.trailing_armed
        and cfg.exit_on_signal_loss
        and direction is not None
        and direction != position.contract.right
    ):
        return ExitDecision(True, ExitReason.SIGNAL_LOSS, "direction no longer supports the trade")

    # 4. Profit taking.
    if cfg.trailing_enabled:
        if position.trailing_armed:
            level = trailing_stop_level(position.peak_return, cfg)
            if ret <= level:
                return ExitDecision(
                    True,
                    ExitReason.TRAILING_STOP,
                    f"return {ret:+.1%} fell to trailing stop {level:+.1%} "
                    f"(peak {position.peak_return:+.1%})",
                )
    elif ret >= cfg.take_profit_pct:
        return ExitDecision(
            True,
            ExitReason.TAKE_PROFIT,
            f"return {ret:+.1%} hit target {cfg.take_profit_pct:+.0%}",
        )

    # 5. Earnings. Implied vol collapses the morning after the print; a long
    #    option can be directionally right and still lose money on IV crush.
    if cfg.exit_before_earnings and earnings_date is not None:
        if 0 <= (earnings_date - now.date()).days <= cfg.earnings_exit_days:
            return ExitDecision(
                True,
                ExitReason.TIME_STOP,
                f"earnings on {earnings_date}; closing to avoid IV crush",
                urgency="urgent",
            )

    # 6. Decay escape hatch. If the contract is bleeding fast and the thesis has
    #    not started working, the remaining premium is better redeployed.
    if cfg.theta_bleed_exit and quote.greeks is not None:
        bleed = quote.greeks.theta_pct_per_day
        if bleed >= cfg.theta_bleed_pct_per_day and ret < cfg.take_profit_pct / 2:
            return ExitDecision(
                True,
                ExitReason.THETA_BLEED,
                f"decaying {bleed:.1%}/day with return only {ret:+.1%}",
            )

    # 7. Two-week holding window from the brief.
    if position.days_held(now) >= cfg.max_hold_days:
        return ExitDecision(
            True,
            ExitReason.TIME_STOP,
            f"held {position.days_held(now):.0f}d (max {cfg.max_hold_days}d), return {ret:+.1%}",
        )

    return ExitDecision(False, detail=f"holding, return {ret:+.1%}, {dte}d to expiry")
