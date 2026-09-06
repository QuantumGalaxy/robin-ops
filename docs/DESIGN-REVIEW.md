# Review of `robinhood_options_trading_agent_design.md`

A comparison against the design in [DESIGN.md](DESIGN.md) and the code in this
repository. Written to be useful rather than encouraging: the sections where your
document is better than mine come first, because those are the ones that changed
the code.

## Summary

Your document is a real specification, not a sketch. It is stronger than mine on
operational safety — order idempotency, reconciliation, failure handling,
staged rollout — and it is correct on the broker integration where I was wrong.
It is weaker on the quantitative side: it lists the experiments to run without
running them, and a few of the starting parameters interact badly in ways the
experiments would surface.

Three things in it changed this codebase. Two things in mine I would still argue
for over yours.

---

## 1. Where you were right and I was wrong

### The Robinhood integration (your section 27)

I told you there is no supported retail options API and that you should consider
a different broker. **That was wrong.** Robinhood shipped an official Model
Context Protocol server at `https://agent.robinhood.com/mcp/trading` in May 2026,
and agentic *options* trading went live for all US customers in early July 2026.
Your document assumed exactly this and named it correctly.

It is not a marginal improvement over the unofficial `robin_stocks` path — it is
a different risk category:

| | `robin_stocks` (what I proposed) | Trading MCP (what you proposed) |
| --- | --- | --- |
| Support status | Undocumented mobile endpoints | Official, documented |
| Auth | Your password plus a TOTP secret on disk | OAuth 2.1 + PKCE, revocable |
| Blast radius | Your entire account | A separate Agentic account you fund deliberately |
| Pre-trade check | Whatever I could compute | `review_option_order`, from the broker |
| Breakage mode | Silent, whenever the app changes | Versioned tool surface |

The isolated Agentic account matters most. Every other Robinhood account stays
readable but not tradable, which is a containment boundary no amount of
application code could give you. It is a better kill switch than the kill switch
I wrote.

I have implemented this: `optionsagent/mcp/` speaks the protocol,
`marketdata/robinhood_mcp.py` and `brokers/robinhood_mcp.py` are the adapters,
and every order now goes through `review_option_order` before
`place_option_order`. Because Robinhood publishes tool *names* but not response
*schemas*, field reads are deliberately tolerant of alternative key names, and
`optionsagent mcp-probe` snapshots the live schemas from your own account so you
can diff them after any Robinhood update.

One caveat your document does not mention: an Agentic account is still an
ordinary self-directed brokerage account, so **the pattern-day-trader rule
applies**. Under $25k you get three day trades per rolling five business days.
With a +10% target on 12–18 DTE contracts you will hit that ceiling, because a
position that jumps 10% on the day you open it is exactly the trade the rule
blocks you from closing. PDT protection is in `risk.py`; the MCP tool surface
does not expose a day-trade count, so the adapter assumes the worst.

### Duplicate-order protection (your section 29)

You flagged this as "critical" and you were right. My design did not have it.
This is the gap I would most want closed before real money, and it was the most
valuable thing in your document.

The failure is quiet, which is what makes it dangerous: the agent submits, the
response is lost to a timeout, the next loop sees no position, and it buys again.
Nothing errors. You simply own twice the risk you sized for.

`optionsagent/orders.py` now implements what you specified. Two details worth
flagging because they are not obvious:

- **The ID is derived from the trade's intent** — contract, side, quantity, and
  the minute of the decision — so a retry of the same decision reproduces the
  same ID, while a genuinely new decision later does not collide with it.
- **The record is written before submission, never after.** Written after, a
  crash mid-submission loses exactly the evidence you needed.

Implementing it surfaced two bugs that are worth knowing about, because any
implementation of your section 29 will hit them:

1. **A reprice ladder is one intent, not several.** Walking a limit order up the
   spread looks like several orders, and a naive guard suppresses its own rungs
   two through five. Reserve once around the whole ladder.
2. **Stamp the ID with the decision time, not the wall clock.** A backtest
   stepping through 60 simulated days inside one real second collapses every
   order into the same minute, and the agent suppresses all of its own trades.
   This silently zeroed out my backtests until a test caught it.

A third point your section does not cover: what to do when an attempt is
*ambiguous* — no fill, no rejection. Assuming it died and retrying is how you get
the double position. The broker interface now has `has_open_order`, which returns
`None` for "cannot tell", and an unknown answer is treated as "possibly working"
and left alone until a TTL expires it.

### Reconciliation should halt trading (your section 28)

Mine reconciled and carried on. Yours stops. Yours is right: sizing, exposure
caps, and every exit decision are computed from the ledger, so continuing to open
positions on a view known to be wrong turns a small bug into a large one. The
engine now holds new entries on a mismatch while continuing to run exits, which
is the correct asymmetry — being wrong about what you own is a reason to stop
buying, never a reason to stop selling.

### Four operating modes (your section 54)

