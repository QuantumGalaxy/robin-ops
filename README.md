# optionsagent

A rule-based options trading agent: it screens a fixed universe of liquid large caps,
buys single long calls or puts that pass a Greeks-aware filter, and manages every
position with automated exits — take profit, trailing stop, hard stop, expiry guard,
and time stop.

It runs end to end with no brokerage account. Paper trading against a synthetic
options market is the default; Robinhood is an optional adapter that stays in dry-run
mode until you explicitly turn it off.

> **This is not financial advice, and the strategy is not proven profitable.**
> Long options can and regularly do lose 100% of the premium paid. Read
> [`docs/DESIGN.md`](docs/DESIGN.md) before risking money — in particular the section
> on why a +10% / -50% rule pair needs an 83% win rate just to break even, and the one
> on why exit rules are not an edge.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
source .venv/bin/activate

optionsagent explain                    # what delta/gamma/theta/vega do here
optionsagent math                       # the break-even arithmetic of the exit rules
optionsagent chain NVDA --direction call  # screen a chain and rank the candidates
optionsagent run --loops 5              # paper-trade five loops
optionsagent status                     # open positions and closed-trade stats
```

Compare the rules as originally specified against the recommended set:

```bash
optionsagent simulate \
  --config config/recommended.yaml \
  --compare config/brief.yaml \
  --worlds 30 --days 180
```

## What it does

Each loop, in this order:

1. refreshes account equity and registers the session with the risk manager;
2. reconciles its ledger against the broker's actual positions;
3. evaluates exits on everything held — **always before** looking for new trades;
4. scans for entries, if risk limits allow.

Entry requires a directional signal and a contract that survives filters on delta,
theta, gamma, bid-ask spread, open interest, IV rank, and — the important one — the
size of the underlying move needed to reach the profit target. Exits follow the rules
in `config/recommended.yaml`, evaluated most-protective-first.

## Layout

| Path | What lives there |
| --- | --- |
| `src/optionsagent/greeks.py` | Black-Scholes pricing, Greeks, implied-vol solver |
| `src/optionsagent/strategy/exits.py` | The exit rule engine |
| `src/optionsagent/strategy/entry.py` | Contract screening and ranking |
| `src/optionsagent/strategy/signals.py` | Directional signal (the replaceable part) |
| `src/optionsagent/strategy/sizing.py` | Position sizing derived from the stop |
| `src/optionsagent/risk.py` | Daily loss limit, drawdown halt, PDT protection, kill switch |
| `src/optionsagent/engine.py` | The agent loop |
| `src/optionsagent/simulate.py` | Monte Carlo evaluation of a rule set |
| `src/optionsagent/marketdata/` | Synthetic and Robinhood data providers |
| `src/optionsagent/brokers/` | Paper and Robinhood order routing |
| `config/` | `recommended.yaml` and `brief.yaml` for head-to-head runs |
| `docs/DESIGN.md` | The design rationale, and what to change before trading it |

## Connecting Robinhood

Robinhood publishes no supported retail options API. The adapter drives the private
endpoints used by the mobile app through [`robin_stocks`](https://github.com/jmfernandes/robin_stocks),
which is against the spirit of their terms of service and breaks without notice. Read
the header of `src/optionsagent/marketdata/robinhood.py` before going further.

```bash
uv pip install -e ".[robinhood]"
export ROBINHOOD_USERNAME=... ROBINHOOD_PASSWORD=... ROBINHOOD_MFA_SECRET=...
```

Set `broker.kind: robinhood` in your config. Orders are logged and **not** submitted
until you pass `--live`, which prompts for confirmation.

## Stopping it

```bash
touch state/KILL     # blocks all new entries immediately; exits keep running
```

The kill switch is deliberately a file, so you can stop the agent from any shell
without an API call or a restart.

## Safety properties

- Long options only. There is no code path that opens a short position, so the worst
  case on any single trade is the premium paid.
- Limit orders only. Nothing sends a market order.
- Risk checks can block entries but can never block an exit.
- Position state is persisted after every change, so a restart resumes with trailing
  stops and high-water marks intact.
- The loop never dies on a transient data error; open positions still get managed.

## Tests

```bash
pytest -q
ruff check .
```
