# Private paper-agent operating guide

This release runs on your Mac. The supported runner only uses a paper broker. Robinhood supplies market data through the application's own OAuth connection. No real order submission is enabled. A working connection and passing software tests do not establish a profitable strategy.

## Start from the repository folder

Install or update in your existing Python environment:

```sh
python -m pip install -e '.[dev,oauth]'
optionsagent auth-status
```

If not signed in, use `optionsagent auth-login`. Complete Robinhood's own consent page; do not paste passwords or tokens into configuration. Credentials live in macOS Keychain. The provider grant is broader than this application's read-only tool allowlist. Logging out removes local credentials; revoke the grant in Robinhood if you also want provider access removed.

For a synthetic demonstration, run these in separate Terminal tabs:

```sh
optionsagent run --config config/recommended.yaml --loops -1
optionsagent web-dashboard --config config/recommended.yaml
```

The dashboard opens a private `127.0.0.1` page with a random access key saved in an owner-only `dashboard-access.key` file under the configured state directory. The private link remains valid across dashboard restarts. The browser remembers access locally and establishes an HttpOnly, SameSite=Strict session cookie; restored tabs can recover using the private link. Do not share its full link. It shows paper equity, cash, marked and realized P&L, holdings, exit thresholds, trades, entry blocks, pending orders, recent decisions, data readiness and alerts. Pause blocks new entries while monitoring existing holdings. Resume removes the manual pause; it does not clear reconciliation or risk halts. Closing the dashboard does not stop the runner.

## Paper trading with Robinhood quotes

Use the same configuration for every process:

```sh
optionsagent reference-refresh --config config/paper-robinhood.yaml
optionsagent run --config config/paper-robinhood.yaml --loops -1
```

In a second tab:

```sh
optionsagent web-dashboard --config config/paper-robinhood.yaml
```

Optional third tab for native Mac notifications:

```sh
optionsagent monitor --config config/paper-robinhood.yaml --notify
```

This configuration isolates its $25,000 simulated balance in `state/robinhood-paper`. It does not use or transfer your real account balance. The background reference worker refreshes hourly using its own connection so slow reference requests cannot delay exit monitoring. For a one-cycle test, run `reference-refresh` first, then `run --loops 1`. The independent monitor detects a missing or stale heartbeat; it cannot monitor while the Mac is asleep or all processes are stopped.

## Required reference data

The automatic feed collects completed split-adjusted daily closes, upcoming earnings when supplied by Robinhood, XNYS holidays and shortened sessions, and available near-close ATM call IV around 30 DTE. Unknown earnings, stale prices, incomplete history and closed sessions block entries.

**The IV rank requires at least 200 sourced completed-session observations, using up to 252.** Robinhood's available tool schemas do not provide a ready-made historical ATM IV series. Forward collection starts with one observation. To avoid waiting for 200 sessions, obtain consistent daily ATM approximately 30-day implied volatility from a legitimate historical data source, then import a CSV:

```csv
symbol,date,iv,source
AAPL,2026-09-04,0.30,Provider and dataset identifier
```

The example row only illustrates the format; it is not verified market history. IV is annualized decimal volatility, not a percentage or realized volatility. Do not mix different definitions to fill gaps. Import all actual rows and rebuild the reference:

```sh
optionsagent iv-import /absolute/path/history.csv --config config/paper-robinhood.yaml
optionsagent reference-refresh --config config/paper-robinhood.yaml
```

No paid provider or subscription has been chosen. The daily rank currently describes the latest completed session, not an intraday rank. Software cannot manufacture missing evidence of an edge.

## Capture and historical evaluation

```sh
optionsagent capture-quotes state/robinhood-paper/quotes.jsonl --config config/paper-robinhood.yaml
optionsagent replay state/robinhood-paper/quotes.jsonl state/robinhood-paper/replay.json --config config/paper-robinhood.yaml
```

Capture appends one observation; call it at each desired observation time. It includes current entry candidates and held contracts even after they leave the entry DTE window. The runner does not automatically schedule captures. Keep full holding-period paths, including failed or absent quotes. At least six observations are needed for the default three-fold report, but that minimum is only a software requirement, not statistically useful evidence.

Replay runs the strategy with dated quotes and supplied point-in-time reference snapshots. It rejects future or stale reference timestamps and reports monitoring gaps. It evaluates a fixed configuration in sequential held-out folds, each starting flat, and compares ordinary and worse fill assumptions. It does not tune parameters on the historical prefix. Open holdings remain marked at the end; they are not silently liquidated. Returns with incomplete quote paths or stale marks are not reliable. This is not an order-book, queue-position or partial-fill market simulator.

## Recovery

Stop the runner before repair; a writer lock prevents concurrent account edits. Recovery takes a JSON evidence file and previews by default:

