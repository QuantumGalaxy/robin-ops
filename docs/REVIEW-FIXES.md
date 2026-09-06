# Review fixes and remaining boundaries

## Implemented

- The CLI and engine enforce OFF/SCAN/PAPER; live modes and HTTP mutations are disabled.
- Asynchronous adapters submit at most one order per intent; timeout/partial/unknown
  outcomes stay reserved. Exits also use reservations. There is no TTL release.
- Partial fills use executed quantities; partial closes retain the remainder.
- Bid-based exit evaluation, signal-loss exit after profit-lock, entry-fee accounting.
- Bidirectional quantity reconciliation with a persistent entry halt; no fabricated closes.
- Atomic SQLite paper-account checkpoints, including risk and synthetic RNG/clock.
- Fixed profile, premium cap, explicit ITM checks and unknown-data rejection.
- Daily signals no longer consume minute polls. Held-position monitoring runs before
  unrelated symbol history refresh; each held quote failure is isolated.
- Timestamped live quotes and verified entry sessions; audit history and terminal dashboard.

## Reference feed contract (paper against MCP quotes)

`reference_data_file` names a JSON snapshot produced by a trusted market-data feed.
It must be refreshed within 24 hours and have a nonempty source. Example shape:

```json
{
  "as_of": "2026-09-08T13:30:00+00:00",
  "source": "your licensed data provider and dataset version",
  "session": {
    "open": "2026-09-08T13:30:00+00:00",
    "close": "2026-09-08T20:00:00+00:00"
  },
  "symbols": {
    "AAPL": {
      "daily_closes": [200.0, 201.0],
      "iv_rank": 0.4,
      "iv_history_days": 252,
      "earnings_checked": true,
      "earnings": "2026-10-29"
    }
  }
}
```

Values above are illustrative, not market facts. Supply at least 30 completed daily
bars for the signal. IV rank must use a historical daily ATM series (minimum 200
observations), not different strikes from one chain. Null earnings with
`earnings_checked: true` means the feed verified no upcoming event. The session
must use the exchange calendar, including holidays and shortened sessions.
Missing/stale inputs halt entries; no unavailable data is replaced with neutral values.

## Deliberately not enabled yet

Real broker schemas, account identity/permissions, OAuth refresh, execution reports,
fill pagination, cancel acknowledgements, and exercise/share reconciliation need
account-level integration fixtures before any live mode can be enabled. No live
order has been used to validate this release. MCP price fields now have an explicit
per-share fixture contract; do not assume the real server matches that contract.

Synthetic expiration settlement remains a model (intrinsic at the simulated spot),
not a model of real broker exercise or share delivery. Missing spot blocks settlement.
Live expiry handling must use actual broker records and is disabled with live execution.

A hosted web/mobile dashboard, an automatic licensed reference-data connector,
notifications, historical replay and walk-forward profitability validation remain
separate integration/product work. The included dashboard is terminal-based.

## Recovery

Keep the account paused after an audit/reconciliation error. Compare the checkpoint,
orders and simulated broker holdings; do not remove pending orders just because they
are old. No automatic migration guesses cash for a legacy position ledger. For a
fresh experiment use a new state directory; keep the previous directory for audit.
