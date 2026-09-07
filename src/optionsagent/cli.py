"""Command line interface."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .brokers import build_broker
from .config import Config, Mode
from .engine import TradingEngine
from .greeks import compute_greeks, years_to_expiry
from .marketdata import build_provider
from .portfolio import Portfolio
from .runtime import RuntimeStore, single_writer
from .simulate import breakeven_win_rate, kelly_fraction, run_simulation
from .strategy.entry import EntryScreener, move_in_sigmas, required_underlying_move
from .strategy.exits import trailing_stop_level

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Options trading agent: Greeks-aware entry screening, automated exits, hard risk limits.",
)
console = Console()

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to a YAML config file.")
]


def _load(config: Path | None) -> Config:
    cfg = Config.load(config) if config else Config()
    return cfg


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command()
def explain() -> None:
    """Explain the Greeks and how each one is used, in plain language."""
    console.print(
        Panel.fit(
            "[bold]The four numbers that decide whether a trade can work[/bold]",
            border_style="cyan",
        )
    )
    t = Table(show_header=True, header_style="bold cyan", expand=True)
    t.add_column("Greek", style="bold", no_wrap=True)
    t.add_column("What it measures")
    t.add_column("How this agent uses it")
    t.add_row(
        "Delta",
        "How much the option moves per $1 move in the stock. 0.60 delta means the "
        "option gains about 60 cents when the stock gains a dollar. It also "
        "approximates the chance of finishing in the money.",
        "Buys 0.55-0.70 delta. High delta means a small stock move produces the "
        "+10% gain; cheap 0.15-delta contracts need a huge move and usually "
        "expire worthless.",
    )
    t.add_row(
        "Gamma",
        "How fast delta itself changes. High gamma means your exposure swings "
        "violently, which is great when right and brutal when wrong.",
        "Caps dollar gamma per contract and force-closes at 3 days to expiry, "
        "where gamma spikes and a polling loop cannot react fast enough.",
    )
    t.add_row(
        "Theta",
        "Dollars the contract loses per day just from time passing. It "
        "accelerates as expiry approaches.",
        "Rejects contracts bleeding more than 2.5%/day, and exits early if decay "
        "outruns the thesis. The default profile buys 12-18 DTE; longer expirations "
        "remain a separate experiment.",
    )
    t.add_row(
        "Vega",
        "Dollars gained per one point of implied volatility. Buying an option is "
        "buying volatility, whether you meant to or not.",
        "Skips entries when IV rank is above 75% and closes before earnings, "
        "because the post-earnings IV collapse can lose money even when the "
        "direction was right.",
    )
    console.print(t)
    console.print(
        "\n[dim]There is no Greek called alpha. Alpha is a portfolio term for "
        "return above a benchmark; it is the result you are hoping for, not an "
        "input you can trade on.[/dim]"
    )


@app.command("math")
def rule_math(
    take_profit: float = typer.Option(0.10, help="Take-profit as a fraction of premium."),
    stop_loss: float = typer.Option(-0.50, help="Stop-loss as a fraction of premium."),
) -> None:
    """Show the arithmetic a fixed take-profit / stop-loss pair has to satisfy."""
    console.print(
        Panel.fit("[bold]Break-even arithmetic of the exit rules[/bold]", border_style="yellow")
    )
    t = Table(show_header=True, header_style="bold")
    t.add_column("Take profit")
    t.add_column("Stop loss")
    t.add_column("Win rate needed to break even", justify="right")
    t.add_column("Comment")

    pairs = [
        (take_profit, stop_loss),
        (0.10, -0.50),
        (0.10, -0.25),
        (0.20, -0.35),
        (0.25, -0.50),
        (0.50, -0.50),
        (1.00, -0.50),
    ]
    seen = set()
    for tp, sl in pairs:
        key = (round(tp, 4), round(sl, 4))
        if key in seen:
            continue
        seen.add(key)
        p = breakeven_win_rate(tp, sl)
        if p >= 0.80:
            comment = "[red]needs a near-perfect hit rate[/red]"
        elif p >= 0.65:
            comment = "[yellow]demanding but not absurd[/yellow]"
        else:
            comment = "[green]achievable with a modest edge[/green]"
        t.add_row(f"{tp:+.0%}", f"{sl:+.0%}", f"{p:.1%}", comment)
    console.print(t)
    console.print(
        "\nWith [bold]+10% / -50%[/bold] you need to win roughly "
        f"[bold red]{breakeven_win_rate(0.10, -0.50):.0%}[/bold red] of trades just to break even, "
        "before spreads and fees. That is the single biggest problem with the rule set as "
        "written, and it is why this agent arms a trailing stop at +10% instead of selling "
        "there, and tightens the stop as expiry approaches instead of waiting for -50%."
    )


@app.command()
def trail(
    take_profit: float = typer.Option(0.10),
    giveback: float = typer.Option(0.40),
    floor: float = typer.Option(0.10),
) -> None:
    """Show where the trailing stop sits for a range of peak gains."""
    from .config import ExitConfig

    cfg = ExitConfig(
        take_profit_pct=take_profit, trailing_giveback_pct=giveback, trailing_floor_pct=floor
    )
    t = Table(title="Trailing stop levels", header_style="bold cyan")
    t.add_column("Peak unrealised gain", justify="right")
    t.add_column("Exit if it falls back to", justify="right")
    t.add_column("Gain locked in", justify="right")
    for peak in (0.10, 0.15, 0.20, 0.30, 0.50, 0.75, 1.00, 2.00):
        level = trailing_stop_level(peak, cfg)
        t.add_row(f"{peak:+.0%}", f"{level:+.1%}", f"{level:+.1%}")
    console.print(t)
    console.print(
        f"\n[dim]The floor at {floor:+.0%} is what implements 'keep it as long as I am up "
        "more than 10%'. The giveback is what lets a winner run past the target instead of "
        "capping every good trade at +10%.[/dim]"
    )


@app.command()
def chain(
    symbol: str = typer.Argument(..., help="Underlying symbol, e.g. AAPL."),
    direction: str = typer.Option("call", help="call or put."),
    config: ConfigOpt = None,
    provider: str = typer.Option("paper", help="paper or robinhood."),
    limit: int = typer.Option(8, help="How many ranked candidates to show."),
) -> None:
    """Screen a live (or synthetic) option chain and show the ranked candidates."""
    cfg = _load(config)
    data = build_provider(provider, risk_free_rate=cfg.market.risk_free_rate)
    symbol = symbol.upper()
    spot = data.underlying_price(symbol)
    if not spot:
        console.print(f"[red]no price for {symbol}[/red]")
        raise typer.Exit(1)

    quotes = data.option_chain(symbol, cfg.entry.min_dte, cfg.entry.max_dte)
    screener = EntryScreener(
        cfg=cfg.entry,
        target_return=cfg.exit.take_profit_pct,
        horizon_days=float(cfg.exit.max_hold_days),
    )
    cands = screener.screen(quotes, direction, iv_rank=data.iv_rank(symbol), confidence=0.7)  # type: ignore[arg-type]

    console.print(
        f"\n[bold]{symbol}[/bold] at ${spot:,.2f} | {len(quotes)} contracts in the "
        f"{cfg.entry.min_dte}-{cfg.entry.max_dte} DTE window | "
        f"[bold]{len(cands)}[/bold] passed screening\n"
    )
    if not cands:
        console.print(
            "[yellow]Nothing passed. Loosen the filters or wait for a better chain.[/yellow]"
        )
        return

    t = Table(header_style="bold cyan")
    t.add_column("Contract", no_wrap=True)
    t.add_column("DTE", justify="right")
    t.add_column("Mid", justify="right")
    t.add_column("Spread", justify="right")
    t.add_column("Delta", justify="right")
    t.add_column("Theta/d", justify="right")
    t.add_column("IV", justify="right")
    t.add_column(f"Move for +{cfg.exit.take_profit_pct:.0%}", justify="right")
    t.add_column("Sigmas", justify="right")
    for c in cands[:limit]:
        move = required_underlying_move(
            c.quote, cfg.exit.take_profit_pct, float(cfg.exit.max_hold_days), c.dte
        )
        sig = move_in_sigmas(c.quote, move, float(cfg.exit.max_hold_days))
        t.add_row(
            c.contract.short,
            str(c.dte),
            f"${c.quote.mid:.2f}",
            f"{c.quote.spread_pct:.1%}",
            f"{c.delta:+.2f}",
            f"{c.theta_pct_per_day:.2%}",
            f"{c.iv:.0%}",
            f"{move:.2%}",
            f"{sig:.2f}",
        )
    console.print(t)
    console.print(
        "\n[dim]'Sigmas' is the required move divided by the move the market is pricing over "
        "the holding period. Below ~1.0 the target is a normal fluctuation; above ~2.0 you are "
        "betting on an outlier.[/dim]"
    )


@app.command()
def greeks(
    spot: float = typer.Option(..., help="Underlying price."),
    strike: float = typer.Option(..., help="Strike price."),
    dte: int = typer.Option(30, help="Calendar days to expiry."),
    iv: float = typer.Option(0.30, help="Implied volatility, e.g. 0.30 for 30%."),
    right: str = typer.Option("call"),
    rate: float = typer.Option(0.042),
) -> None:
    """Price one contract and print its Greeks."""
    g = compute_greeks(spot, strike, years_to_expiry(dte), rate, iv, 0.0, right)  # type: ignore[arg-type]
    t = Table(header_style="bold cyan", title=f"{right.upper()} {strike:g} · {dte}d · IV {iv:.0%}")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    t.add_column("Meaning")
    t.add_row("Price", f"${g.price:.2f}", f"${g.price * 100:,.0f} per contract")
    t.add_row("Delta", f"{g.delta:+.3f}", f"moves ${abs(g.delta) * 100:.0f} per $1 in the stock")
    t.add_row("Gamma", f"{g.gamma:.4f}", f"delta changes {g.gamma:.4f} per $1")
    t.add_row("Theta", f"${g.theta:+.2f}/day", f"{g.theta_pct_per_day:.2%} of the contract per day")
    t.add_row("Vega", f"${g.vega:+.2f}", "per +1 point of implied volatility")
    console.print(t)


@app.command()
def simulate(
    config: ConfigOpt = None,
    compare: Path | None = typer.Option(
        None, "--compare", help="A second config to run side by side."
    ),
    worlds: int = typer.Option(30, help="Independent synthetic markets to run."),
    days: int = typer.Option(180, help="Calendar days per world."),
    seed: int = typer.Option(1234),
    json_out: Path | None = typer.Option(None, "--json", help="Write raw results here."),
) -> None:
    """Run the full agent across many synthetic markets and report the distribution."""
    _setup_logging(False)
    logging.getLogger("optionsagent").setLevel(logging.WARNING)

    runs = [("recommended" if config is None else config.stem, _load(config))]
    if compare:
        runs.append((compare.stem, _load(compare)))

    results = []
    for label, cfg in runs:
        console.print(f"[cyan]running {worlds} worlds x {days} days for '{label}'...[/cyan]")
        results.append(run_simulation(cfg, label=label, worlds=worlds, days=days, seed=seed))

    t = Table(title=f"{worlds} synthetic markets x {days} days", header_style="bold cyan")
    t.add_column("Metric")
    for r in results:
        t.add_column(r.label, justify="right")

    def row(name: str, fmt) -> None:
        t.add_row(name, *[fmt(r) for r in results])

    row("Trades per world", lambda r: f"{r.trades / max(r.worlds, 1):.1f}")
    row("Win rate", lambda r: f"{r.win_rate:.1%}")
    row(
        "Break-even from observed win/loss sizes",
        lambda r: f"{breakeven_win_rate(r.avg_win_pct, r.avg_loss_pct):.1%}" if r.trades else "n/a",
    )
    row("Average win", lambda r: f"{r.avg_win_pct:+.1%}")
    row("Average loss", lambda r: f"{r.avg_loss_pct:+.1%}")
    row("Expectancy per trade", lambda r: f"{r.expectancy_per_trade:+.2%}")
    row(
        "Profit factor",
        lambda r: f"{r.profit_factor:.2f}" if r.profit_factor is not None else "n/a",
    )
    row("Median account return", lambda r: f"{r.as_dict()['median_return']:+.1%}")
    row("5th percentile", lambda r: f"{r.percentile(0.05):+.1%}")
    row("95th percentile", lambda r: f"{r.percentile(0.95):+.1%}")
    row("Chance of ending up", lambda r: f"{r.as_dict()['prob_profit']:.0%}")
    row("Average max drawdown", lambda r: f"{r.as_dict()['avg_max_drawdown']:.1%}")
    row(
        "Kelly sizing implied",
        lambda r: f"{kelly_fraction(r.win_rate, r.avg_win_pct, r.avg_loss_pct):.1%}",
    )
    console.print(t)

    for r in results:
        reasons = ", ".join(f"{k} {v}" for k, v in r.as_dict()["exit_reasons"].items()) or "none"
        console.print(f"[dim]{r.label} exits: {reasons}[/dim]")

    console.print(
        "\n[yellow]Read this as a mechanical stress test of the rules, not a forecast.[/yellow] "
        "The synthetic market has no real predictability in it, so a rule set that survives here "
        "is one whose costs and exits are sane, not one that is proven profitable."
    )

    if json_out:
        json_out.write_text(json.dumps([r.as_dict() for r in results], indent=2))
        console.print(f"[green]wrote {json_out}[/green]")


@app.command()
def run(
    config: ConfigOpt = None,
    loops: int = typer.Option(1, help="Number of loop iterations. Use -1 to run continuously."),
    live: bool = typer.Option(
        False, "--live", help="Reserved; live execution is disabled in this release."
    ),
    advance_days: int = typer.Option(
        0,
        "--advance-days",
        help="Paper mode only: advance the synthetic market by N days between loops "
        "so exits actually trigger. Ignored for live data.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the paper agent. Live execution is disabled."""
    _setup_logging(verbose)
    cfg = _load(config)
    if loops < -1 or advance_days < 0:
        raise typer.BadParameter("loops must be -1 or nonnegative; advance-days cannot be negative")

    if live or cfg.mode in (Mode.LIVE_APPROVAL, Mode.LIVE_AUTO):
        raise typer.BadParameter(
            "Live execution is disabled until account schemas and order recovery are verified."
        )
    if cfg.mode is Mode.OFF:
        console.print("Agent is off; no account or market-data calls made.")
        return
    if cfg.broker.kind != "paper":
        raise typer.BadParameter(
            "This release requires broker.kind: paper; choose data_provider separately"
        )
    with single_writer(cfg.state_dir):
        data = build_provider(cfg.data_provider, risk_free_rate=cfg.market.risk_free_rate)
        if cfg.reference_data_file and cfg.data_provider == "robinhood_mcp":
            data.reference_data_file = cfg.reference_data_file
        broker = build_broker(
            "paper",
            starting_equity=cfg.broker.starting_equity,
            clock=lambda: _sim_now(data) or datetime.now(UTC),
        )
        if cfg.data_provider == "robinhood_mcp" and cfg.reference_auto_refresh:
            cfg.reference_data_file = cfg.reference_data_file or str(
                Path(cfg.state_dir) / "reference.json"
            )
            data.reference_data_file = cfg.reference_data_file
        store = RuntimeStore(cfg.state_dir)
        # The database checkpoint is authoritative. JSON files are legacy exports.
        portfolio = Portfolio(cfg.state_dir) if store.read() else Portfolio.load(cfg.state_dir)
        engine = TradingEngine(cfg, data, broker, portfolio, store=store)
        if not store.restore(engine):
            engine.warmup()
            engine._checkpoint()
        import time as wall_time

        count = 0
        from threading import Event, Thread

        reference_stop = Event()

        def refresh_worker():
            from .health import Alerts
            from .mcp.client import HttpToolCaller
            from .reference_feed import refresh_reference

            # A separate connection keeps slow reference requests off the exit-monitor path.
            caller = HttpToolCaller()
            while not reference_stop.is_set():
                try:
                    issues = refresh_reference(
                        caller,
                        cfg.universe.symbols,
                        cfg.reference_data_file,
                        Path(cfg.state_dir) / "iv.sqlite3",
                    )
                    if issues:
                        Alerts(cfg.state_dir).set("reference", "; ".join(issues))
                    else:
                        Alerts(cfg.state_dir).clear("reference")
                except Exception as exc:
                    Alerts(cfg.state_dir).set(
                        "reference", f"Reference refresh failed: {type(exc).__name__}"
                    )
                reference_stop.wait(3600)

        if cfg.data_provider == "robinhood_mcp" and cfg.reference_auto_refresh:
            Thread(target=refresh_worker, daemon=True).start()
        try:
            while loops < 0 or count < loops:
                if hasattr(data, "step"):
                    data.step(advance_days or 1)
                    while data.today.weekday() >= 5:
                        data.step(1)
                try:
                    report = engine.run_once(as_of=_sim_now(data))
                    console.print(f"[cyan]loop {count + 1}[/cyan]: {report.describe()}")
                except Exception as exc:
                    # Persist uncertainty; no blind retry after a partially completed loop.
                    engine.reconcile_halt = (
                        "loop failed; inspect audit and reconcile before entries"
                    )
                    store.event("error", {"type": type(exc).__name__, "message": str(exc)})
                    engine._checkpoint()
                    from .mcp.client import McpError

                    if loops >= 0 or not isinstance(exc, (OSError, McpError)):
                        raise
                    console.print("Data connection failed; entries halted, monitoring will retry.")
                count += 1
                if loops < 0:
                    wall_time.sleep(cfg.execution.poll_interval_seconds)
        except KeyboardInterrupt:
            engine._checkpoint()
        finally:
            reference_stop.set()
        _print_status(portfolio)


