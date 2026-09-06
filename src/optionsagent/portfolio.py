"""Position ledger and durable state.

The agent's own view of its positions is authoritative for strategy metadata
(entry Greeks, peak return, whether the trailing stop is armed) because no
broker stores any of that. The broker remains authoritative for what is
actually held, so :meth:`Portfolio.reconcile` drops anything the broker no
longer reports, which is what keeps a manual close in the app from leaving a
ghost position that the agent keeps trying to sell.

State is written to JSON after every mutation so a crash or restart mid-session
resumes with trailing stops intact rather than resetting every high-water mark
to zero.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .models import ExitReason, OptionContract, Position, TradeRecord, utcnow

log = logging.getLogger(__name__)


@dataclass
class Portfolio:
    state_dir: Path = Path("state")
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)
    realized_pnl: float = 0.0

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def state_file(self) -> Path:
        return self.state_dir / "positions.json"

    @property
    def trade_log(self) -> Path:
        return self.state_dir / "trades.jsonl"

    # ---- ledger ----------------------------------------------------------

    def add(self, position: Position) -> None:
        self.positions[position.contract.occ_symbol] = position
        self.save()

    def get(self, occ_symbol: str) -> Position | None:
        return self.positions.get(occ_symbol)

    def symbols_held(self) -> set[str]:
        return {p.contract.symbol for p in self.positions.values()}

    def count_for_symbol(self, symbol: str) -> int:
        return sum(1 for p in self.positions.values() if p.contract.symbol == symbol)

    def open_premium(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    def close(
        self,
        occ_symbol: str,
        exit_price: float,
        reason: ExitReason,
        fees: float = 0.0,
        quantity: int | None = None,
        at: datetime | None = None,
    ) -> TradeRecord | None:
        pos = self.positions.get(occ_symbol)
        if pos is None:
            return None
        qty = pos.quantity if quantity is None else quantity
        if not 0 < qty <= pos.quantity:
            raise ValueError("Fill quantity is outside the held quantity")
        entry_fees = pos.entry_fees * qty / pos.quantity
        total_fees = entry_fees + fees
        pnl = (exit_price - pos.entry_price) * 100 * qty - total_fees
        record = TradeRecord(
            contract=str(pos.contract),
            quantity=qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            opened_at=pos.opened_at,
            closed_at=at or utcnow(),
            reason=reason,
            pnl=pnl,
            return_pct=pnl / (pos.entry_price * 100 * qty),
            fees=total_fees,
        )
        pos.quantity -= qty
        pos.entry_fees -= entry_fees
        if pos.quantity == 0:
            self.positions.pop(occ_symbol)
        self.trades.append(record)
        self.realized_pnl += pnl
        self._append_trade(record)
        self.save()
        return record

    def find_orphans(self, broker_positions: list[Position]) -> list[Position]:
        """Ledger entries the broker no longer reports.

        These are not dropped here. The caller books them as closed trades so
        that a manual close in the brokerage app still lands in the trade log;
        silently deleting them would quietly inflate the recorded win rate by
        making losers disappear.
        """
        broker_keys = {p.contract.occ_symbol for p in broker_positions}
        return [p for key, p in self.positions.items() if key not in broker_keys]

    # ---- stats -----------------------------------------------------------

    def summary(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {
            "open_positions": len(self.positions),
            "open_premium": round(self.open_premium(), 2),
            "closed_trades": len(self.trades),
            "realized_pnl": round(self.realized_pnl, 2),
            "win_rate": round(len(wins) / len(self.trades), 4) if self.trades else 0.0,
            "avg_win_pct": (round(sum(t.return_pct for t in wins) / len(wins), 4) if wins else 0.0),
            "avg_loss_pct": (
                round(sum(t.return_pct for t in losses) / len(losses), 4) if losses else 0.0
            ),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "exit_reasons": self.exit_reason_counts(),
        }

    def exit_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.trades:
            counts[t.reason.value] = counts.get(t.reason.value, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    # ---- persistence -----------------------------------------------------

    def _append_trade(self, record: TradeRecord) -> None:
        with self.trade_log.open("a") as fh:
            fh.write(json.dumps(record.to_dict()) + "\n")

    def save(self) -> None:
        payload = {
            "saved_at": utcnow().isoformat(),
            "realized_pnl": self.realized_pnl,
            "positions": [
                {
                    "symbol": p.contract.symbol,
                    "expiry": p.contract.expiry.isoformat(),
                    "strike": p.contract.strike,
                    "right": p.contract.right,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "opened_at": p.opened_at.isoformat(),
                    "entry_underlying": p.entry_underlying,
                    "entry_iv": p.entry_iv,
                    "entry_delta": p.entry_delta,
                    "peak_return": p.peak_return,
                    "trailing_armed": p.trailing_armed,
                    "last_mark": p.last_mark,
                    "entry_fees": p.entry_fees,
                    "notes": p.notes,
                    "broker_order_id": p.broker_order_id,
                }
                for p in self.positions.values()
            ],
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.state_file)

    @classmethod
    def load(cls, state_dir: str | Path = "state") -> Portfolio:
        pf = cls(state_dir=Path(state_dir))
        if not pf.state_file.exists():
            return pf
        data = json.loads(pf.state_file.read_text())
        pf.realized_pnl = float(data.get("realized_pnl", 0.0))
        for row in data.get("positions", []):
            contract = OptionContract(
                symbol=row["symbol"],
                expiry=date.fromisoformat(row["expiry"]),
                strike=float(row["strike"]),
                right=row["right"],
            )
            pf.positions[contract.occ_symbol] = Position(
                contract=contract,
                quantity=int(row["quantity"]),
                entry_price=float(row["entry_price"]),
                opened_at=datetime.fromisoformat(row["opened_at"]),
                entry_underlying=float(row.get("entry_underlying", 0.0)),
                entry_iv=float(row.get("entry_iv", 0.0)),
                entry_delta=float(row.get("entry_delta", 0.0)),
                peak_return=float(row.get("peak_return", 0.0)),
                trailing_armed=bool(row.get("trailing_armed", False)),
                last_mark=float(row.get("last_mark", 0.0)),
                entry_fees=float(row.get("entry_fees", 0.0)),
                notes=row.get("notes", ""),
                broker_order_id=row.get("broker_order_id", ""),
            )
        if pf.trade_log.exists():
            for line in pf.trade_log.read_text().splitlines():
                row = json.loads(line)
                row["opened_at"] = datetime.fromisoformat(row["opened_at"])
                row["closed_at"] = datetime.fromisoformat(row["closed_at"])
                row["reason"] = ExitReason(row["reason"])
                pf.trades.append(TradeRecord(**row))
        log.info("restored %d position(s) from %s", len(pf.positions), pf.state_file)
        return pf

    def mark_to_market(self, marks: dict[str, float]) -> float:
        """Total dollar value of open positions given ``occ_symbol -> mid`` marks."""
        return sum(
            p.market_value(marks.get(key, p.entry_price)) for key, p in self.positions.items()
        )
