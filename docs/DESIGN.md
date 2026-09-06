# Design: a Robinhood options trading agent

This document does three things: it states the rules you asked for, it works through
which of them survive contact with how options actually price, and it describes the
system that implements the result.

I could not read your `robinhood_options_trading_agent_design.md` — it lives on your
laptop and never reached this workspace. Everything below is written from your
description of the rules. Paste the file in and I will diff it against this properly.

---

## 1. The rules as you stated them

1. A fixed universe of 20 standard stocks.
2. Buy options with roughly a two-week horizon.
3. Sell at +10% on the position, or keep holding while it stays above +10%.
4. If it never reaches +10%, hold to expiry.
5. If it loses more than 50%, sell immediately — particularly with two or three days left.

Rules 3 and 5 are the interesting ones, and rule 4 is the dangerous one.

---

## 2. The arithmetic problem, first

Take a +10% profit target against a -50% stop. For the pair to break even:

```
p x 0.10 = (1 - p) x 0.50
p = 0.50 / 0.60 = 0.833
```

**You need to win 83% of your trades just to break even**, before spreads and fees.
Five winners pay for one loser, and no more. Run `optionsagent math` to see this
against other target/stop pairs.

Nothing about that is a detail to fix later. It is the whole strategy. A rule set that
takes small profits and accepts large losses is only viable at a hit rate that
essentially nobody sustains on directional option bets. This is the single change I
would insist on before anything else runs with money in it.

There are three ways out, and the design uses all three:

- **Let winners run past the target.** +10% arms a trailing stop rather than
  triggering a sale. Your own phrasing — "keep my options as long as I am in more
  than 10% profit" — already describes this; it just needs to be the primary
  behaviour rather than the alternative.
- **Cut losers before -50%.** A -50% stop on a long option is barely a stop at all,
  because by the time premium has halved the position usually needs an implausible
  move to recover. Tightening it near expiry, and adding a decay-based exit, shrinks
  the average loss.
- **Pick contracts where +10% is a small move.** Covered in section 4.

After those changes the realised distribution in simulation is a +27.3% average win
against a -38.7% average loss at a 61.6% hit rate, which needs 58.6% to break even
instead of 83%. Still demanding, but on the right side of possible.

---

## 3. The Greeks, and why each one is in the code

You said you were not sure what these are, so here is the working version. There is
no Greek called alpha — alpha is a portfolio term for return above a benchmark, an
outcome rather than an input. The four that matter:

| Greek | What it is | Why the agent filters on it |
| --- | --- | --- |
| **Delta** | How much the option moves per $1 move in the stock. 0.60 delta gains about 60 cents per dollar. Also roughly the chance of finishing in the money. | Determines how big a stock move your +10% target requires. This is the most important filter in the system. |
| **Gamma** | How fast delta itself changes. | Convexity is why a losing option can go from -20% to -60% between two polls. It explodes near expiry, which is why the agent refuses to hold into expiry week. |
| **Theta** | Dollars lost per day purely to time passing. Accelerates as expiry approaches. | Sets the clock you are trading against. It is the direct reason to buy 30-45 days out rather than 14. |
| **Vega** | Dollars gained per one point of implied volatility. | Buying an option is buying volatility whether you meant to or not. Buy it cheap, and never hold it through an earnings report. |

Concretely, on a $200 stock with 35% implied vol, a 0.30-delta 14-day call costs $2.56
and decays $17.78 per day — **7% of the contract's value every day**. To be up 10% two
days later the stock has to first cover 14% of decay. That contract is not a directional
bet over two weeks, it is a bet on an immediate, large move.

`optionsagent greeks --spot 200 --strike 210 --dte 14 --iv 0.35` prints these for any
contract you want to reason about.

---

## 4. Making +10% a reachable target

This is where most of the leverage in the design sits, and it is the part your rules
did not address at all.

Ten percent of premium is not a fixed amount of stock movement. It depends entirely on
which contract you buy. For every candidate the screener reprices the contract at the
end of the holding period and solves for the underlying price that gets it to +10%,
then divides that move by the move the market is already pricing over the same period
to get a **sigma requirement**.

