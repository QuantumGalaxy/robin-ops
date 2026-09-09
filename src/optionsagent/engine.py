"""The agent loop.

Each pass does the same four things, in this order and never a different one:

1. refresh account equity and register the trading session with the risk manager;
2. reconcile the local ledger against the broker's actual positions;
3. evaluate exits on everything held — exits run before entries, always, so a
   stall or an exception while scanning for new trades can never delay closing
   a losing position;
4. scan for new entries, if and only if risk limits allow it.

The loop is synchronous. The runtime checkpoint restores paper-account, risk,
order and strategy state; ambiguous crash-window orders require recovery.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .brokers.base import Broker
from .brokers.paper import PaperBroker
from .config import Config, Mode
from .greeks import intrinsic
from .marketdata.base import MarketDataProvider
from .models import ExitReason, OptionQuote, Position, Side, utcnow
from .orders import OrderRegistry, OrderState, client_order_id
from .portfolio import Portfolio
from .risk import RiskManager
from .runtime import RuntimeStore
from .strategy.entry import EntryScreener
from .strategy.exits import evaluate_exit
from .strategy.signals import MomentumSignal, PriceHistory
from .strategy.sizing import size_position

log = logging.getLogger(__name__)


@dataclass
class LoopReport:
    at: datetime
    equity: float
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    """Populated in scan-only mode: what the agent would have bought."""

    blocked_reason: str = ""
    reconcile_mismatch: str = ""
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = [f"equity ${self.equity:,.0f}"]
        if self.opened:
            bits.append(f"opened {len(self.opened)}")
        if self.closed:
            bits.append(f"closed {len(self.closed)}")
        bits.append(f"holding {len(self.held)}")
        if self.blocked_reason:
            bits.append(f"entries blocked: {self.blocked_reason}")
        return ", ".join(bits)


@dataclass
class TradingEngine:
    config: Config
    data: MarketDataProvider
    broker: Broker
    portfolio: Portfolio
    signal: MomentumSignal = field(default_factory=MomentumSignal)
    history: PriceHistory = field(default_factory=PriceHistory)
    risk: RiskManager | None = None
    screener: EntryScreener | None = None
    orders: OrderRegistry | None = None
    store: RuntimeStore | None = None
    reconcile_halt: str = ""
    max_candidates_considered: int = 12
    """How far down the ranked list to look for a contract that fits the budget."""

    halt_on_reconcile_mismatch: bool = True
    """Stop opening positions when the ledger and the broker disagree.

    A mismatch means the agent's picture of what it owns is wrong, and sizing,
    exposure caps, and exit logic are all computed from that picture. Continuing
    to open new positions on a known-bad view is how a small bug becomes a large
    one. Exits keep running regardless.
    """

    def __post_init__(self) -> None:
        Config.model_validate(self.config.model_dump())
        if self.store:
            self.portfolio.persist = False
        if self.config.mode in (Mode.LIVE_APPROVAL, Mode.LIVE_AUTO):
            raise RuntimeError(
                "Live trading disabled pending verified broker lifecycle integration"
            )
        if self.config.mode is Mode.PAPER and not isinstance(self.broker, PaperBroker):
            raise RuntimeError("PAPER mode requires PaperBroker")
        if self.risk is None:
            self.risk = RiskManager(self.config.risk)
        if self.orders is None:
            self.orders = OrderRegistry(state_dir=Path(self.config.state_dir))
        if self.screener is None:
            self.screener = EntryScreener(
                cfg=self.config.entry,
                target_return=self.config.exit.take_profit_pct,
                horizon_days=float(self.config.exit.max_hold_days),
            )

    def warmup(self, days: int = 90) -> None:
        """Seed the signal with historical closes so the first loop can trade."""
        for symbol in self.config.universe.symbols:
            try:
                self.history.replace(symbol, self.data.historical_closes(symbol, days))
            except Exception:
                log.exception("warmup unavailable for %s; entries will remain unqualified", symbol)
        log.info("warmed up price history for %d symbols", len(self.config.universe.symbols))

    # ---- main loop -------------------------------------------------------

    def run_once(self, as_of: datetime | None = None) -> LoopReport:
        now = as_of or utcnow()
        if self.config.mode in (Mode.LIVE_APPROVAL, Mode.LIVE_AUTO):
            raise RuntimeError(
                "Live trading disabled pending verified broker lifecycle integration"
            )
        if self.config.mode is Mode.PAPER and not isinstance(self.broker, PaperBroker):
            raise RuntimeError("PAPER mode requires PaperBroker")
        if self.config.mode is Mode.OFF:
            return LoopReport(at=now, equity=0.0, blocked_reason="off")
        equity = self.broker.equity()
        assert self.risk is not None
        self.risk.start_session(
            now.astimezone(ZoneInfo(self.config.execution.timezone)).date(), equity
        )
        report = LoopReport(at=now, equity=equity)
        if self.config.mode is Mode.SCAN_ONLY:
            self._refresh_history(now, report)
            self._scan_entries(now, report, dry=True)
            report.blocked_reason = "scan-only: no order or position mutations"
            self._checkpoint(report)
            return report

        # Broker truth and held-position exits do not depend on scanning the universe.
        self._reconcile(report)
        self._settle_expirations(now, report)
        self._manage_exits(now, report)
        self._refresh_history(now, report)
        equity = self.broker.equity()
        allowed, reason = self.risk.can_open(equity, self.broker.day_trades_used())
        if self.reconcile_halt:
            report.blocked_reason = self.reconcile_halt
        elif report.errors:
            report.blocked_reason = "data/monitoring failure: " + "; ".join(report.errors)
        elif self.orders.pending():
            report.blocked_reason = "unresolved order: reconciliation required"
        elif allowed:
            self._scan_entries(now, report)
        else:
            report.blocked_reason = reason
        report.held = [str(p.contract) for p in self.portfolio.positions.values()]
        report.equity = self.broker.equity()
        self._checkpoint(report)
        return report

    def _checkpoint(self, report=None):
        if self.store:
            self.store.save(self)
            from .health import Alerts

            alerts = Alerts(self.config.state_dir)
            if report:
                alerts.monitoring_result(report.errors)
                if self.reconcile_halt or self.orders.pending():
                    alerts.set(
                        "recovery",
                        self.reconcile_halt or "Unresolved order requires recovery",
                        "critical",
                    )
                else:
                    alerts.clear("recovery")
            if report:
                from dataclasses import asdict

                self.store.event("loop", asdict(report))

    def _usable(self, quote, now):
        return (
            quote is not None
            and quote.is_tradeable()
            and (
                self.config.data_provider == "synthetic"
                or quote.is_fresh(now, self.config.execution.max_quote_age_seconds)
            )
        )

    def run_forever(self, max_loops: int | None = None) -> None:
        loops = 0
        while max_loops is None or loops < max_loops:
            try:
                report = self.run_once()
                log.info("loop complete: %s", report.describe())
            except KeyboardInterrupt:
                log.info("interrupted; exiting without touching open positions")
                return
            except Exception:
                # Never die on a transient data or network error: the positions
                # still need managing on the next pass.
                log.exception("loop failed; retrying after the poll interval")
            loops += 1
            time.sleep(self.config.execution.poll_interval_seconds)

    # ---- steps -----------------------------------------------------------

    def _refresh_history(self, now: datetime, report: LoopReport | None = None) -> None:
        for symbol in self.config.universe.symbols:
            try:
                # Daily strategy: refresh completed daily bars, never append minute polls.
                if self.config.data_provider != "synthetic":
                    closes = self.data.historical_closes(symbol, 90)
                    self.history.replace(symbol, closes)
                    if not closes and report:
                        report.errors.append(f"{symbol}: completed daily bars unavailable")
                else:
                    price = self.data.underlying_price(symbol)
                    if price:
                        self.history.push_daily(symbol, price, now.date())
            except Exception:
                self.history.replace(symbol, [])
                if report:
                    report.errors.append(f"{symbol}: history refresh failed")
                log.exception("history refresh failed for %s", symbol)

    def _quote_for(self, position: Position) -> OptionQuote | None:
        c = position.contract
        return self.data.quote(c.symbol, c.expiry, c.strike, c.right)

    def _settle_expirations(self, now: datetime, report: LoopReport) -> None:
        """Book any contract that reached expiry without being closed.

        The expiry guard is supposed to make this unreachable, but a quote
        outage or a relaxed guard can let one through. Settling here — before
        reconciliation — is what keeps an expired contract from vanishing from
        the trade log when the broker stops reporting it.
        """
        for key, position in list(self.portfolio.positions.items()):
            if position.contract.days_to_expiry(now.date()) >= 0:
                continue
            if not isinstance(self.broker, PaperBroker) or self.config.data_provider != "synthetic":
                self.reconcile_halt = (
                    "expired position needs a verified expiration settlement; "
                    "current spot is not settlement"
                )
                continue
            actual = {p.contract.occ_symbol: p for p in self.broker.positions()}.get(key)
            if actual is None or actual.quantity != position.quantity:
                continue
            try:
                spot = self.data.underlying_price(position.contract.symbol)
            except Exception:
                spot = None
            if spot is None or not math.isfinite(spot) or spot <= 0:
                self.reconcile_halt = "expired position has no valid settlement reference"
                continue
            value = intrinsic(spot, position.contract.strike, position.contract.right)
            self.broker.settle_expiration(position, value)
            record = self.portfolio.close(key, value, ExitReason.EXPIRED, at=now)
            if record:
                assert self.risk is not None
                self.risk.record_trade_result(record.pnl)
                self._checkpoint()
                log.info(
                    "settled expired %s at intrinsic $%.2f | P&L $%+.2f",
                    position.contract,
                    value,
                    record.pnl,
                )
                report.closed.append(
                    f"{position.contract} (expired) ${record.pnl:+,.0f} / {record.return_pct:+.1%}"
                )

    def _reconcile(self, report: LoopReport) -> None:
        """Book positions that left the broker without the agent selling them.

        The broker is the source of record. Where the two disagree the ledger is
        corrected, and — because a disagreement means the agent's view was wrong
        about something — new entries are held for the rest of the loop.
        """
        actual = {p.contract.occ_symbol: p for p in self.broker.positions()}
        expected = self.portfolio.positions
        mismatches = []
        self._unverified_positions = set()
        for key in actual.keys() | expected.keys():
            if key not in expected:
                mismatches.append(f"broker-only holding {key}")
            elif key not in actual:
                mismatches.append(f"ledger-only holding {key}")
            elif expected[key].quantity != actual[key].quantity:
                mismatches.append(f"quantity mismatch {key}")
        self._unverified_positions = {
            key
            for key in expected
            if key not in actual or expected[key].quantity != actual[key].quantity
        }
        if mismatches:
            self.reconcile_halt = "; ".join(mismatches)
            report.reconcile_mismatch = self.reconcile_halt
            if self.store:
                self.store.event("reconciliation_required", {"reason": self.reconcile_halt})
        # Never fabricate fills or clear a halt automatically. Verified repair is required.

    def _manage_exits(self, now: datetime, report: LoopReport) -> None:
        if self.config.data_provider != "synthetic" and not self.data.is_market_open():
            if self.store:
                self.store.event("monitor_wait", {"reason": "market session unverified or closed"})
            return
        for key, position in list(self.portfolio.positions.items()):
            if (
                key in getattr(self, "_unverified_positions", set())
                or position.contract.days_to_expiry(now.date()) < 0
            ):
                continue
            try:
                quote = self._quote_for(position)
            except Exception:
                report.errors.append(f"{position.contract}: held quote refresh failed")
                log.exception("quote refresh failed for held %s", position.contract)
                continue
            if (
                not self._usable(quote, now)
                or quote.contract.occ_symbol != position.contract.occ_symbol
            ):
                report.errors.append(f"{position.contract}: held quote missing or stale")
                log.warning("no usable quote for %s; will retry next loop", position.contract)
                continue
            if hasattr(self.broker, "mark"):
                self.broker.mark(quote)  # type: ignore[attr-defined]

            try:
                earnings = self.data.next_earnings_date(position.contract.symbol)
                symbol = position.contract.symbol
                if self.config.data_provider != "synthetic":
                    self.history.replace(symbol, self.data.historical_closes(symbol, 90))
                current = PriceHistory()
                current.replace(symbol, self.history.get(symbol))
                current.push(symbol, quote.underlying_price)
                direction, _ = self.signal.direction(symbol, current)
            except Exception:
                earnings, direction = None, None
                report.errors.append(f"{position.contract}: signal/event refresh failed")
            decision = evaluate_exit(
                position,
                quote,
                self.config.exit,
                as_of=now,
                earnings_date=earnings,
                direction=direction,
            )
            self.portfolio.save()
            if self.store:
                self.store.event(
                    "exit_decision",
                    {
                        "contract": str(position.contract),
                        "mark": position.last_mark,
                        "quote_at": quote.as_of,
                        "reason": decision.detail,
                        "exit": decision.should_exit,
                    },
                )
            if not decision.should_exit:
                continue

            urgent = decision.urgency == "urgent"
            try:
                fill = self._sell_with_reprice(position, quote, urgent)
            except Exception:
                report.errors.append(f"{position.contract}: exit submission uncertain")
                log.exception("exit submission uncertain for %s", position.contract)
                continue
            if fill is None:
                report.errors.append(
                    f"{position.contract}: exit did not fill; monitoring continues"
                )
                log.warning(
                    "exit order for %s did not fill (%s); retrying next loop",
                    position.contract,
                    decision.reason.value if decision.reason else "?",
                )
                continue

            record = self.portfolio.close(
                key,
                fill.price,
                decision.reason or ExitReason.MANUAL,
                fees=fill.fees,
                quantity=fill.quantity,
                at=now,
            )
            if record:
                assert self.risk is not None
                self.risk.record_trade_result(record.pnl)
                self._checkpoint()
                log.info(
                    "closed %s: %s | P&L $%+.2f (%.1f%%)",
                    position.contract,
                    decision.detail,
                    record.pnl,
                    record.return_pct * 100,
                )
                report.closed.append(
                    f"{position.contract} ({record.reason.value}) "
                    f"${record.pnl:+,.0f} / {record.return_pct:+.1%}"
                )

    def _scan_entries(self, now: datetime, report: LoopReport, dry: bool = False) -> None:
        cfg = self.config
        if cfg.data_provider != "synthetic":
            local = now.astimezone(ZoneInfo(cfg.execution.timezone))
            clock = local.strftime("%H:%M")
            if (
                not self.data.is_market_open()
                or local.weekday() >= 5
                or not cfg.execution.entry_window_start <= clock < cfg.execution.entry_window_end
            ):
                report.blocked_reason = "outside verified entry session"
                return
        assert self.screener is not None
        if len(self.portfolio.positions) >= cfg.sizing.max_positions:
            report.blocked_reason = f"at max positions ({cfg.sizing.max_positions})"
            return

        for symbol in cfg.universe.symbols:
            if not dry:
                allowed, reason = self.risk.can_open(
                    self.broker.equity(), self.broker.day_trades_used()
                )
                if not allowed or self.orders.pending():
                    report.blocked_reason = reason if not allowed else "unresolved order"
                    break
            if len(self.portfolio.positions) >= cfg.sizing.max_positions:
                break
            if self.portfolio.count_for_symbol(symbol) >= 1:
                continue

            try:
                self._scan_symbol(symbol, now, report, dry, self.broker.equity())
            except Exception:
                report.errors.append(f"{symbol}: entry scan failed")
                report.blocked_reason = f"{symbol}: entry scan failed"
                if self.store:
                    self.store.event("scan_error", {"symbol": symbol})
                log.exception("entry scan failed for %s", symbol)
                continue

    def _scan_symbol(self, symbol, now, report, dry, equity):
        cfg = self.config
        direction, confidence = self.signal.direction(symbol, self.history)
        if direction == "none":
            report.skipped.append(f"{symbol}: neutral or insufficient direction history")
            return

        if cfg.entry.require_market_alignment:
            market_direction, _ = self.signal.direction("SPY", self.history)
            if market_direction != direction:
                report.skipped.append(f"{symbol}: SPY direction does not confirm {direction}")
                return

        if cfg.paper_iv_history_experiment:
            from .iv_daily import select_rank

            # Config validation restricts this alternate source to the paper broker.
            details = select_rank(
                cfg.iv_history_state_dir or cfg.state_dir,
                symbol,
                now,
                auto=cfg.paper_iv_auto_switch,
            )
            iv_rank = details["rank"]
            eligible = iv_rank is not None and iv_rank <= cfg.entry.max_iv_rank
            reason = (
                details["reason"]
                if iv_rank is None
                else (
                    f"experimental IV rank {iv_rank:.1%} "
                    f"{'passes' if eligible else 'exceeds'} {cfg.entry.max_iv_rank:.0%} limit"
                )
            )
            if self.store:
                self.store.event(
                    "paper_iv_decision",
                    {
                        **details,
                        "symbol": symbol,
                        "eligible": eligible,
                        "threshold": cfg.entry.max_iv_rank,
                        "message": f"{symbol}: {reason}",
                    },
                )
            if not eligible:
                report.skipped.append(f"{symbol}: {reason}")
                return
        else:
            iv_rank = self.data.iv_rank(symbol)

        quotes = self.data.option_chain(symbol, cfg.entry.min_dte, cfg.entry.max_dte)
        if not quotes:
            report.skipped.append(f"{symbol}: no option quotes in the DTE range")
            return

        quotes = [q for q in quotes if q.contract.symbol == symbol and self._usable(q, now)]
        if cfg.entry.require_known_events and not self.data.earnings_known(symbol):
            report.skipped.append(f"{symbol}: earnings calendar unavailable")
            return
        earnings = self.data.next_earnings_date(symbol)
        days_to_earnings = (earnings - now.date()).days if earnings else None
        candidates = self.screener.screen(
            quotes,
            direction,
            iv_rank=iv_rank,
            days_to_earnings=days_to_earnings,
            confidence=confidence,
            as_of=now.date(),
        )
        if not candidates:
            report.skipped.append(
                f"{symbol}: no contract passed screening; "
                + "; ".join(
                    f"{reason} ({count})" for reason, count in self.screener.rejections.items()
                )
            )
            return

        # Walk down the ranked list rather than skipping the symbol when the
        # top pick is unaffordable. On a small account the best-scoring
        # contract on an expensive underlying routinely costs more than the
        # per-trade risk budget allows, and the second choice is usually
        # only marginally worse.
        best = None
        sizing = None
        for cand in candidates[: self.max_candidates_considered]:
            trial = size_position(
                cand.quote,
                equity,
                self.broker.buying_power(),
                self.portfolio.open_premium(),
                cfg.sizing,
                stop_loss_pct=cfg.exit.stop_loss_pct,
            )
            if trial.contracts > 0:
                best, sizing = cand, trial
                break
        if best is None or sizing is None:
            report.skipped.append(
                f"{symbol}: no candidate fits the per-trade budget "
                f"(cheapest ranked contract ${candidates[-1].quote.ask * 100:,.0f})"
            )
            return

        if dry:
            report.candidates.append(
                f"{best.contract} x{sizing.contracts} @ ~${best.quote.mid:.2f} | "
                + "; ".join(best.reasons)
            )
            return

        if self.store:
            self.store.event(
                "entry_decision",
                {
                    "contract": str(best.contract),
                    "quote_at": best.quote.as_of,
                    "bid": best.quote.bid,
                    "ask": best.quote.ask,
                    "quantity": sizing.contracts,
                    "reasons": best.reasons,
                    "direction": direction,
                    "confidence": confidence,
                    "iv_rank": iv_rank,
                    "greeks": vars(best.quote.greeks) if best.quote.greeks else None,
                },
            )
        fill = self._buy_with_reprice(best.quote, sizing.contracts, now)
        if fill is None:
            report.skipped.append(
                f"{symbol}: entry did not fill within the slippage budget "
                f"(bid ${best.quote.bid:.2f} / ask ${best.quote.ask:.2f})"
            )
            return

        g = best.quote.greeks
        position = Position(
            contract=best.contract,
            quantity=fill.quantity,
            entry_price=fill.price,
            opened_at=now,
            entry_underlying=best.quote.underlying_price,
            entry_iv=g.iv if g else 0.0,
            entry_delta=g.delta if g else 0.0,
            last_mark=fill.price,
            entry_fees=fill.fees,
            broker_order_id=fill.order_id,
            notes="; ".join(best.reasons),
        )
        self.portfolio.add(position)
        self._checkpoint()
        log.info(
            "opened %s x%d @ $%.2f | %s",
            best.contract,
            sizing.contracts,
            fill.price,
            "; ".join(best.reasons),
        )
        report.opened.append(f"{best.contract} x{sizing.contracts} @ ${fill.price:.2f}")

    # ---- pricing helpers -------------------------------------------------

    def _entry_limit(self, quote: OptionQuote, concession: float | None = None) -> float:
        """Bid through the mid by ``concession`` of the half-spread, never past the ask.

        Paying the full ask on entry and hitting the full bid on exit hands the
        market maker the entire spread, which on a 6%-wide contract is more than
        half the +10% target before the stock has moved at all.
        """
        if concession is None:
            concession = self.config.execution.entry_limit_offset
        cap = quote.mid * (1 + self.config.execution.max_slippage_pct)
        price = min(quote.ask, cap, quote.mid + (quote.spread / 2.0) * concession)
        return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))

    def _exit_limit(
        self, quote: OptionQuote, urgent: bool, concession: float | None = None
    ) -> float:
        if urgent and self.config.execution.urgent_cross_spread:
            return round(quote.bid, 2)
        if concession is None:
            concession = self.config.execution.exit_limit_offset
        floor = quote.mid * (1 - self.config.execution.max_slippage_pct)
        price = max(quote.bid, floor, quote.mid - (quote.spread / 2.0) * concession)
        return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))

    def _concession_ladder(self, start: float) -> list[float]:
        """Limit prices to try, walking from ``start`` toward the far touch.

        A resting order at the mid often will not fill on a quiet contract. Each
        rung concedes a bit more of the spread, and the ladder stops before the
        far touch unless the slippage budget allows going all the way, so the
        agent never blindly pays the ask.
        """
        attempts = max(1, self.config.execution.reprice_attempts)
        ceiling = 1.0
        if attempts == 1:
            return [start]
        step = (ceiling - start) / (attempts - 1)
        return [round(start + step * i, 4) for i in range(attempts)]

    def _buy_with_reprice(self, quote: OptionQuote, quantity: int, now: datetime | None = None):
        """Work an entry order up the spread until it fills or the budget runs out.

        The whole ladder is **one** trading intent, reserved once against the
        order registry. Each rung is a cancel-and-replace at a slightly worse
        price, not a new decision, so repricing must not trip the duplicate
        guard. A real adapter owns the place/wait/cancel cycle behind each call;
        here it is one synchronous attempt per rung.

        The intent is written to disk *before* the first submission. If the
        process dies between that write and the broker's response, the next run
        sees a pending order and reconciles instead of buying the contract twice.
        """
        assert self.orders is not None
        contract = quote.contract
        if self.orders.has_pending_for(contract.occ_symbol):
            log.info("an order is already working on %s; not adding another", contract)
            return None

        # Stamped with the loop's decision time, not the wall clock, so a
        # backtest stepping through simulated days does not collapse every order
        # into the same minute and suppress itself.
        order_id = client_order_id(contract, Side.BUY, quantity, now)
        if self.orders.is_duplicate(order_id):
            log.warning("suppressing duplicate buy for %s", contract)
            return None

        ladder = self._concession_ladder(self.config.execution.entry_limit_offset)
        if not self.broker.synchronous_fills:
            if self.broker.has_open_order(contract.occ_symbol) is not False:
                return None
            ladder = ladder[:1]
        self.orders.reserve(order_id, contract, Side.BUY, quantity, self._entry_limit(quote))
        try:
            for concession in ladder:
                limit = self._entry_limit(quote, concession)
                fill = self.broker.buy_to_open(quote, quantity, limit)
                if fill is not None:
                    state = OrderState.FILLED if fill.quantity == quantity else OrderState.PENDING
                    self.orders.mark(order_id, state, broker_order_id=fill.order_id)
                    return fill
        except Exception as exc:
            # Timeout may mean accepted: retain the reservation until proven terminal.
            self.orders.mark(order_id, OrderState.PENDING, detail=repr(exc))
            raise

        # Nothing filled. If the broker can tell us no order is working, release
        # the reservation so the next loop may try again. When it cannot, leave
        # the order pending until execution/cancellation is explicitly verified, because
        # assuming "not filled" on an ambiguous response is how you end up long
        # two contracts.
        self.orders.mark(
            order_id, OrderState.PENDING, broker_order_id=getattr(self.broker, "last_order_id", "")
        )
        if (
            self.broker.synchronous_fills
            and self.broker.has_open_order(contract.occ_symbol) is False
        ):
            self.orders.mark(order_id, OrderState.FAILED, detail="no rung filled")
        return None

    def _sell_with_reprice(self, position: Position, quote: OptionQuote, urgent: bool):
        if not self.broker.synchronous_fills:
            key = position.contract.occ_symbol
            if self.orders.has_pending_for(key) or self.broker.has_open_order(key) is not False:
                return None
            oid = client_order_id(position.contract, Side.SELL, position.quantity)
            self.orders.reserve(
                oid,
                position.contract,
                Side.SELL,
                position.quantity,
                self._exit_limit(quote, urgent),
            )
            fill = self.broker.sell_to_close(
                position, quote, self._exit_limit(quote, urgent), urgent
            )
            if fill:
                self.orders.mark(
                    oid,
                    OrderState.FILLED if fill.quantity == position.quantity else OrderState.PENDING,
                    broker_order_id=fill.order_id,
                )
            return fill
        if urgent:
            return self.broker.sell_to_close(
                position, quote, self._exit_limit(quote, True), urgent=True
            )
        for concession in self._concession_ladder(self.config.execution.exit_limit_offset):
            limit = self._exit_limit(quote, False, concession)
            fill = self.broker.sell_to_close(position, quote, limit, urgent=False)
            if fill is not None:
                return fill
        return None
