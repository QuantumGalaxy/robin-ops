"""Configuration schema.

Every tunable lives here so that the strategy has no magic numbers buried in it.
Defaults encode the recommended parameter set (see ``docs/DESIGN.md``); the
literal rules from the original brief are available via ``config/brief.yaml``
so the two can be simulated side by side.
"""

from __future__ import annotations

import math
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Large-cap, deeply liquid names with weekly expirations and penny-wide or
# near-penny option spreads. Liquidity is the selection criterion here: a 10%
# profit target is unreachable on a contract whose spread is 15% of mid.
DEFAULT_UNIVERSE: list[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "AVGO",
    "TSLA",
    "JPM",
    "V",
    "UNH",
    "XOM",
    "COST",
    "HD",
    "LLY",
    "AMD",
    "NFLX",
    "CRM",
    "MA",
    "SPY",
]


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class UniverseConfig(StrictConfig):
    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    max_symbols: int = Field(default=20, ge=1, le=20)

    @model_validator(mode="after")
    def _dedupe(self) -> UniverseConfig:
        seen: list[str] = []
        for s in self.symbols:
            u = s.upper().strip()
            if u and u not in seen:
                seen.append(u)
        if not seen or len(seen) > self.max_symbols:
            raise ValueError("Choose between one and max_symbols unique symbols")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9.]{0,5}", symbol) for symbol in seen):
            raise ValueError("Invalid universe symbol")
        object.__setattr__(self, "symbols", seen)
        return self


class EntryConfig(StrictConfig):
    """Filters applied to every contract before it can be bought."""

    min_dte: int = 12
    max_dte: int = 18
    """User-requested 12–18 calendar-day contracts; experimental profiles are separate."""

    min_abs_delta: float = 0.55
    max_abs_delta: float = 0.70
    """Slightly in-the-money. Delta ~0.60 means the option captures 60% of the
    underlying's move, needs a far smaller move to reach +10%, and has a much
    tighter relative spread than a cheap 0.20-delta lottery ticket."""

    max_spread_pct: float = 0.06
    """Reject anything whose bid-ask spread exceeds 6% of mid."""

    min_open_interest: int = 500
    min_volume: int = 25
    max_theta_pct_per_day: float = 0.025
    """Reject contracts bleeding more than 2.5% of their own value per day."""

    max_delta_change_per_1pct: float = 0.12
    """Convexity cap, expressed as how much delta moves on a 1% move in the stock
    (``gamma * 0.01 * S``). Near expiry this number explodes, which is what makes
    a short-dated position swing faster than a polling loop can defend it."""

    require_iv_rank: bool = True
    max_iv_rank: float = 0.75
    """Do not buy premium when implied vol is in the top quartile of its own
    52-week range; that is where IV crush does the most damage."""

    min_premium: float = 0.75
    max_premium: float = 25.0
    avoid_earnings_within_days: int = 7
    require_itm: bool = True
    require_known_events: bool = True
    allowed_rights: list[Literal["call", "put"]] = Field(default_factory=lambda: ["call", "put"])


class ExitConfig(StrictConfig):
    """The exit rule set. This is where the brief's requirements are encoded."""

    take_profit_pct: float = 0.10
    """+10% on premium. With ``trailing_enabled`` this arms a trailing stop
    instead of selling outright, which is the "keep it while I'm still up more
    than 10%" behaviour from the brief."""

    trailing_enabled: bool = True
    exit_on_signal_loss: bool = True
    exit_mark: Literal["bid", "mid"] = "bid"

    trailing_mode: Literal["giveback_of_gain", "pct_of_peak_value"] = "giveback_of_gain"
    """How the trailing stop is measured, which matters more than it looks.

    ``giveback_of_gain`` surrenders a fraction of the *profit*: from a +30% peak
    at 40% giveback, the exit sits at +18%. The stop widens as the trade works,
    so a winner is given room proportional to what it has already earned.

    ``pct_of_peak_value`` surrenders a fraction of the option's *price*, which is
    the more common formulation: a 5% trail from a $13.00 peak exits at $12.35.
    On a 0.6-delta contract, 5% of premium is roughly a 0.5% move in the
    underlying — inside the daily noise of most large caps, so it exits winners
    early. Run ``optionsagent sweep`` before choosing.
    """

    trailing_giveback_pct: float = 0.40
    """Once armed, exit after surrendering 40% of the peak gain. At a +25% peak
    that means exiting at +15%, so the floor always stays above the +10% target."""

    trailing_floor_pct: float = 0.10
    """Minimum trigger level; gaps, fees and execution can realize less."""

    stop_loss_pct: float = -0.50
    """-50% on premium, checked on every loop regardless of days remaining."""

    expiry_guard_dte: int = 3
    """At 3 days to expiry, close everything. Gamma explodes and the spread
    widens, so an unattended agent should simply not be in the position."""

    expiry_guard_loss_pct: float = -0.30
    """Tighter stop inside the guard window: down 30% with 3 days left is not
    coming back often enough to justify holding."""

    max_hold_days: int = 14
    """The brief's two-week window, enforced as a time stop."""

    theta_bleed_exit: bool = True
    theta_bleed_pct_per_day: float = 0.04
    """Bail out if decay accelerates past 4%/day while the position is flat or
    down; at that rate the thesis needs to be right almost immediately."""

    exit_before_earnings: bool = True
    earnings_exit_days: int = 1