On a $200 stock at 30-35% implied vol, holding two weeks:

| Contract | Price | Theta | Move needed for +10% | In sigmas |
| --- | --- | --- | --- | --- |
| 0.73-delta, 40 DTE | $14.33 | 0.7%/day | 1.81% | 0.31 |
| 0.54-delta, 40 DTE | $8.37 | 1.3%/day | 2.17% | 0.37 |
| 0.30-delta, 14 DTE | $2.56 | 7.0%/day | 5.41% | 0.79 |
| 0.13-delta, 10 DTE | $0.63 | 17.0%/day | 6.35% | 1.08 |

Same +10% rule, three times the required move between the top row and the bottom. The
cheap contracts look attractive because a small move is a large *percentage* gain, but
they have to overcome their own decay first, and the decay is enormous. The agent
rejects anything needing more than 1.25 sigma, which quietly does more work than every
other filter combined.

`optionsagent chain NVDA` shows this calculation for a live chain. It is a full
Black-Scholes reprice rather than a delta-gamma-theta approximation, because over a
two-week horizon the Greeks themselves move enough that the approximation is off by a
factor of two on exactly the short-dated contracts you most need to judge correctly.

The corollary is that **buying 14-DTE contracts is the wrong way to get a two-week
holding period**. Theta scales roughly with `1/sqrt(T)`, so the last two weeks are the
most expensive fortnight in the contract's life. Buy 30-45 DTE and exit after 14 days:
same directional window, materially less decay, and you never touch the gamma spike
near expiry.

---

## 5. The other thing that eats a 10% target: the spread

If a contract is bid $1.90 / ask $2.10, the mid is $2.00 and the spread is 10% of mid.
Buy at the ask and sell at the bid and you have lost 10% without the stock moving at
all. **Your profit target and your transaction cost are the same size.**

So:

- positions are marked at the **mid**, never at last trade (which is routinely stale
  and outside the current spread, and would fire your exits on phantom moves);
- contracts with a spread above 6% of mid are rejected outright;
- orders are **always limit orders**, starting near the mid and working up the spread
  over a few repricing attempts rather than crossing immediately;
- urgent exits (stop-loss, expiry guard) are the deliberate exception: they cross to
  the bid, because getting out matters more than two cents.

Robinhood charges no options commission, but regulatory fees still apply at roughly
$0.06 per contract per side. That is noise next to the spread.

---

## 6. Rule 4 is the one to delete

> "If you don't see 10% profit, wait till the expiry date."

This is the most expensive rule in the set. Holding a long option to expiry means
holding through the period of maximum decay and maximum gamma, and the terminal
outcome for an out-of-the-money contract is a total loss. Your -50% stop does not
protect you either: in the final days an option can gap from -40% to worthless
overnight, and a polling loop cannot sell into a move it never saw.

Replace it with three exits that all fire well before expiry:

- **Expiry guard**: close everything at 3 DTE regardless of P&L.
- **Near-expiry stop**: tighten from -50% to -30% once inside 6 days.
- **Decay stop**: if the contract is bleeding more than 4%/day and is not yet working,
  the remaining premium is better redeployed.

Your instinct in the last sentence of the brief — sell a big loser when there are only
two or three days left — is exactly right. The design just applies it earlier and
unconditionally rather than only to positions already down 50%.

---

## 7. The uncomfortable part: none of this is an edge

Your rules describe *exits*. Exits are risk management. They shape the distribution of
outcomes; they do not create a positive one.

Every long option starts underwater in two ways. Implied volatility has historically
run about 10-15% above subsequently realised volatility, because option sellers demand
payment for carrying tail risk — so on average you overpay for the contract. And you
pay the spread twice. Buying calls and puts with no view is reliably negative
expectancy, and no exit rule fixes that.

So something must decide **which direction to buy and whether to buy at all**. In this
implementation that lives in exactly one file, `strategy/signals.py`, deliberately
isolated so it can be replaced without touching anything else. The default is a plain
trend filter: it requires price on the correct side of a 30-day average *and* a
10-day move large enough relative to that symbol's own noise, otherwise it stands
aside. It is honest and hard to overfit. It is not a demonstrated edge, and I would not
claim otherwise.