def _sim_now(data) -> datetime | None:
    """Use the synthetic market's clock in paper mode so time actually passes."""
    today = getattr(data, "today", None)
    if today is None:
        return None
    return datetime.combine(today, time(15, 0), tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


@app.command()
def status(config: ConfigOpt = None) -> None:
    """Show open positions and closed-trade statistics from saved state."""
    cfg = _load(config)
    store = RuntimeStore(cfg.state_dir)
    snapshot = store.read()
    if snapshot:
        from .models import ExitReason, TradeRecord
        from .runtime import position

        pf = Portfolio(cfg.state_dir)
        pf.positions = {p.contract.occ_symbol: p for p in map(position, snapshot["positions"])}
        pf.realized_pnl = snapshot["realized_pnl"]
        for raw in snapshot["trades"]:
            row = dict(raw)
            row["opened_at"] = datetime.fromisoformat(row["opened_at"])
            row["closed_at"] = datetime.fromisoformat(row["closed_at"])
            row["reason"] = ExitReason(row["reason"])
            pf.trades.append(TradeRecord(**row))
        halt = snapshot.get("reconcile_halt") or snapshot["risk"].get("halted_reason") or "none"
        console.print(
            Panel(
                f"Mode: {snapshot['config']['mode']} | "
                f"Data: {snapshot['config']['data_provider']}\n"
                f"Simulated cash: ${snapshot.get('paper', {}).get('cash', 0):,.2f}\n"
                f"Entry halt: {halt}\n"
                f"Saved: {snapshot.get('saved_at', 'unknown')}",
                title="Options Agent",
            )
        )
        _print_status(pf)
    else:
        _print_status(Portfolio.load(cfg.state_dir))


def _print_status(portfolio: Portfolio) -> None:
    if portfolio.positions:
        t = Table(title="Open positions", header_style="bold cyan")
        t.add_column("Contract")
        t.add_column("Qty", justify="right")
        t.add_column("Entry", justify="right")
        t.add_column("Mark", justify="right")
        t.add_column("Return", justify="right")
        t.add_column("Peak", justify="right")
        t.add_column("Trailing", justify="right")
        for p in portfolio.positions.values():
            mark = p.last_mark or p.entry_price
            ret = p.unrealized_return(mark)
            colour = "green" if ret >= 0 else "red"
            t.add_row(
                str(p.contract),
                str(p.quantity),
                f"${p.entry_price:.2f}",
                f"${mark:.2f}",
                f"[{colour}]{ret:+.1%}[/{colour}]",
                f"{p.peak_return:+.1%}",
                "armed" if p.trailing_armed else "-",
            )
        console.print(t)
    else:
        console.print("[dim]No open positions.[/dim]")

    summary = portfolio.summary()
    if summary["closed_trades"]:
        s = Table(title="Closed trades", header_style="bold cyan")
        s.add_column("Metric")
        s.add_column("Value", justify="right")
        for key in (
            "closed_trades",
            "realized_pnl",
            "win_rate",
            "avg_win_pct",
            "avg_loss_pct",
            "profit_factor",
        ):
            s.add_row(key.replace("_", " "), str(summary[key]))
        console.print(s)
        console.print(f"[dim]exits: {summary['exit_reasons']}[/dim]")
    else:
        console.print("[dim]No closed trades yet.[/dim]")


@app.command()
def dashboard(config: ConfigOpt = None) -> None:
    """Read-only terminal dashboard; refreshes persisted state every two seconds."""
    import sqlite3
    import time as wall_time

    from rich.console import Group
    from rich.live import Live

    cfg = _load(config)
    path = Path(cfg.state_dir) / "runtime.sqlite3"
    if not path.exists():
        raise typer.BadParameter("Run a simulation first to create the dashboard state")

    def render():
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()
            events = db.execute(
                "SELECT at, kind, body FROM audit ORDER BY id DESC LIMIT 5"
            ).fetchall()
        data = json.loads(row[0]) if row else {}
        table = Table(title="Positions (simulated)")
        for col in ("Symbol", "Right", "Strike", "Expiry", "Qty", "Entry", "Mark", "Trail"):
            table.add_column(col)
        for p in data.get("positions", []):
            c = p["contract"]
            table.add_row(
                c["symbol"],
                c["right"],
                str(c["strike"]),
                c["expiry"],
                str(p["quantity"]),
                str(p["entry_price"]),
                str(p["last_mark"]),
                "armed" if p["trailing_armed"] else "off",
            )
        current = data.get("config", {})
        status = f"Mode: {current.get('mode')} | Data: {current.get('data_provider')}\n"
        status += f"Cash: {data.get('paper', {}).get('cash', 0):.2f} | "
        status += f"Realized P/L: {data.get('realized_pnl', 0):.2f}\n"
        status += "Halt: " + str(
            data.get("reconcile_halt") or data.get("risk", {}).get("halted_reason") or "none"
        )
        pending = [o for o in data.get("orders", {}).values() if o["state"] == "pending"]
        status += f" | Pending orders: {len(pending)}"
        status += f"\nSaved: {data.get('saved_at', 'unknown')}"
        if Path(cfg.risk.kill_switch_file).exists():
            status += " | OPERATOR PAUSE"
        activity = "\n".join(f"{at} {kind}: {body}" for at, kind, body in events)
        return Group(
            Panel(status, title="Options Agent — Simulation"),
            table,
            Panel(activity, title="Recent decisions and health"),
        )

    try:
        with Live(render(), console=console, refresh_per_second=1) as live_view:
            while True:
                wall_time.sleep(2)
                live_view.update(render())
    except KeyboardInterrupt:
        pass


@app.command()
def pause(config: ConfigOpt = None) -> None:
    """Pause entries; held-position monitoring continues."""
    cfg = _load(config)
    path = Path(cfg.risk.kill_switch_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    console.print("New entries paused. Position monitoring continues.")


@app.command()
def resume(config: ConfigOpt = None) -> None:
    """Remove the operator pause; risk/reconciliation halts remain enforced."""
    cfg = _load(config)
    Path(cfg.risk.kill_switch_file).unlink(missing_ok=True)
    console.print("Operator pause removed. Data, risk and reconciliation checks still apply.")


@app.command("init-config")
def init_config(
    path: Path = typer.Argument(Path("config/my-config.yaml")),
) -> None:
    """Write the default configuration to a YAML file you can edit."""
    Config().dump(path)
    console.print(f"[green]wrote {path}[/green]")


@app.command("mcp-probe")
def mcp_probe(
    snapshot: Annotated[
        Path | None, typer.Option(help="Write the full tool schemas here as JSON.")
    ] = None,
) -> None:
    """Enumerate the Robinhood Trading MCP tools your account actually exposes.

    Robinhood publishes tool names but not response schemas, and the server is in
    beta. Run this before trusting the adapters, and again whenever something
    starts parsing oddly: a missing tool usually means options approval is still
    pending rather than that the code is broken.

    Run optionsagent auth-login first. Uses this application's own OS-keyring
    credential, or an explicitly supplied ROBINHOOD_MCP_TOKEN.
    """
    from .mcp.client import HttpToolCaller, McpError
    from .mcp.robinhood import REQUIRED_TOOLS

    try:
        caller = HttpToolCaller()
        tools = caller.list_tools()
    except McpError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    table = Table(title=f"{len(tools)} tools available", header_style="bold cyan")
    table.add_column("Tool")
    table.add_column("Needed", justify="center")
    table.add_column("Description", overflow="fold")
    available = {t.get("name") for t in tools}
    for tool in sorted(tools, key=lambda t: str(t.get("name"))):
        name = str(tool.get("name"))
        table.add_row(
            name,
            "[green]yes[/green]" if name in REQUIRED_TOOLS else "",
            str(tool.get("description", ""))[:90],
        )
    console.print(table)

    missing = [t for t in REQUIRED_TOOLS if t not in available]
    if missing:
        console.print(
            Panel(
                "This account is missing: " + ", ".join(missing) + "\n"
                "Options tools need level 2 approval. Ask your agent to call "
                "get_option_level_upgrade_info for the application link.",
                title="Not ready to trade options",
                border_style="red",
            )
        )
    else:
        console.print("[green]All tools this agent needs are present.[/green]")

    if snapshot:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(tools, indent=2, sort_keys=True))
        console.print(f"[dim]wrote {snapshot} — diff it after any Robinhood update[/dim]")


@app.command("auth-login")
def auth_login(no_browser: bool = typer.Option(False, "--no-browser")):
    """Connect robin-ops directly to Robinhood using its own OAuth client."""
    from .mcp.oauth import AuthError, login

    try:
        login(open_browser=not no_browser, announce=typer.echo)
    except AuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("auth-status")
def auth_status():
    """Check local login metadata without displaying credentials or fetching accounts."""
    import time

    from .mcp.oauth import AuthError, CredentialStore

    try:
        saved = CredentialStore().read()
        if not saved.get("access_token"):
            typer.echo("Not signed in. Run optionsagent auth-login.")
        else:
            state = "valid" if saved.get("expires_at", 0) > time.time() else "expired"
            typer.echo(
                f"robin-ops credential: {state}; refresh available: "
                f"{bool(saved.get('refresh_token'))}. Live trading disabled."
            )
    except AuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("auth-logout")
def auth_logout():
    """Delete this application's local credentials; revoke provider access separately."""
    from .mcp.oauth import AuthError, CredentialStore, credential_lock

    try:
        with credential_lock():
            CredentialStore().clear()
        typer.echo(
            "Local robin-ops credentials removed. To revoke the grant, use Robinhood "
            "Security & Privacy settings."
        )
    except AuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("reference-refresh")
def reference_refresh(config: ConfigOpt = None):
    """Collect daily prices, earnings, exchange sessions and available IV history."""
    from .mcp.client import HttpToolCaller
    from .reference_feed import refresh_reference

    cfg = _load(config)
    path = cfg.reference_data_file or str(Path(cfg.state_dir) / "reference.json")
    errors = refresh_reference(
        HttpToolCaller(), cfg.universe.symbols, path, Path(cfg.state_dir) / "iv.sqlite3"
    )
    console.print(f"Reference snapshot saved to {path}")
    for error in errors:
        console.print(error)


@app.command("iv-import")
def iv_import(path: Path, config: ConfigOpt = None):
    """Import sourced daily ATM ~30D IV observations (symbol,date,iv,source CSV)."""
    from .reference_feed import IVArchive

    cfg = _load(config)
    count = IVArchive(Path(cfg.state_dir) / "iv.sqlite3").import_csv(path)
    console.print(f"Imported {count} daily IV observations; no trading settings changed.")


@app.command("web-dashboard")
def web_dashboard(config: ConfigOpt = None, port: int = 8766, no_browser: bool = False):
    """Serve the private browser dashboard on this Mac only."""
    from .webserver import serve

    serve(_load(config), port, not no_browser)


@app.command("recover")
def recover(evidence: Path, config: ConfigOpt = None, apply: bool = False):
    """Preview a sourced paper-account repair; --apply records and applies it."""
    from .recovery import repair

    console.print(repair(_load(config).state_dir, evidence, apply))


@app.command("replay")
def replay_command(path: Path, output: Path, config: ConfigOpt = None, folds: int = 3):
    """Replay sourced point-in-time quotes and write held-out/stressed results."""
    from .replay import load_frames, replay, walk_forward

    cfg = _load(config)
    frames = load_frames(path)
    result = {"full_period": replay(cfg, frames), "walk_forward": walk_forward(cfg, frames, folds)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2))
    console.print(
        f"Replay report saved to {output}. Review data coverage before interpreting returns."
    )


