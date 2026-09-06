"""The agent loop.

Each pass does the same four things, in this order and never a different one:

1. refresh account equity and register the trading session with the risk manager;
2. reconcile the local ledger against the broker's actual positions;
3. evaluate exits on everything held — exits run before entries, always, so a
   stall or an exception while scanning for new trades can never delay closing
   a losing position;
4. scan for new entries, if and only if risk limits allow it.

The loop is synchronous and stateless between passes apart from the persisted
portfolio, which makes it safe to kill and restart at any point.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .brokers.base import Broker
from .config import Config, Mode
from .greeks import intrinsic
from .marketdata.base import MarketDataProvider
from .models import ExitReason, OptionQuote, Position, Side, utcnow
from .orders import OrderRegistry, OrderState, client_order_id
from .portfolio import Portfolio
from .risk import RiskManager
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
            closes = self.data.historical_closes(symbol, days)
            for price in closes:
                self.history.push(symbol, price)
        log.info("warmed up price history for %d symbols", len(self.config.universe.symbols))

    # ---- main loop -------------------------------------------------------

    def run_once(self, as_of: datetime | None = None) -> LoopReport:
        now = as_of or utcnow()
        equity = self.broker.equity()
        assert self.risk is not None
        self.risk.start_session(now.date(), equity)

        report = LoopReport(at=now, equity=equity)
        self._refresh_history()
        self._settle_expirations(now, report)
        self._reconcile(report)
        self._manage_exits(now, report)

        allowed, reason = self.risk.can_open(equity, self.broker.day_trades_used())
        if self.config.mode is Mode.SCAN_ONLY:
            self._scan_entries(now, report, dry=True)
            report.blocked_reason = "scan-only mode: candidates logged, nothing opened"
        elif report.reconcile_mismatch:
            report.blocked_reason = report.reconcile_mismatch
        elif allowed:
            self._scan_entries(now, report)
        else:
            report.blocked_reason = reason

        report.held = [str(p.contract) for p in self.portfolio.positions.values()]
        report.equity = self.broker.equity()
        return report

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

    def _refresh_history(self) -> None:
        for symbol in self.config.universe.symbols:
            price = self.data.underlying_price(symbol)
            if price:
                self.history.push(symbol, price)

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
            spot = self.data.underlying_price(position.contract.symbol) or 0.0
            value = intrinsic(spot, position.contract.strike, position.contract.right)
            self.broker.settle_expiration(position, value)
            record = self.portfolio.close(key, value, ExitReason.EXPIRED)
            if record:
                assert self.risk is not None
                self.risk.record_trade_result(record.pnl)
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
        orphans = self.portfolio.find_orphans(self.broker.positions())
        for position in orphans:
            mark = position.last_mark or position.entry_price
            log.warning(
                "%s is no longer held at the broker; booking it closed at the last mark $%.2f",
                position.contract,
                mark,
            )
            record = self.portfolio.close(position.contract.occ_symbol, mark, ExitReason.MANUAL)
            if record:
                assert self.risk is not None
                self.risk.record_trade_result(record.pnl)
                report.closed.append(f"{position.contract} (closed outside the agent)")

        if orphans and self.halt_on_reconcile_mismatch:
            report.reconcile_mismatch = (
                f"{len(orphans)} position(s) disagreed with the broker; "
                "holding new entries this loop"
            )

    def _manage_exits(self, now: datetime, report: LoopReport) -> None:
        for key, position in list(self.portfolio.positions.items()):
            quote = self._quote_for(position)
            if quote is None or not quote.is_tradeable():
                log.warning("no usable quote for %s; will retry next loop", position.contract)
                continue
            if hasattr(self.broker, "mark"):
                self.broker.mark(quote)  # type: ignore[attr-defined]

            decision = evaluate_exit(
                position,
                quote,
                self.config.exit,
                as_of=now,
                earnings_date=self.data.next_earnings_date(position.contract.symbol),
            )
            self.portfolio.save()
            if not decision.should_exit:
                continue

            urgent = decision.urgency == "urgent"
            fill = self._sell_with_reprice(position, quote, urgent)
            if fill is None:
                log.warning(
                    "exit order for %s did not fill (%s); retrying next loop",
                    position.contract,
                    decision.reason.value if decision.reason else "?",
                )
                continue

            record = self.portfolio.close(
                key, fill.price, decision.reason or ExitReason.MANUAL, fees=fill.fees
            )
            if record:
                assert self.risk is not None
                self.risk.record_trade_result(record.pnl)
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
        assert self.screener is not None
        if len(self.portfolio.positions) >= cfg.sizing.max_positions:
            report.blocked_reason = f"at max positions ({cfg.sizing.max_positions})"
            return

        equity = self.broker.equity()
        for symbol in cfg.universe.symbols:
            if len(self.portfolio.positions) >= cfg.sizing.max_positions:
                break
            if self.portfolio.count_for_symbol(symbol) >= cfg.sizing.max_positions_per_symbol:
                continue

            direction, confidence = self.signal.direction(symbol, self.history)
            if direction == "none":
                continue

            quotes = self.data.option_chain(symbol, cfg.entry.min_dte, cfg.entry.max_dte)
            if not quotes:
                continue

            earnings = self.data.next_earnings_date(symbol)
            days_to_earnings = (earnings - now.date()).days if earnings else None
            candidates = self.screener.screen(
                quotes,
                direction,
                iv_rank=self.data.iv_rank(symbol),
                days_to_earnings=days_to_earnings,
                confidence=confidence,
                as_of=now.date(),
            )
            if not candidates:
                report.skipped.append(f"{symbol}: no contract passed screening")
                continue

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
                continue

            if dry:
                report.candidates.append(
                    f"{best.contract} x{sizing.contracts} @ ~${best.quote.mid:.2f} | "
                    + "; ".join(best.reasons)
                )
                continue

            fill = self._buy_with_reprice(best.quote, sizing.contracts, now)
            if fill is None:
                report.skipped.append(
                    f"{symbol}: entry did not fill within the slippage budget "
                    f"(bid ${best.quote.bid:.2f} / ask ${best.quote.ask:.2f})"
                )
                continue

            g = best.quote.greeks
            position = Position(
                contract=best.contract,
                quantity=sizing.contracts,
                entry_price=fill.price,
                opened_at=now,
                entry_underlying=best.quote.underlying_price,
                entry_iv=g.iv if g else 0.0,
                entry_delta=g.delta if g else 0.0,
                last_mark=fill.price,
                broker_order_id=fill.order_id,
                notes="; ".join(best.reasons),
            )
            self.portfolio.add(position)
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
        return round(min(quote.ask, quote.mid + (quote.spread / 2.0) * concession), 2)

    def _exit_limit(
        self, quote: OptionQuote, urgent: bool, concession: float | None = None
    ) -> float:
        if urgent and self.config.execution.urgent_cross_spread:
            return round(quote.bid, 2)
        if concession is None:
            concession = self.config.execution.exit_limit_offset
        return round(max(quote.bid, quote.mid - (quote.spread / 2.0) * concession), 2)

    def _concession_ladder(self, start: float) -> list[float]:
        """Limit prices to try, walking from ``start`` toward the far touch.

        A resting order at the mid often will not fill on a quiet contract. Each
        rung concedes a bit more of the spread, and the ladder stops before the
        far touch unless the slippage budget allows going all the way, so the
        agent never blindly pays the ask.
        """
        attempts = max(1, self.config.execution.reprice_attempts)
        ceiling = min(1.0, start + self.config.execution.max_slippage_pct / 0.05)
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
        self.orders.reserve(order_id, contract, Side.BUY, quantity, self._entry_limit(quote))
        try:
            for concession in ladder:
                limit = self._entry_limit(quote, concession)
                fill = self.broker.buy_to_open(quote, quantity, limit)
                if fill is not None:
                    self.orders.mark(order_id, OrderState.FILLED, broker_order_id=fill.order_id)
                    return fill
        except Exception as exc:
            # A raised exception means nothing rests at the broker.
            self.orders.mark(order_id, OrderState.FAILED, detail=repr(exc))
            raise

        # Nothing filled. If the broker can tell us no order is working, release
        # the reservation so the next loop may try again. When it cannot, leave
        # the order pending and let the stale-pending TTL free it, because
        # assuming "not filled" on an ambiguous response is how you end up long
        # two contracts.
        if self.broker.has_open_order(contract.occ_symbol) is False:
            self.orders.mark(order_id, OrderState.FAILED, detail="no rung filled")
        return None

    def _sell_with_reprice(self, position: Position, quote: OptionQuote, urgent: bool):
        # Exits are never suppressed by the duplicate guard. Selling a contract
        # you no longer hold is rejected by the broker; failing to sell one you do
        # hold is the expensive mistake.
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