**This is the part of the system worth your attention.** The rest is plumbing that can
be verified. Treat the signal as a slot to fill with something you can defend out of
sample. If you cannot find one, the correct conclusion is not to trade this strategy —
and the simulator's Kelly output will tell you so by returning zero.

---

## 8. What the simulation says

`optionsagent simulate` runs the real engine — same screener, same exits, same fill
model — across many independent synthetic markets, and compares your rules as stated
(`config/brief.yaml`) against the recommended set (`config/recommended.yaml`).

Three modelling choices make this a real test rather than a flattering one:

- **Implied vol is set 12% above realised vol**, so buying premium starts negative-EV
  the way it does in practice. Set that ratio to 1.0 and almost any option-buying
  strategy looks brilliant, which is how backtests lie.
- **The agent polls four times a day, not once.** With one look per day a +10% target
  "fills" at +50% because the mark gapped straight past it overnight. That single
  detail was inflating the take-profit results by roughly 2x before it was fixed.
- **Fills are not at the mid.** Orders concede part of the spread in both directions
  and sometimes miss entirely.

Output over 40 markets x 250 days (raw data in `simulation-results.json`):

| Metric | Recommended | Your rules as stated |
| --- | --- | --- |
| Trades per world | 231 | 1,462 |
| Win rate | 61.6% | 69.5% |
| Break-even win rate needed | 83.3% | 83.3% |
| Average win | +27.3% | +28.5% |
| Average loss | -38.7% | -61.3% |
| **Expectancy per trade** | **+1.97%** | **+1.11%** |
| Profit factor | 1.12 | 1.05 |
| Median account return | +6.5% | +38.2% |
| 5th percentile outcome | -22.4% | **-78.7%** |
| Average max drawdown | -20.8% | **-69.2%** |
| Kelly-implied position size | 7.2% | 3.9% |

Read the rows in that order, because the headline row is misleading on its own.

Your rules produce the *higher* win rate — taking profits at +10% wins often, exactly
as intended — and still barely clear break-even. Plug the realised numbers back into
the same formula from section 2: `61.3 / (28.5 + 61.3) = 68.3%` needed against a 69.5%
actual hit rate. **The entire margin is 1.2 percentage points of win rate.** Anything
that nudges the hit rate — a wider spread, a slower fill, a worse month — puts it
underwater.

The recommended set wins less often and earns nearly twice as much per trade, because
the trailing stop lets winners past +10% while the tighter near-expiry stop holds
losses to -38.7%. Its break-even requirement is `38.7 / (27.3 + 38.7) = 58.6%` against
a 61.6% hit rate: a three-point buffer instead of a one-point one.

**The median return column favours your rules, and I want to be straight about why.**
That config takes six times as many trades with 60% of equity deployed instead of 25%.
In a synthetic market with positive drift and a thin positive edge, leverage compounds
and wins. It is the same reason the 95th percentile is +522%. But look one row down:
a 5th-percentile outcome of **-78.7%** and an average maximum drawdown of **-69%**.
That is not a bad quarter, that is the account gone, and it happens in a *benign*
simulated market. Kelly, which sizes bets from the actual edge, says the recommended
rules justify roughly twice the position size — which is the formal way of saying your
config bets far more on a materially thinner edge.

**Read the absolute returns with real suspicion.** The synthetic market has a positive
drift and no macro regimes, so both columns look better than reality. The comparison
between columns is what carries information; the levels are not a forecast.

---

## 9. Architecture

```
CLI (typer)
  |
  +-- TradingEngine .................. the loop
        |
        +-- MarketDataProvider ....... synthetic | robinhood
        +-- Broker ................... paper | robinhood
        +-- Portfolio ................ ledger, JSON state, trade log
        +-- RiskManager .............. limits, PDT, kill switch
        +-- Strategy
              +-- MomentumSignal ..... direction (the replaceable part)
              +-- EntryScreener ...... which contract
              +-- size_position ...... how many
              +-- evaluate_exit ...... when to get out
```

Each loop, in this order and never another:

