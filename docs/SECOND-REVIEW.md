# Second implementation review

Reviewed 2026-09-06, starting from `434c05c`. Scope: all source modules, configurations,
CLI commands, tests, simulation/sweep tooling and CI. This review includes code
changes; it does not include authenticated Robinhood calls or real orders.

**Verdict: the core design is implemented for a local synthetic paper prototype.
It is not yet the complete live-data paper MVP or a live-trading release.**
Passing tests establishes the tested mechanics, not profitability or broker compatibility.

## Design coverage

| Requirement | Current implementation | Remaining boundary |
|---|---|---|
| Simulation and operating modes | `engine.py`, `cli.py`: OFF/SCAN/PAPER enforced; paper orders isolated from market-data provider | Live modes disabled |
| Contract selection | `config.py`, `strategy/entry.py`: 12–18 DTE, ITM calls/puts, absolute delta .55–.70; spread, volume, OI, IV, earnings, theta/gamma filters | Greeks are a Black–Scholes approximation; real payloads/adjusted contracts need validation |
| No force trade | `strategy/signals.py`, `engine.py`: neutral/missing data/unaffordable contracts skip; one holding per symbol prevents averaging down | Momentum signal has no demonstrated predictive edge |
| +10% profit lock | `strategy/exits.py`: bid-based activation, persisted peak, trailing floor and direction-loss exit | Default trail is 40% giveback of gain, not a 5% price trail; both modes supported; fills can realize below trigger |
| Loss/expiry | `strategy/exits.py`: hard loss, tighter near-expiry loss, mandatory DTE/time/theta/event exits | Real-quote paper expiry requires verified settlement; synthetic cash settlement is only a modeling convention |
| Risk/limits | `config.py`, `risk.py`, `strategy/sizing.py`: $1,000 entry budget, fees, exposure, cash, quantity, daily loss/drawdown and operator pause | No sector/correlation risk model; day-trade rule is a configurable simulation guard, not account permission verification |
| Order safety | `orders.py`, `engine.py`: durable intent before submission, no TTL release, partial quantities, ambiguity blocks entries | Real cancel/replace and execution reconciliation are not complete |
| Recovery | `runtime.py`: transactional paper cash/holdings/history/risk/market checkpoints; preserve newer journal ambiguity; single writer | Ambiguous recovery still requires investigation; no automatic real-account repair |
| Robinhood MCP | `mcp/client.py`, `marketdata/robinhood_mcp.py`: read/review allowlist, capability discovery, bounded response parsing, timestamp validation | OAuth refresh/account identity and actual tool schemas still unverified |
| Reference feed | `marketdata/reference.py`: strict timestamped daily bars/IV/events/session snapshot contract; unknown fails closed | Automatic licensed-feed adapter and full calendar/bar provenance checks still needed |
| Dashboard and audit | `cli.py`, `runtime.py`: terminal positions/status/pause/health, SQLite decisions and errors | Web/mobile dashboard, proactive alerts, full per-filter rejection analytics remain absent |
| Tests | Regression/unit/integration tests plus CI | No authenticated broker contract tests, historical replay, walk-forward or OOS validation |

All paths above are under `src/optionsagent/`. Active profile:
[`config/recommended.yaml`](../config/recommended.yaml).

## Bugs and risks corrected in this pass

1. **Unmonitored holdings could coexist with new entries.** Missing/stale/mismatched
   held quotes now block entries; failures are reported while other holdings continue
   to receive exit checks. Quantity mismatches never submit an unverified close.
2. **An expired holding could be closed with fabricated economics.** Only matched
   synthetic paper holdings can use modeled intrinsic settlement. Real-quote paper
   holdings require verified settlement information and latch a halt instead.
3. **Restart could discard newer order evidence.** Restore preserves journal records
   newer than a checkpoint, including a retry of an existing intent. Such outcomes
   become unresolved instead of being replayed. Order journal writes are flushed to disk.
4. **Invalid configuration and data could pass comparisons.** Reject nonfinite values,
   malformed risk/session/quantity settings, unknown config keys, ambiguous reference
   metadata, invalid Greeks and missing/future timestamps. Unknown IV never defaults
   to a neutral rank. Legacy direct-broker mutations are disabled.
5. **Limits and budgets had edge cases.** Paper urgent exits now respect their limit;
   entry sizing includes entry fees; quote identity is checked. Price concessions are
   capped as a percentage of midpoint, with buy/sell rounding in the protective direction.
6. **Simulation results had reproducibility/clock problems.** Stable history seeds,
   session-consistent dates, corrected calendar-time diffusion and intraday drawdown
   measurement replace mixed clock assumptions. Old sweep statistics are obsolete.
7. **MCP transport could wait for a stream to close or select an unrelated event.**
   It stops at the matching response ID, handles multiline events, follows tool-list
   pagination and sends the negotiated protocol header. Unknown tools are denied;
   tokens are excluded from object representations. Session loss resets initialization
   for the next read attempt; it never retries an order.

Tests are in [`test_second_review.py`](../tests/test_second_review.py) and the
existing safety, order, engine and strategy suites.

## Prioritized completion plan

1. **Finish the live-data paper MVP:** connect an authorized reference feed; verify
   real MCP tool schemas with sanitized recorded fixtures; confirm quote identity,
   timestamps, completed sessions, daily bars, IV history and earnings. Exercise
   outages, restarts and multi-session paper operation. Add verified paper expiry
   settlement and a reviewed recovery workflow. Keep real execution disabled.
2. **Finish operator visibility:** stale-run/quote and exit-failure alerts, clear
   order/recovery health and an authenticated local/private web dashboard if a browser
   UI is part of the MVP. Terminal status and pause/resume suffice for supervised local use.
3. **Validate the strategy:** historical option bid/ask replay, fees, missed/delayed
   fills, market regimes and held-out walk-forward evaluation. The earlier profit
   factor range 0.87–1.28 is not evidence of a durable edge.
4. **Only then consider live execution:** verified account/permissions, OAuth refresh,
   explicit broker schemas, instrument/tick rules, execution IDs, partial/late fills,
   cancellation acknowledgements, buying-power reconciliation, exercise/share delivery
   and crash recovery. Enablement requires a separate reviewed change.

## Verification

- 150 automated tests pass locally; Ruff and whitespace checks pass.
- Tests include safety modes, costs, trailing exits, stale data, partial fills,
  duplicate attempts, two-writer exclusion, restart and crash-window recovery,
  configuration validation and transport failure cases.
- No live order was placed. Network integration and strategy returns were not certified.
