"""Command line interface."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .brokers import build_broker
from .config import Config
from .engine import TradingEngine
from .greeks import compute_greeks, years_to_expiry
from .marketdata import build_provider
from .portfolio import Portfolio
from .simulate import breakeven_win_rate, kelly_fraction, run_simulation
from .strategy.entry import EntryScreener, move_in_sigmas, required_underlying_move
from .strategy.exits import trailing_stop_level
from .strategy.signals import MomentumSignal, PriceHistory

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
        "Buys 0.45-0.70 delta. High delta means a small stock move produces the "
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
        "outruns the thesis. This is why the agent buys 30-45 days out instead "
        "of 14.",
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
    row("Break-even win rate needed", lambda r: f"{breakeven_win_rate(0.10, -0.50):.1%}")
    row("Average win", lambda r: f"{r.avg_win_pct:+.1%}")
    row("Average loss", lambda r: f"{r.avg_loss_pct:+.1%}")
    row("Expectancy per trade", lambda r: f"{r.expectancy_per_trade:+.2%}")
    row("Profit factor", lambda r: f"{r.profit_factor:.2f}" if r.profit_factor else "n/a")
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
        False, "--live", help="Route orders to the configured broker for real."
    ),
    advance_days: int = typer.Option(
        0,
        "--advance-days",
        help="Paper mode only: advance the synthetic market by N days between loops "
        "so exits actually trigger. Ignored for live data.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the agent loop. Paper trading unless --live is passed."""
    _setup_logging(verbose)
    cfg = _load(config)

    if live:
        if cfg.broker.kind == "paper":
            console.print("[red]--live requires broker.kind = robinhood in the config.[/red]")
            raise typer.Exit(1)
        console.print(
            Panel(
                "[bold red]LIVE TRADING[/bold red]\n\n"
                "Real orders will be submitted to a real account. Options can lose "
                "100% of the premium paid, and this agent has no ability to detect a "
                "halted stock, a corporate action, or a broken quote feed.\n\n"
                "Run it in paper mode for a full options cycle first.",
                border_style="red",
            )
        )
        if cfg.broker.require_confirmation and not typer.confirm("Proceed with live trading?"):
            raise typer.Exit(1)
        cfg.broker.dry_run = False

    provider_kind = "paper" if cfg.broker.kind == "paper" else "robinhood"
    data = build_provider(provider_kind, risk_free_rate=cfg.market.risk_free_rate)
    if cfg.broker.kind == "paper":
        # Give the paper broker the synthetic market's clock. Without it,
        # day-trade accounting runs on wall-clock time while positions are
        # opened in simulated time, and every close looks like a day trade.
        broker_kwargs = {
            "starting_equity": cfg.broker.starting_equity,
            "clock": lambda: _sim_now(data) or datetime.now(UTC),
        }
    else:
        broker_kwargs = {"dry_run": cfg.broker.dry_run}
    broker = build_broker(cfg.broker.kind, **broker_kwargs)
    portfolio = Portfolio.load(cfg.state_dir)

    engine = TradingEngine(
        config=cfg,
        data=data,
        broker=broker,
        portfolio=portfolio,
        signal=MomentumSignal(),
        history=PriceHistory(),
    )
    engine.warmup()

    if loops < 0:
        engine.run_forever()
        return
    for i in range(loops):
        if advance_days and hasattr(data, "step"):
            data.step(advance_days)  # type: ignore[attr-defined]
        report = engine.run_once(as_of=_sim_now(data))
        console.print(f"[cyan]loop {i + 1}[/cyan]: {report.describe()}")
        for line in report.opened:
            console.print(f"  [green]opened[/green] {line}")
        for line in report.closed:
            console.print(f"  [magenta]closed[/magenta] {line}")
    _print_status(portfolio)


def _sim_now(data) -> datetime | None:
    """Use the synthetic market's clock in paper mode so time actually passes."""
    today = getattr(data, "today", None)
    if today is None:
        return None
    return datetime.combine(today, time(15, 0), tzinfo=UTC)


@app.command()
def status(config: ConfigOpt = None) -> None:
    """Show open positions and closed-trade statistics from saved state."""
    cfg = _load(config)
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

    Requires ROBINHOOD_MCP_TOKEN. Authorise once in a desktop browser through an
    MCP host, then export the access token.
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


if __name__ == "__main__":
    app()