1. refresh equity, register the session with the risk manager;
2. settle any expired contracts at intrinsic;
3. reconcile against the broker's actual positions;
4. **evaluate exits on everything held**;
5. scan for entries, if risk limits allow.

Exits run before entries so that a stall or an exception while scanning chains can
never delay closing a loser. Risk checks can block an entry; nothing can block an exit.

Two accounting details that are easy to get wrong and matter more than they look.
Expired contracts are settled at intrinsic *before* reconciliation, and positions that
vanish from the broker are booked as closed trades rather than silently dropped —
otherwise every trade you closed by hand in the app disappears from the log and your
recorded win rate quietly inflates. Position state is written to disk after every
change, so a restart resumes with trailing stops and high-water marks intact instead of
resetting every position's peak to zero.

---

## 10. Risk controls

| Control | Default | Why |
| --- | --- | --- |
| Risk per trade | 2% of equity | With a -50% stop this is 4% of equity in premium per position. |
| Max positions | 6, one per underlying | Concentration limit. |
| Max premium deployed | 25% of equity | A full book stopping out together costs ~12%. |
| Daily loss limit | 5% | Halts new entries for the session. |
| Drawdown halt | 20% | Stops the agent pending human review. |
| Consecutive losses | 5 | Usually means the signal has stopped working. |
| PDT protection | on below $25k | See below. |
| Kill switch | `touch state/KILL` | Blocks entries from any shell, no restart. |

**Pattern day trader rule.** Below $25,000 in equity, FINRA allows three day trades per
rolling five business days. A +10% target on a liquid option is routinely hit the same
session, so without protection the agent spends its day trades taking small profits and
then cannot cut a loser on the day it needs to. It reserves the last one for stop-losses.

**Position sizing is a real constraint on a small account.** At 2% risk per trade with a
-50% stop, a $25k account can commit about $1,000 of premium per position. A 0.60-delta
contract on a $500 stock costs $2,000+, so most of a mega-cap universe is simply
unaffordable. The engine walks down the ranked candidate list to find something that
fits, but you should know the constraint is binding. Your options are to accept a
narrower effective universe, raise risk per trade (and accept the drawdown), or use
vertical spreads to cut per-position cost — the last being the genuinely correct answer,
and the most natural next feature.

**What none of this protects against.** Every stop here is a *trigger*, not a
guaranteed fill price. The agent looks at the market, decides to sell, and sends an
order; the market does not wait. In paper runs I have watched a position go from +26%
to -22% between two observations, blowing straight through a trailing stop that should
have exited around +16%. Overnight gaps, halted stocks, and fast moves all do this, and
options are convex enough to make it routine rather than exceptional. A -50% stop
does not cap your loss at 50%; it caps the loss at "whatever the contract is worth the
next time we look". Size positions on the assumption that a stop can be missed, which
is exactly why the premium caps in the table above exist alongside the stop.

---

## 11. Robinhood specifically

**Use the official Trading MCP, not `robin_stocks`.**

Robinhood shipped an official Model Context Protocol server at
`https://agent.robinhood.com/mcp/trading` in May 2026, and agentic **options** trading
went live for all US customers in early July 2026. This is a supported, documented
integration path and it is strictly better than screen-scraping the mobile endpoints.

The options tool surface is exactly what this agent needs:

| Tool | Used for |
| --- | --- |
| `get_option_chains` | Load the chain for a symbol |
| `get_option_instruments` | Filter contracts by expiry, strike, type |
| `get_option_quotes` | Real-time quotes and Greeks |
| `get_option_historicals` | OHLC bars per contract |
| `get_option_positions` | Open and closed positions, for reconciliation |
| `get_option_orders` | Order history, for duplicate detection |
| `review_option_order` | **Simulate an order and get pre-trade alerts** |
| `place_option_order` | Submit |
| `cancel_option_order` | Cancel |

Properties that matter for an unattended agent:

- **OAuth 2.1 + PKCE.** The agent never sees your password, and there is no TOTP
  scraping. Tokens are scoped and revocable from the Robinhood app.
