# optionsagent

## Private dashboard and paper operations

See [the paper MVP operating guide](docs/PAPER-MVP.md) for the local dashboard, standalone Robinhood data connection, automatic reference feeds, alerts, reviewed recovery, quote capture and replay. Real trading remains disabled; sufficient sourced IV history is still required before real-data paper entries.

Simulation-first options agent with a deterministic scanner, contract selector,
risk controls, exit rules, durable account state, and a terminal dashboard.

**Live execution is disabled.** This release repairs the review's safety issues;
it does not claim a validated broker integration or a profitable strategy.
The HTTP MCP transport allows only explicitly listed read/review calls. The unofficial
Robinhood adapter is legacy code and is not reachable from the supported runner.

## Run locally

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
optionsagent run --config config/recommended.yaml --loops 20
optionsagent status
optionsagent dashboard
```

Each synthetic loop advances one calendar day by default, skipping weekends. Use `--advance-days`
to change that, or `--loops -1` to continue at the configured polling interval.
The dashboard is a read-only terminal view. Ctrl-C closes it without stopping
another running agent.

## Strategy profile

- 12–18 DTE, explicitly ITM calls/puts, absolute delta 0.55–0.70.
- Maximum $1,000 premium per trade plus portfolio/cash/position limits.
- Reject missing IV history, unknown earnings calendars, invalid quotes and
  stale live quotes. No qualifying candidate means no trade.
- +10% on the option bid activates trailing protection. Direction deterioration,
  configured trailing drawdown, hard loss, theta, time and expiry rules can exit.
  A trigger is not a guaranteed execution price or guaranteed profit.
- The loss threshold remains configurable (default -50%, with a tighter
  near-expiry rule); this is an experiment, not a validated risk recommendation.
- `config/experimental-long-dte.yaml` retains the earlier 30–45 DTE experiment.
  `config/brief.yaml` is an older comparison profile, not the current requirements.

## Operating boundaries

`off` makes no account/data calls. `scan_only` produces candidates but neither
opens nor closes positions. `paper` requires the paper broker. Both live modes
and `--live` refuse startup. Market data is selected independently using
`data_provider: synthetic` or `robinhood_mcp`.

For paper trading against MCP quotes, set `data_provider: robinhood_mcp`, keep
`broker.kind: paper`, and connect with `optionsagent auth-login` after installing
`pip install -e ".[oauth]"`.
Credentials are stored in the native OS keyring and refreshed before expiry.
See [standalone sign-in](docs/STANDALONE-AUTH.md).
Use `optionsagent mcp-probe` for read-only schema discovery. Do not commit tokens.
The provider requires timestamped quotes and a current sourced reference-data
snapshot for daily closes, historical ATM IV rank, earnings and exchange sessions.
See [reference data and remaining integration work](docs/REVIEW-FIXES.md).
Without these inputs it correctly reports no trade.

## Persistence, recovery and audit

The runner checkpoints paper cash, holdings, trade history, risk state, pending
orders, price history and synthetic market/RNG state together in SQLite at
`state/runtime.sqlite3`. Order intent is reserved before submission. Unknown
outcomes stay pending indefinitely; a timeout or an empty open-order list is
not evidence of cancellation. Only synchronous paper fills may be retried.

One process may own a state directory. Use different directories for different
accounts/data sources. The database is authoritative; legacy JSON exports are
not updated by the checkpointed runner. Legacy holdings without a complete account checkpoint refuse
automatic migration, rather than resetting cash and fabricating closed trades.
Back up the full state directory before any manual repair.

Reconciliation compares holdings both ways and checks quantities. Mismatches
latch an entry halt and are recorded for investigation, never booked as fictional
fills. The audit table records decisions, quotes used, errors and loop summaries.

```sh
optionsagent pause    # pause entries; position monitoring continues
optionsagent resume   # remove operator pause; safety halts still apply
```

## Verification

```sh
pytest -q
ruff check .
```

Tests cover queued orders, timeouts, partial fills, strict modes, corrupt state,
full restart recovery, reconciliation, quote freshness, costs and profit-lock.
Synthetic sweeps are mechanical stress tests, not historical backtests or
out-of-sample evidence. Previously published sweep statistics predate these fixes
and must not be used to select live parameters.

See the [second-review design checklist](docs/SECOND-REVIEW.md) for current readiness, fixes and remaining MVP work.