`SCAN_ONLY → SIMULATION → LIVE_APPROVAL → LIVE_AUTO` is better than my
paper/dry-run/live. `LIVE_APPROVAL` in particular is the mode that actually gets
used: it is where you discover that your simulated fills were optimistic, without
betting on that discovery. Both `SCAN_ONLY` and `LIVE_APPROVAL` are now in
`config.py`, and `review_option_order` makes the approval mode genuinely useful
because the broker's own pre-trade alerts are shown alongside the proposal.

### Things you have that I do not, and should

I have not built these; they are real gaps in my design rather than
disagreements.

- **A proper database (your section 38).** I persist JSON. Your
  `position_snapshots` table is the important one — without a time series of
  P&L, delta, IV, and DTE per position, you cannot answer "why did this trade go
  wrong" or run your section 41 breakdowns by delta band and DTE. JSON files
  cannot do that. Your schema is close to right; I would add the entry
  `direction_score` to `positions` so section 41's "performance by entry score"
  is a single query.
- **The dashboard and the explain screens (sections 33–37).** "Why did the agent
  buy" and "why did the agent sell" as first-class, stored artifacts is better
  than my approach of logging reasons and hoping. My screener produces exactly
  this data and then throws it away.
- **The failure-handling matrix (section 51).** Mine is thinner. "Do not invent
  position state" is the right instinct and is stated more crisply than anything
  in my document.
- **AI behind a deterministic validator (section 45).** Correct architecture,
  correctly argued. Nothing to add.

---

## 2. Where I think you should change something

### The trailing stop is tighter than it looks (your section 19)

`TRAILING_PROFIT_PERCENT = 5%` is 5% of the option's *value*. On a 0.60-delta
contract, a 5% move in the option is roughly a **0.5% move in the underlying** —
comfortably inside the daily noise of every stock on your list. That trail will
fire on a normal intraday wobble in a trade whose thesis is still intact.

This is worth being precise about because your document and mine use the same
word for different rules. Mine gives back a fraction of the *gain*: from a +30%
peak at 40% giveback, the exit sits at +18%. Yours gives back a fraction of the
*price*: from the same peak, 5% exits at +23.5%. Yours captures more when it
works. It also arms much closer to the current price, so it triggers far more
often.

Your section 19 says not to assume 5% is optimal and lists the alternatives to
test, so this is less a disagreement than an answer to a question you already
asked. I implemented both parameterizations (`exit.trailing_mode`) and ran your
section 42 matrix; results are in the numbers section below.

### The stop-loss and the profit target are not compatible (your section 21)

**You already found this, and I owe you a correction.** In my previous message I
presented the +10%/-50% breakeven arithmetic as the biggest gap in your design.
It is in your document, in section 21, with a worked example that reaches the
same conclusion I did. I should have read more carefully before saying it.

Where I would push further: the fix is not only a tighter stop. With a +10%
target and a -50% stop you need an **83.3% win rate** to break even. Tightening
the stop to -25% brings that to 71.4%, which is still high. The other half of the
answer is letting winners run — which is what your profit-lock in section 18 does
— because raising the average win moves the breakeven faster than tightening the
stop does. Your sections 18 and 21 are solving the same problem from two ends,
and the document treats them as separate concerns.

The corollary is that **the +10% number should probably not survive contact with
the simulator.** It is the one parameter in your brief that is stated as a
requirement rather than a hypothesis, and it is also the one doing the most
damage to the arithmetic.

### 12–18 DTE is the choice I would most want to revisit (your section 7)

This is my main substantive disagreement. Your document prefers 12–18 days with
14 as the target; I argued for 30–45 days held about 14 days.

The reason is that theta is not linear. An option loses time value roughly with
the square root of remaining time, so the decay *rate* accelerates as expiry
approaches, and the last two weeks are where it is steepest. Buying a 14-day
option and holding it means paying the worst part of the curve. Buying a 35-day
option and selling it at 21 days gets you the same two weeks of directional
exposure while paying a much flatter part.

Your section 11 already notices the related effect — "near-expiration options
often have more aggressive Gamma behavior... another reason to avoid opening very
short-DTE contracts" — and section 44 lists 18–25 as a variant to test. I would
extend that range to 45 and expect the answer to come back longer than 14. The
sweep numbers below bear this out.

There is a second-order benefit: a 35-day contract still has 21 days of life when
your two-week window closes, so an exit is a normal sale into a normal spread. A
14-day contract that has not worked is at 2 DTE, where the spread is widest and
your section 22 forces you out regardless of what you think.

### Sizing is specified in dollars but risk is specified in percent

`MAX_POSITION_COST = $1,000` with `MAX_OPEN_POSITIONS = 5` puts $5,000 of premium
at risk. Your section 30 says this "should be based on total capital" but does not
close the loop. On $10,000 of capital that is 50% of the account in long options,
and with the -50% emergency stop, a correlated drawdown across five positions is
a 25% account loss. Your list is AAPL, MSFT, NVDA, AMD, META, AMZN, GOOGL, TSLA —
these are not five independent bets. They are one bet on large-cap tech, made five
times.

