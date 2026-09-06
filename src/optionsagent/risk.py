"""Account-level guardrails.

Everything here answers the same question: what stops a bug, a bad day, or a
flash crash from turning into an account-ending loss while nobody is watching.
The rule is that risk checks can only ever *block* entries, never block an exit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import RiskConfig

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    session_date: date | None = None
    session_start_equity: float = 0.0
    peak_equity: float = 0.0
    consecutive_losses: int = 0
    halted_reason: str = ""


@dataclass
class RiskManager:
    cfg: RiskConfig
    state: RiskState = field(default_factory=RiskState)

    def start_session(self, today: date, equity: float) -> None:
        if self.state.session_date != today:
            self.state.session_date = today
            self.state.session_start_equity = equity
            self.state.halted_reason = ""
        self.state.peak_equity = max(self.state.peak_equity, equity)

    def record_trade_result(self, pnl: float) -> None:
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    def kill_switch_engaged(self) -> bool:
        """A file on disk that halts everything.

        Deliberately the crudest possible mechanism: ``touch state/KILL`` stops
        new entries from any shell, with no API call and no restart needed.
        """
        return Path(self.cfg.kill_switch_file).exists()

    def can_open(self, equity: float, day_trades_used: int) -> tuple[bool, str]:
        if self.kill_switch_engaged():
            return False, f"kill switch present at {self.cfg.kill_switch_file}"
        if self.state.halted_reason:
            return False, self.state.halted_reason

        if self.state.session_start_equity > 0:
            session_return = equity / self.state.session_start_equity - 1.0
            if session_return <= -self.cfg.daily_loss_limit_pct:
                self.state.halted_reason = (
                    f"daily loss limit hit ({session_return:+.1%}); no new entries today"
                )
                log.warning(self.state.halted_reason)
                return False, self.state.halted_reason

        if self.state.peak_equity > 0:
            drawdown = equity / self.state.peak_equity - 1.0
            if drawdown <= -self.cfg.max_drawdown_halt_pct:
                self.state.halted_reason = (
                    f"max drawdown breached ({drawdown:+.1%}); agent halted pending review"
                )
                log.error(self.state.halted_reason)
                return False, self.state.halted_reason

        if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
            return False, (
                f"{self.state.consecutive_losses} consecutive losses; "
                "halted for review of the signal"
            )

        blocked, why = self.pdt_blocks_entry(equity, day_trades_used)
        if blocked:
            return False, why
        return True, "ok"

    def pdt_blocks_entry(self, equity: float, day_trades_used: int) -> tuple[bool, str]:
        """Reserve the last day trade for a stop-loss, not a new position.

        Under FINRA's pattern-day-trader rule an account below $25,000 gets
        three day trades per rolling five business days. A +10% profit target on
        a liquid option is routinely hit the same session, so without this
        check the agent would spend its day trades taking small profits and then
        be unable to cut a loser on the day it needs to.
        """
        if not self.cfg.pdt_protection or equity >= self.cfg.pdt_equity_threshold:
            return False, ""
        remaining = self.cfg.pdt_max_day_trades - day_trades_used
        if remaining <= 1:
            return True, (
                f"PDT protection: {day_trades_used}/{self.cfg.pdt_max_day_trades} day trades used "
                "in the rolling window; reserving the remainder for stop-losses"
            )
        return False, ""
