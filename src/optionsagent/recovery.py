"""Reviewed paper-account repair. Never guesses execution or edits a real account."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from .runtime import RuntimeStore, single_writer


def repair(state_dir, evidence_path, apply=False):
    evidence = json.loads(Path(evidence_path).read_text())
    if not evidence.get("source") or not evidence.get("reason"):
        raise ValueError("Evidence source and review reason required")
    with single_writer(state_dir):
        store = RuntimeStore(state_dir)
        data = store.read()
        if not data or data["config"]["broker"]["kind"] != "paper":
            raise ValueError("Paper checkpoint required")
        from dataclasses import asdict

        from .orders import OrderRegistry

        journal = OrderRegistry(Path(state_dir))
        for oid, record in journal.orders.items():
            checkpoint_record = data["orders"].get(oid)
            if checkpoint_record is None or record.updated_at > checkpoint_record["updated_at"]:
                raise ValueError("Order journal is newer than checkpoint; restore runner first")
        kind = evidence["kind"]
        action = {"kind": kind, "source": evidence["source"], "reason": evidence["reason"]}
        if kind == "resolve_order":
            oid = evidence["order_id"]
            record = data["orders"].get(oid)
            if not record or record["state"] != "pending":
                raise ValueError("Pending checkpoint order required")
            if evidence.get("resolution") != "verified_no_execution":
                raise ValueError("Only verified no-execution repairs are supported")
            if evidence.get("executed_quantity") != 0 or evidence.get("working") is not False:
                raise ValueError("Explicit zero executions and no working order required")
            record.update(
                state="failed",
                detail="Reviewed recovery: " + evidence["reason"],
                updated_at=datetime.now(UTC).isoformat(),
            )
            action["order_id"] = oid
        elif kind == "settlement":
            occ = evidence["contract"]
            price = float(evidence["settlement_per_share"])
            import math

            if not math.isfinite(price) or price < 0:
                raise ValueError("Invalid settlement price")
            ledger = next(
                (p for p in data["positions"] if p["contract"]["occ_symbol"] == occ), None
            )
            broker = next(
                (p for p in data["paper"]["positions"] if p["contract"]["occ_symbol"] == occ), None
            )
            if not ledger or not broker or ledger["quantity"] != broker["quantity"]:
                raise ValueError("Matched ledger/broker holding required")
            if date.fromisoformat(ledger["contract"]["expiry"]) >= datetime.now(UTC).date():
                raise ValueError("Contract has not expired")
            if evidence.get("expiry") != ledger["contract"]["expiry"]:
                raise ValueError("Settlement expiry mismatch")
            # Use the existing accounting code to allocate costs consistently.
            from .models import ExitReason
            from .portfolio import Portfolio
            from .runtime import position

            pf = Portfolio(Path(state_dir), persist=False)
            pf.add(position(ledger))
            trade = pf.close(occ, price, ExitReason.EXPIRED)
            from dataclasses import asdict

            data["trades"].append(asdict(trade))
            data["realized_pnl"] += trade.pnl
            data["paper"]["cash"] += price * 100 * ledger["quantity"]
            data["positions"] = [p for p in data["positions"] if p is not ledger]
            data["paper"]["positions"] = [p for p in data["paper"]["positions"] if p is not broker]
            data["paper"]["marks"].pop(occ, None)
            data["risk"]["consecutive_losses"] = (
                data["risk"]["consecutive_losses"] + 1 if trade.pnl < 0 else 0
            )
            action.update(contract=occ, pnl=trade.pnl)
        elif kind != "clear_reconcile":
            raise ValueError("Unknown repair kind")
        actual = {p["contract"]["occ_symbol"]: p["quantity"] for p in data["paper"]["positions"]}
        expected = {p["contract"]["occ_symbol"]: p["quantity"] for p in data["positions"]}
        if kind == "clear_reconcile":
            if actual != expected or any(o["state"] == "pending" for o in data["orders"].values()):
                raise ValueError("Mismatch or pending order still unresolved")
            data["reconcile_halt"] = ""
        if apply:
            from .orders import OrderRecord, OrderRegistry
            from .runtime import encode

            store.event("recovery_before", {"checkpoint": store.read(), "review": action})
            with store.connect() as db:
                db.execute(
                    "UPDATE checkpoint SET body=? WHERE id=1", (json.dumps(data, default=encode),)
                )
            registry = OrderRegistry(Path(state_dir))
            registry.orders = {k: OrderRecord(**r) for k, r in data["orders"].items()}
            registry.save()
            store.event("recovery_applied", action)
        return {"applied": apply, "action": action}