@app.command("capture-quotes")
def capture_quotes(output: Path, config: ConfigOpt = None):
    """Append one sourced market-data snapshot for future replay; no orders."""
    from .replay import capture

    cfg = _load(config)
    if cfg.data_provider != "robinhood_mcp" or not cfg.reference_data_file:
        raise typer.BadParameter("Use a Robinhood paper config with reference_data_file")
    data = build_provider(cfg.data_provider, risk_free_rate=cfg.market.risk_free_rate)
    data.reference_data_file = cfg.reference_data_file
    console.print(f"Captured {capture(data, cfg, output)} quotes")


@app.command("monitor")
def monitor(config: ConfigOpt = None, notify: bool = False, once: bool = False):
    """Independent heartbeat monitor; optional native Mac notifications."""
    import subprocess
    import sys
    import time

    from .health import Alerts

    cfg = _load(config)
    alerts = Alerts(cfg.state_dir)
    seen = set()
    while True:
        alerts.watchdog(max(180, cfg.execution.poll_interval_seconds * 3))
        for alert in alerts.list():
            identity = (alert["key"], alert["updated_at"])
            if alert["active"] and not alert["acknowledged"] and identity not in seen:
                console.print(alert["severity"] + ": " + alert["message"])
                seen.add(identity)
                if notify and sys.platform == "darwin":
                    subprocess.run(
                        [
                            "osascript",
                            "-e",
                            'display notification "Paper agent needs attention. '
                            'Open your local dashboard." with title "Robin Ops"',
                        ],
                        check=False,
                        capture_output=True,
                    )
        if once:
            return
        time.sleep(30)


