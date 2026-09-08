# Forward paper comparisons

The comparison service starts three independent simulated accounts with the same
configured starting balance. The original paper account continues unchanged.

- **Current rules:** a fresh control portfolio using the current strategy.
- **Smaller loss stop:** a -25% main option stop instead of -50%. The sizing risk
  fraction is reduced proportionally, preserving the control's premium budget.
  Other exit rules still apply; a stop is a trigger, not a guaranteed fill price.
- **Market agreement:** the stock's momentum direction must match SPY's direction.
  A missing/neutral/opposite SPY signal blocks entry. Other rules remain unchanged.

These are hypotheses, not proven improvements. Changing several rules together
would make the comparison harder to interpret. Longer expiries, different profit
exits, and additional direction signals are deliberately deferred.

## Isolation and operation

`optionsagent paper-compare --config config/paper-robinhood.yaml --loops -1`
runs under `robin-ops-paper-lab.service`. It accepts only Robinhood data with the
paper broker. The engine's existing live-order prohibition remains in effect.
Accounts, positions, orders and audit records live in separate `experiments/*-v1`
folders. Locks prevent duplicate processes; checkpoints restore on restart.
Configuration fingerprints reject changed rules against existing results. Future
strategy changes require a new comparison version rather than silently reusing
past results. The fingerprint tracks configuration, not source-code changes.

All portfolios read the existing shared IV history and source-selection rules.
They do not start extra IV collectors or download workers. The dashboard pause
file blocks new entries across the original account and comparisons; exits retain
the existing pause behavior. There is no automatic promotion to live trading.

The three comparisons share market responses for up to 30 seconds within each
pass. Slow scans may observe different quote times. The original account has its
own scan cycle. Therefore compare the fresh control with the other fresh
portfolios, not with the original account's accumulated balance. Paper fills are
simulations, not evidence that a real order would fill at the same price.

## Evidence and interpretation

The dashboard reports account return, closed and open counts, net closed P&L,
win rate, average win/loss, average net P&L per closed trade, and observed maximum
drawdown. Fees are included through the existing broker/trade accounting.
Drawdown uses sampled paper equity and can miss moves between scans.
Zero trades produce unknown outcome metrics, not a profitable result.

Evaluate net results and drawdown together, across enough independent trades and
market conditions. A week is useful for operations testing, not proof of an edge.
Do not select a winner from a few wins or continuously tune rules to past results.
Use a later untouched evaluation period before considering real money.

Quote/chain and history evidence is compressed in `experiments/comparison.sqlite3`
and retained for 30 days. This supports later analysis, not a historical options
backtest. Account audit/checkpoints and equity observations are durable. Hourly
backups include experiment account state and comparison metadata/equity, but omit
the rolling quote evidence. Backups remain on the same server; they are not an
off-server disaster-recovery copy.

## Daily review logs

The dashboard's **Daily paper review** downloads a Markdown report for a selected
New York date. It includes the original account and all comparison portfolios:
completed scans, first/last sampled equity, positions at the last scan, grouped
skip/block/error counts, decisions versus fills, and net closed-fill accounting.
New entry decisions include IV rank, direction, confidence and Greeks in the
structured audit. The report includes hold/other audit events in its JSON archive,
while the readable timeline focuses on entry/exit actions.

`robin-ops-report.timer` refreshes today's and yesterday's private Markdown/JSON
archives every five minutes under `state/robinhood-paper/reports/`. Reports remain
in hourly backups; raw audit records remain in each account's runtime database.
The authenticated `/api/report?date=YYYY-MM-DD` route regenerates selected dates.
No account login tokens are added to reports. Missing scans are reported as missing
activity, not assumed successful monitoring. Intraday reports remain provisional.