class SizingConfig(StrictConfig):
    max_trade_premium: float = Field(default=1000.0, gt=0)
    risk_per_trade_pct: float = 0.02
    """Fraction of equity put at risk per trade. Because the stop is -50%, the
    position size is 2x this, i.e. 4% of equity of premium per position."""

    max_positions: int = 6
    max_positions_per_symbol: int = 1
    max_portfolio_premium_pct: float = 0.25
    """Never hold more than 25% of equity as long option premium."""

    min_contracts: int = 1
    max_contracts: int = 20
    cash_reserve_pct: float = 0.20


class RiskConfig(StrictConfig):
    daily_loss_limit_pct: float = 0.05
    """Halt new entries for the rest of the session after a 5% equity drawdown."""

    max_drawdown_halt_pct: float = 0.20
    max_consecutive_losses: int = 5
    pdt_protection: bool = True
    pdt_equity_threshold: float = 25_000.0
    pdt_max_day_trades: int = 3
    """Under $25k, FINRA allows 3 day trades per rolling 5 business days. A +10%
    target can easily be hit the same session, so the agent reserves day trades
    for stop-losses rather than spending them on profit-taking."""

    kill_switch_file: str = "state/KILL"


class ExecutionConfig(StrictConfig):
    order_type: Literal["limit"] = "limit"
    """Market orders on options are how accounts get filled at the ask on a wide
    spread. The agent only ever sends limit orders."""

    entry_limit_offset: float = 0.25
    """Fraction of the half-spread to concede on entry: 0.0 bids at mid, 1.0 pays
    the ask. 0.25 sits just through the mid, which usually fills on a liquid
    name without donating the whole spread."""

    exit_limit_offset: float = 0.25
    """Same, for exits: 0.0 offers at mid, 1.0 hits the bid."""

    urgent_cross_spread: bool = True
    """For urgent exits, price at the bid. Execution is never guaranteed."""

    reprice_attempts: int = 3
    reprice_interval_seconds: int = 20
    max_slippage_pct: float = 0.05
    poll_interval_seconds: int = 60
    max_quote_age_seconds: int = Field(default=120, gt=0)
    market_open: str = "09:30"
    market_close: str = "16:00"
    entry_window_start: str = "09:45"
    """No entries in the first 15 minutes; opening spreads are wide and quotes
    are unreliable."""

    entry_window_end: str = "15:30"
    timezone: str = "America/New_York"


class MarketConfig(StrictConfig):
    risk_free_rate: float = 0.042
    dividend_yield: float = 0.0


class BrokerConfig(StrictConfig):
    kind: Literal["paper", "robinhood_mcp", "robinhood"] = "paper"
    """``robinhood_mcp`` is the official Trading MCP and the one to use. ``robinhood``
    is the unofficial ``robin_stocks`` path, kept only for reference."""

    starting_equity: float = 25_000.0
    """Paper broker only."""

    dry_run: bool = True
    """When true against a live broker, orders are still reviewed with the broker's
    own pre-trade simulation but never submitted."""

    require_confirmation: bool = True