@app.command("lifecycle-replay")
def lifecycle_replay(evidence: Path, database: Path):
    """Validate sourced broker lifecycle fixtures in an isolated local ledger; no API calls."""
    from .lifecycle import Lifecycle

    if database.exists():
        raise typer.BadParameter("Use a new database path to preserve prior evidence")
    fixture = json.loads(evidence.read_text())
    ledger = Lifecycle(database)
    for intent in fixture["intents"]:
        ledger.reserve(**intent)
    results = [ledger.apply(**event) for event in fixture["events"]]
    console.print({"events_checked": len(results), "results": results})


@app.command("alpha-key")
def alpha_key():
    """Save an Alpha Vantage key privately in the native OS credential store."""
    from getpass import getpass

    from .alpha_vantage import save_key

    save_key(getpass("Alpha Vantage API key (hidden): "))
    console.print("Key saved privately. No API requests made.")


@app.command("alpha-history")
def alpha_history(
    output: Path,
    config: ConfigOpt = None,
    days: int = 252,
    max_requests: int = 10,
    rpm: int = 5,
    symbol: str | None = None,
):
    """Download resumable EOD IV history; default request budget is ten. No orders."""
    from .alpha_vantage import AlphaClient, download, load_key

    cfg = _load(config)
    try:
        result = download(
            AlphaClient(load_key(), rpm),
            [symbol] if symbol else cfg.universe.symbols,
            output,
            days,
            max_requests,
        )
        console.print(result)
        console.print("History saved; not imported or enabled for trading automatically.")
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
