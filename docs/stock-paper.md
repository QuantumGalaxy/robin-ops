# Regular stock paper account

This is an independent, long-only whole-share simulation. It reads Robinhood stock
quotes and five-minute candles. It never submits, reviews or approves a brokerage
order. It does not use options IV, Greeks, contract multipliers or options cash.
No strategy or account is automatically promoted to real-money trading.

## Starting rules (version stocks-orb-paper-v1)

- $25,000 simulated starting cash; no borrowing or short selling.
- $5,000 maximum purchase cost including the modeled entry fee per stock.
- $25 planned loss at the protective stop, including estimated round-trip costs.
- $50 net profit target per trade; three open positions maximum.
- Ten entries per session, three per symbol, and a 15-minute re-entry cooldown.
- $100 decline from first observed session equity triggers a sticky daily halt and
  attempts to close holdings. Missing prices and gaps can cause larger losses.
- Observe the first three regular-session five-minute candles (9:30–9:45 Eastern).
  A subsequent completed candle must close above that range's high while the
  previous completed candle closed at or below it. First possible entry: 9:50.
- Breakout volume must be at least 1.2 times the mean volume of the first three
  candles. This is an opening-range comparison, not a 20-day relative-volume measure.
- SPY's matching completed candle must close above its session opening price and
  above its preceding candle. Missing SPY observations block entry.
- Buy quotes must have a spread at most 0.2%, price at least $5, and ask above the
  opening-range high but no more than 1% above the confirming candle's close.
- Stop at the opening-range low or estimated net loss of $25, whichever is reached
  first. Quantity is rounded down using the stop distance and cash/risk limits.
- Stop new entries 30 minutes before the exchange close. Attempt to exit all stock
  holdings five minutes before close, including early-close sessions. A failed exit
  stays open and is explicitly reported; next session prioritizes its recovery.

These are hypotheses for forward testing, not demonstrated profitable rules.
A $50 target may be far from the entry when the risk budget permits only a few shares.
There is no obligation to trade each day or to achieve $50 every day.

## Data and execution

The watchlist is a fixed 50-name large-cap research universe, not an automatically
maintained ranking of the largest 50 companies. All 50 symbols were recognized by
Robinhood during the September 8, 2026 deployment check. Unsupported/missing/stale
observations are skipped; there is no substitution with synthetic prices.

The service attempts an exit check every 15 seconds and then scans up to nine
stocks plus SPY. A complete watchlist pass normally takes roughly 90–180 seconds;
network latency or errors can take longer. This is not an instant-execution system.
Both bid and ask timestamps must be within 30 seconds, rechecked at fill time.
Interpolated, duplicate, incomplete or malformed candles cannot qualify entries.
Stale holding marks also block new entries.

Simulated buy fills use ask plus 1 basis point; sells use bid minus 1 basis point.
The model charges $0.01 per side. Those costs are assumptions, not Robinhood's fee
schedule. Simulated fills do not model queue priority, partial fills or order-book
capacity. Live fills could differ significantly. The dashboard target fill price
is the estimated execution price needed for $50 net; the observed bid must cover
slippage as well.

## Persistence and operations

- Service: `robin-ops-stock-paper.service`, automatically starts after a reboot.
- Ledger: `state/robinhood-paper/stocks/stocks.sqlite3`.
- Fills, cash, positions, consumed signals and audit events commit atomically.
- An exclusive runner lock prevents duplicate workers. Restart preserves all state.
- A rules fingerprint prevents silently changing the experiment on a saved account.
- Dashboard Pause entries applies to this account; exit monitoring continues.
- Daily review downloads include stock scans, skip reasons, errors and fills.
- Existing recursive state backups include the stock SQLite ledger.

Review multiple market conditions and after-cost results before considering a live
implementation. Real-money support and brokerage account restrictions require a
separate review; they are not enabled by this change.