class Mode(StrEnum):
    """Operating modes, in the order you should progress through them."""

    OFF = "off"
    SCAN_ONLY = "scan_only"
    """Screen and log candidates. Opens nothing, not even on paper."""

    PAPER = "paper"
    LIVE_APPROVAL = "live_approval"
    """Prepare and review real orders, but require a human yes on each one."""

    LIVE_AUTO = "live_auto"


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPTIONSAGENT_", env_nested_delimiter="__", extra="forbid", allow_inf_nan=False
    )

    data_provider: Literal["synthetic", "robinhood_mcp"] = "synthetic"
    reference_data_file: str | None = None
    reference_auto_refresh: bool = False
    paper_iv_history_experiment: bool = False
    mode: Mode = Mode.PAPER
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    exit: ExitConfig = Field(default_factory=ExitConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    state_dir: str = "state"

    @model_validator(mode="after")
    def validate_safety(self) -> Config:
        if self.paper_iv_history_experiment and (
            self.mode != Mode.PAPER
            or self.broker.kind != "paper"
            or self.data_provider != "robinhood_mcp"
            or not self.entry.require_iv_rank
        ):
            raise ValueError(
                "Research IV experiment requires Robinhood data, paper broker, "
                "paper mode, and historical IV filtering enabled"
            )
        if not self.entry.require_iv_rank and (
            self.mode != Mode.PAPER or self.broker.kind != "paper"
        ):
            raise ValueError("Disabling historical IV rank is permitted only for paper testing")
        for group in (
            self.entry,
            self.exit,
            self.sizing,
            self.risk,
            self.execution,
            self.market,
            self.broker,
        ):
            for name, value in group.model_dump().items():
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError(f"{name} must be finite")
        for value in (
            self.sizing.risk_per_trade_pct,
            self.sizing.max_portfolio_premium_pct,
            self.risk.daily_loss_limit_pct,
            self.risk.max_drawdown_halt_pct,
        ):
            if not 0 < value <= 1:
                raise ValueError("Risk and exposure fractions must be in (0, 1]")
        if not 0 <= self.sizing.cash_reserve_pct < 1:
            raise ValueError("cash_reserve_pct must be in [0, 1)")
        if not 1 <= self.sizing.min_contracts <= self.sizing.max_contracts:
            raise ValueError("Invalid contract quantity bounds")
        if not 1 <= self.sizing.max_positions_per_symbol <= self.sizing.max_positions:
            raise ValueError("Invalid position limits")
        if (
            min(
                self.execution.poll_interval_seconds,
                self.execution.reprice_attempts,
                self.exit.max_hold_days,
                self.risk.max_consecutive_losses,
            )
            <= 0
        ):
            raise ValueError("Polling, retry, holding and loss-count limits must be positive")
        if self.entry.min_dte < 1 or self.exit.expiry_guard_dte < 0:
            raise ValueError("Invalid DTE bounds")
        if not 0 < self.entry.min_premium <= self.entry.max_premium:
            raise ValueError("Invalid premium bounds")
        if (
            min(
                self.entry.min_open_interest,
                self.entry.min_volume,
                self.entry.avoid_earnings_within_days,
                self.exit.earnings_exit_days,
            )
            < 0
        ):
            raise ValueError("Liquidity and event windows cannot be negative")
        if not -1 <= self.exit.expiry_guard_loss_pct < 0 or self.exit.take_profit_pct <= 0:
            raise ValueError("Invalid exit thresholds")
        if not 0 <= self.exit.trailing_floor_pct <= self.exit.take_profit_pct:
            raise ValueError("Trailing floor cannot exceed activation threshold")
        for value in (
            self.execution.entry_limit_offset,
            self.execution.exit_limit_offset,
            self.execution.max_slippage_pct,
            self.entry.max_iv_rank,
        ):
            if not 0 <= value <= 1:
                raise ValueError("Execution and IV fractions must be in [0, 1]")
        if self.broker.starting_equity <= 0:
            raise ValueError("Starting equity must be positive")
        ZoneInfo(self.execution.timezone)
        times = (
            self.execution.market_open,
            self.execution.entry_window_start,
            self.execution.entry_window_end,
            self.execution.market_close,
        )
        if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t) for t in times):
            raise ValueError("Session times must use HH:MM")
        if not times[0] <= times[1] < times[2] <= times[3]:
            raise ValueError("Entry window must be within market hours")
        if self.state_dir != "state" and self.risk.kill_switch_file == "state/KILL":
            self.risk.kill_switch_file = str(Path(self.state_dir) / "KILL")
        if self.entry.min_dte > self.entry.max_dte:
            raise ValueError("min_dte must not exceed max_dte")
        if not 0 <= self.entry.min_abs_delta <= self.entry.max_abs_delta <= 1:
            raise ValueError("delta bounds must be between zero and one")
        if not -1 <= self.exit.stop_loss_pct < 0:
            raise ValueError("stop_loss_pct must be negative and at least -1")
        if not 0 <= self.exit.trailing_giveback_pct <= 1:
            raise ValueError("trailing giveback must be between zero and one")
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        if path is None:
            return cls()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config file not found: {p}")
        data: dict[str, Any] = yaml.safe_load(p.read_text()) or {}
        return cls(**data)

    def dump(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False))