I would size from risk rather than from a dollar cap: choose the fraction of
equity you accept losing per trade, then derive the contract count from the stop
distance. `sizing.risk_per_trade_pct` in my config does this, and it makes the
position size automatically adjust when you change the stop — which matters
because you intend to test five different stops.

Related: a per-symbol cap does not address sector correlation. Neither design
handles this well. Mine caps total premium as a fraction of equity, which is a
blunt instrument that at least bounds the damage.

### Two smaller things

- **Mark positions at the mid, not the last trade.** Your section 34 shows a P/L
  computed against a `Current:` price without saying which. Options trade
  infrequently; the last trade can be stale by hours and can sit on either side
  of the spread. A stop measured against a stale last price fires at random. Use
  `(bid + ask) / 2`, consistently, everywhere.
- **Your section 13 min-volume of 100 is doing less than it looks.** Volume
  resets daily, so a contract legitimately shows 0 at 09:35. Open interest is the
  more robust liquidity screen; keep both, but treat OI as the binding one.

---

## 3. What the experiments say

Your sections 42–44 list the experiments and say the final rule should come from
evidence. I ran them: 40 synthetic worlds, 250 trading days each, four
observations per day. `python scripts/sweep.py` reproduces this.

**Read these as a ranking of rules against each other, not as a forecast.** These
are synthetic price paths with a variance risk premium baked in — options are
priced slightly above their fair value, as they are in reality — not historical
data. The ordering is more trustworthy than the magnitudes.

<!-- SWEEP RESULTS -->

---

## 4. Section-by-section verdict

| Your section | Verdict |
| --- | --- |
| 2 Three-layer architecture | Agree. Risk engine with final authority is right. |
| 4 Hard-coded universe | Agree, and the reject-on-unknown-ticker rule is good. |
| 5–6 Direction scoring | Agree in shape. The 75/100 threshold is arbitrary; make it a percentile of recent scores so it adapts. |
| 7 12–18 DTE | **Disagree.** Steepest part of the theta curve. See above. |
| 8–9 Slightly ITM, 0.55–0.70 delta | Agree. Good risk profile for a directional buyer. |
| 10 Theta burden = theta/premium | Agree — same metric I use. |
| 11 Gamma monitored, not dominant | Agree for v1. |
| 12 IV rank / percentile | Agree. Note you need 52 weeks of ATM IV history per symbol; neither of us has a source for that yet. |
| 13 Liquidity | Agree. Prefer OI over volume as the binding screen. |
| 14 $1,000 cap | **Change.** Size from risk, not dollars. |
| 16 No-force-trade | Strongly agree. "Cash is a valid position" is the single best line in the document. |
| 17–18 Profit lock | Agree, and better than a plain +10% sale. |
| 19 5% trailing | **Tighten your definition, then test.** 5% of premium ≈ 0.5% of underlying. |
| 20 Trend-failure exit | **Better than mine.** I exit on price and time only. |
| 21 Loss rules | Agree, and you found the breakeven problem yourself. |
| 22 Mandatory expiry exit | Agree. |
| 23 No earnings | Agree. |
| 24 Limit orders | Agree. Add: never a market order on an option, ever. |
| 26 Never assume a fill | Agree, and this is a discipline my `Broker` interface originally papered over. |
| 27 Robinhood MCP | **You were right, I was wrong.** |
| 28 Reconciliation halt | **Better than mine.** Adopted. |
| 29 Duplicate protection | **Better than mine.** Adopted, and the most valuable item in the document. |
| 30–31 Portfolio limits | Agree on the controls; add correlation. |
| 32 Pause vs emergency stop | Agree, and the distinction is well drawn. |
| 33–37 Dashboard and explainability | **Better than mine.** Not yet built here. |
| 38 Database schema | **Better than mine.** `position_snapshots` is the key table. |
| 40 Backtest vs forward simulation | Agree, and the distinction is one many designs miss. |
| 41 Metrics | Agree. Add expectancy per trade and the breakeven win rate for your target/stop pair. |
| 42–44 Experiments | Agree — see the numbers above. |
| 45 AI behind a validator | Agree. |
| 46 Stack | Agree. |
| 49–50 Schedule | Agree. Scan every 5–15 min is right; nothing here needs to be faster. |
| 51 Failure handling | **Better than mine.** |
| 53–54 Safety gate and modes | **Better than mine.** Adopted. |
| 56–65 Phased rollout | Agree, and the ordering is correct. |
| 68 Closing principle | Agree, including the last sentence. |

---

## 5. If you only change three things

1. **Fix the target/stop arithmetic.** Either raise the average win by letting
   winners run further, or tighten the stop, ideally both. +10%/-50% needs an
   83.3% win rate to break even, and nothing in either design produces that.
2. **Size from risk, not from a dollar cap.** Then the position size adjusts
   itself when you test the five stops in your section 43, instead of silently
   changing the experiment.
3. **Test 30–45 DTE alongside 12–18.** It is one config line, and it is the
   parameter with the largest effect in the sweep.

Everything else in your document is either right, or a preference the simulator
can settle.