- **Trading is confined to a separate Agentic account** that you fund deliberately.
  Every other Robinhood account is readable but not tradable. That is a stronger
  containment boundary than any kill switch this code could implement, and it is the
  single best reason to prefer this over the unofficial API.
- **`review_option_order` is a first-class dry run.** It returns pre-trade alerts from
  the broker itself, so the "propose then approve" mode below is native rather than
  simulated.
- **Robinhood does not supervise the agent.** There are no server-side risk limits.
  Position sizing, symbol allow-lists, and daily caps are entirely the client's job,
  which is what everything in section 10 exists to do.

Still true, and still constraints:

- **Quotes are request/response, not a stream.** Polling 20 underlyings plus open
  positions costs real time. The 60-second default loop is deliberate, and it means
  the agent is not reacting intraday to a fast move.
- **You need options approval level 2** for long calls and puts. `get_option_level_upgrade_info`
  returns the link to apply.
- **PDT still applies.** An Agentic account is an ordinary self-directed brokerage
  account, so the pattern-day-trader rule binds below $25k exactly as described above.
- **You are responsible for the trades your agent places.** Robinhood's terms are
  explicit that liability does not move to the agent or the model provider.

The agent computes its own Greeks from the mid price even when the API supplies them,
so paper and live remain numerically identical and a null or stale field cannot silently
change position sizing.

> An earlier revision of this document claimed no supported retail options API existed
> and recommended a different broker. That was wrong. `brokers/robinhood.py` and
> `marketdata/robinhood.py` still contain the unofficial `robin_stocks` path; prefer
> the `*_mcp.py` adapters.

---

## 12. Verdict on your design

**Good:**

- A fixed, liquid universe. Exactly right, and more important than it sounds: liquidity
  is what makes a 10% target reachable at all.
- Mechanical exits defined in advance. This is the part most people skip.
- A defined holding period. Prevents the classic slow bleed into expiry.
- "Keep it while I am up more than 10%." This instinct is correct and is what the
  trailing stop implements.
- Cutting a big loser when only days remain. Also correct.

**Needs to change:**

| Your rule | Change | Why |
| --- | --- | --- |
| Sell at +10% | Arm a trailing stop at +10% with a floor there | +10%/-50% needs an 83% win rate; capping winners guarantees you never clear it |
| Two-week contracts | Buy 30-45 DTE, exit after 14 days | Same window, much less decay, no gamma spike |
| Hold to expiry if flat | Expiry guard at 3 DTE, plus a decay stop | Holding to expiry is where total losses come from |
| -50% stop | Keep it, but tighten to -30% near expiry | -50% on a long option is barely a stop |
| (not specified) | Delta 0.45-0.70 and a sigma-requirement filter | Determines whether +10% is a normal fluctuation or an outlier |
| (not specified) | Spread cap, mid-price marking, limit orders | Otherwise costs are the same size as the target |
| (not specified) | IV rank cap and earnings avoidance | IV crush loses money on directionally correct trades |
| (not specified) | Sizing from the stop, portfolio caps, PDT protection | Turns a rule set into something that survives a bad month |
| (not specified) | **An explicit directional signal** | Exits are not an edge; something has to decide what to buy |

**The gap that matters most** is the last one. Your design specifies when to get out in
some detail and does not specify what to buy or why. That is backwards relative to where
the money is: exits shape the distribution, entries determine whether it has a positive
mean at all.

---

## 13. How to proceed

1. `optionsagent math` and `optionsagent explain` — the arithmetic and the Greeks.
2. `optionsagent simulate --config config/recommended.yaml --compare config/brief.yaml`
   — see the two rule sets side by side, and change the parameters you disagree with.
3. `optionsagent run --loops 50 --advance-days 1` — watch the agent trade on paper.
4. Replace `MomentumSignal` with something you can defend out of sample. Nothing before
   this step is worth risking money on.
5. Only then connect Robinhood, leave `dry_run` on, and compare the orders it *would*
   have sent against what you would have done yourself for a full options cycle.
6. Go live at the smallest size that is not a rounding error, with the kill switch
   within reach.

Steps 1 through 3 work today. Step 4 is the actual project.