```sh
optionsagent recover /absolute/path/evidence.json --config config/paper-robinhood.yaml
optionsagent recover /absolute/path/evidence.json --config config/paper-robinhood.yaml --apply
```

Supported evidence types:

- `resolve_order`: requires `order_id`, `resolution: "verified_no_execution"`, `executed_quantity: 0`, and `working: false` for a pending paper order.
- `settlement`: requires an expired matched paper holding's `contract` (OCC symbol), `expiry`, and independently verified `settlement_per_share`. This is reviewed simulated settlement accounting, not real exercise or assignment processing.
- `clear_reconcile`: only clears the halt when holdings match and no order is pending.

All require nonempty `source` and `reason`. Newer order-journal evidence blocks repair until runner restoration incorporates it. Never assert zero execution just to remove a warning. Applied repairs retain the prior checkpoint and review reason in the SQLite audit log. Back up the stopped agent's entire state folder before repairs.

## Broker lifecycle foundation

`lifecycle.py` provides a separate transactional evidence ledger for account-scoped intents, execution deduplication, partial fills, cancellation acknowledgement, late fills, quantity/cash comparison, and protective limit tick rounding. Test supplied normalized fixtures with:

```sh
optionsagent lifecycle-replay /absolute/path/fixture.json /absolute/path/new-ledger.sqlite3
```

Fixture shape: `intents` is a list of `{identity, account, contract, side, qty}`; `events` contains `{identity, account, broker_id, state, executions, source}`; each execution has `{id, quantity, price, fee}`. Use a new database path. This command performs no network calls. The ledger is a tested foundation, **not a connected live execution engine**. Provider-specific execution envelopes, live account cash/position reconciliation, exercise/assignment and real cancellation acceptance still need dedicated integration and acceptance testing before a future live release.

## Validation and remaining acceptance work

On September 7, 2026, the application successfully read AAPL's quote, a normal options chain with 24 expiries, 90 completed daily closes, an upcoming earnings date and one IV observation through its standalone OAuth client. An isolated real-data/paper-broker cycle correctly blocked entries outside the verified session. No actual account balances, holdings or real order endpoints were used.

The automated suite covers data schemas, pagination, holidays, exits, sizing, restart/duplicate protection, persistence, uncertain fills, lifecycle evidence, repair guards and private dashboard authorization. Still required: a sourced IV archive, sustained market-hours paper operation, complete historical option quote paths, and an evidence-based strategy assessment. The original weak profit-factor results remain a reason to keep live trading disabled. A +10% trailing trigger cannot guarantee a +10% realized profit when quotes gap or exits do not fill.

## Alpha Vantage historical IV

`optionsagent alpha-key` prompts for a hidden key and stores it in the native credential store, separate from Robinhood. A free key alone does not establish historical-options entitlement. Verify with a two-request, one-symbol test:

```sh
optionsagent alpha-key
optionsagent alpha-history state/alpha/iv.csv --symbol AAPL --days 1 --max-requests 2
```

With confirmed premium access, download the configured universe in resumable batches:

```sh
optionsagent alpha-history state/alpha/iv.csv --config config/paper-robinhood.yaml --days 252 --max-requests 100 --rpm 5
```

The default is only ten requests per invocation. Choose a rate within your plan. One symbol needs one unadjusted daily-price request plus approximately 252 option-chain requests; the 20-symbol universe needs approximately 5,060 requests, plus daily-price reloads on resumed batches. Resume with the same output filename. A successful file contains only dated standard calls, expiry 20–45 DTE nearest 30 days, strike nearest that day's unadjusted close. The IV must be positive and its bid/ask valid. Missing days are reported, never fabricated. The output is a vendor-specific approximate ATM series, not a constant-maturity interpolated index.

After checking coverage, import it with `iv-import` and rebuild reference data. Do not import the one-day probe as a ready archive. The rank rejects mixed source definitions, and Robinhood's forward collector leaves external archives untouched. Continue refreshing Alpha Vantage history daily and importing it; this vendor download is manual, not an automatic background subscription. Live execution remains disabled.

Documentation: https://www.alphavantage.co/documentation/#historical-options


## Paper experiment with DoltHub IV history

The paper profile now enables `paper_iv_history_experiment: true` and `entry.require_iv_rank: true`. It uses the imported same-source daily history and automatically checks for the latest completed-day observation in the hourly reference worker. Missing or stale IV skips new entries; it does not stop exit monitoring. The source methodology remains unverified, so the dashboard labels the filter experimental and configuration blocks its use outside paper mode. See [IV import and filtering](iv-history.md) for validation rules and limitations.

Observe entries, skips, simulated fills, exits, state recovery, and errors over multiple market sessions with the Mac awake and online. Keep the paper broker enabled. A week of observation does not establish profitability and does not enable live trading automatically.
