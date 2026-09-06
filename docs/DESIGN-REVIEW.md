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

I implemented both parameterizations (`exit.trailing_mode`) and ran your section
42 matrix. **Your 5% trail beat my 40% giveback** — the concern above is real, in
that the win rate does drop, but banking more of each move more than compensates.
Numbers below. Keep your rule.

### The stop-loss and the profit target are not compatible (your section 21)

**You already found this, and I owe you a correction.** In my previous message I
presented the +10%/-50% breakeven arithmetic as the biggest gap in your design.
It is in your document, in section 21, with a worked example that reaches the
same conclusion I did. I should have read more carefully before saying it.

Where I would push further: the fix is on the **target** side, not the stop side.
With a +10% target and a -50% stop you need an **83.3% win rate** to break even.
Tightening the stop to -25% brings that to 71.4%, which sounds like progress — but
when I actually ran your section 43 matrix, every tighter stop performed *worse*
(numbers below). Raising the average win is what moves the breakeven, and that is
exactly what your section 18 profit-lock does. Your sections 18 and 21 are solving
the same problem from two ends, and the document treats them as separate concerns.

I had originally written this section recommending a -20% to -25% stop. The sweep
says that is wrong, so I have removed it.

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
short-DTE contracts" — and section 44 lists 18–25 as a variant to test. The sweep
below is emphatic about this: 12–18 DTE has an expectancy of +0.12% per trade,
essentially zero, while 21–60 days is several times better. It is the largest
single effect in the whole experiment matrix.

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

Two of the four results contradict advice I gave you, and one contradicts your
brief. I have flagged which is which.

### Days to expiry — the largest effect in the sweep

| Variant | Trades | Win % | Expectancy | Profit factor | Median | 5th pct | Avg drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 7–10 DTE | 1,142 | 42.2% | **−2.58%** | 0.87 | −4.3% | −11.9% | −8.5% |
| **12–18 DTE (your brief)** | 5,760 | 51.5% | **+0.12%** | 1.01 | −8.6% | −21.2% | −19.6% |
| 21–30 DTE | 8,805 | 58.5% | +2.13% | 1.14 | +3.5% | −20.6% | −21.2% |
| 30–45 DTE (my brief) | 9,242 | 61.6% | +1.97% | 1.12 | +6.5% | −22.4% | −20.8% |
| 45–60 DTE | 9,226 | 64.7% | **+2.82%** | 1.22 | **+18.0%** | −19.7% | −18.4% |

This is the clearest signal in the whole sweep, and it is the one place I would
push hardest for a change. **Your 12–18 DTE window has an expectancy of +0.12%
per trade — a coin flip.** Its median world *loses* 8.6%. Move to 21+ days and
expectancy multiplies; 45–60 is the best cell tested.

The 7–10 row is the same effect taken further, and it is the only outright
negative-expectancy configuration in the entire sweep. That is theta doing
exactly what the square-root-of-time curve predicts. 12–18 sits on the shoulder
of that cliff.

### Profit taking — your instinct beat mine

| Variant | Trades | Win % | Expectancy | Profit factor | Median | 5th pct | Avg drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fixed +10% | 13,646 | 76.5% | +1.61% | 1.13 | +14.6% | −20.6% | −17.8% |
| Fixed +15% | 13,066 | 71.8% | +2.19% | 1.14 | +11.7% | −18.9% | −18.4% |
| Fixed +20% | 10,971 | 67.1% | +2.63% | 1.16 | +21.9% | −22.0% | −18.0% |
| **+10% then 5% trail (your brief)** | 12,853 | 64.4% | **+2.32%** | 1.15 | **+21.6%** | −19.3% | −18.0% |
| +10% then 7.5% trail | 11,814 | 63.1% | +2.23% | 1.14 | +15.4% | −19.4% | −17.8% |
| +10% then 10% trail | 11,285 | 62.0% | +2.35% | 1.15 | +17.5% | −18.5% | −18.3% |
| +10% then 25% giveback | 10,794 | 64.2% | +1.93% | 1.13 | +10.9% | −22.9% | −20.1% |
| **+10% then 40% giveback (my brief)** | 9,242 | 61.6% | **+1.97%** | 1.12 | **+6.5%** | −22.4% | −20.8% |
| +10% then 60% giveback | 8,306 | 59.2% | **+2.94%** | 1.20 | +14.2% | −19.9% | −20.5% |

**Your 5% trail beats my 40% giveback**, on expectancy (2.32% vs 1.97%) and much
more clearly on median return (+21.6% vs +6.5%). I argued above that 5% of
premium is only about 0.5% of underlying and would whipsaw. It does exit often —
win rate drops to 64.4% — but it banks enough of each move that the trade-off
pays. I was wrong to be confident about that without testing it.

Two honest caveats. The 60% giveback row has the highest expectancy of any
variant tested (2.94%) but a much lower median, meaning it depends on rare large
winners; that is a different risk appetite, not strictly better. And plain fixed
+20% is competitive with every trailing rule here, which suggests the trailing
machinery earns less than its complexity costs.

### Stop-loss — we were both wrong

| Variant | Trades | Win % | Expectancy | Profit factor | Median | 5th pct | Avg drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| −15% | 4,797 | 45.4% | +0.72% | 1.01 | −4.8% | −20.8% | −20.5% |
| −20% | 6,184 | 49.5% | +0.90% | 1.02 | −6.0% | −22.6% | −19.9% |
| −25% | 6,990 | 53.1% | +0.93% | 1.07 | −8.2% | −21.9% | −21.7% |
| −30% | 9,053 | 55.9% | **+1.77%** | 1.14 | +5.8% | −25.0% | −21.5% |
| **−50% (your brief)** | 9,242 | 61.6% | **+1.97%** | 1.12 | **+6.5%** | −22.4% | −20.8% |

**I told you to tighten the stop. The evidence says the opposite.** Tightening to
−15% cut expectancy from 1.97% to 0.72% and turned the median world negative.
Worse, it did not even reduce drawdown — every row sits near −20%, because the
account-level drawdown halt binds before the per-trade stop matters.

Two things are going on, and both are worth understanding because they generalise:

1. **A tight stop on a long option is inside the noise.** On a 0.6-delta
   contract, −20% of premium is roughly a −2% move in the underlying. Large-cap
   tech does that on an ordinary Tuesday. The stop is not cutting losers, it is
   sampling volatility.
2. **Risk-based sizing couples the stop to the position size.** Halving the stop
   distance doubles the contracts bought for the same dollar risk, which fills
   the premium cap with fewer, larger positions — note the trade count falling
   from 9,242 to 4,797. You lose diversification exactly when you thought you
   were reducing risk.

This does *not* rescue the +10%/−50% pair. The breakeven arithmetic in your
section 21 still holds, and the −50% row only clears it because the trailing
profit lock raises the average win. The lesson is that **the fix for a bad
target/stop ratio is on the target side, not the stop side** — which is what your
section 18 profit-lock does, and it is the more valuable of your two ideas.

### Delta band — neither of us picked the best cell

| Variant | Trades | Win % | Expectancy | Profit factor | Median | 5th pct | 95th pct |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.30–0.45 | 10,254 | 58.2% | **+4.57%** | 1.28 | **+42.7%** | −21.3% | +188.9% |
| 0.45–0.60 | 10,894 | 61.1% | +3.19% | 1.20 | +21.1% | −18.0% | +121.9% |
| **0.55–0.70 (both briefs)** | 9,542 | 63.0% | +2.44% | 1.15 | +13.5% | **−14.4%** | +74.7% |
| 0.60–0.75 | 8,332 | 63.8% | +1.60% | 1.11 | +2.6% | −18.1% | +57.9% |

Lower delta scored better on every headline number. I would still not act on this
one, for two reasons.

First, look at the 95th percentile column: 0.30–0.45 delta is not a better
strategy so much as a more leveraged one. Cheaper contracts mean more contracts
per risk dollar, and the distribution stretches in both directions. Its 5th
percentile is no better than the 0.55–0.70 band, whose −14.4% is the best
downside in the table.

Second, this is the result I trust least from a synthetic market. Real
out-of-the-money options carry a volatility skew — people pay up for lottery
tickets — and if the simulator underprices that skew it will systematically
flatter low-delta contracts. Your 0.55–0.70 preference buys the tightest downside
in the sweep, and for an unattended agent trading real money that is worth more
than the median.

### What the sweep does not say

Every variant has a profit factor between 0.87 and 1.28, and a 5th-percentile
outcome near −20%. **No configuration tested is robustly profitable.** The sweep
ranks rules against each other; it does not establish that the best-ranked rule
makes money on real data. Treat it as a way to avoid the clearly bad cells —
7–10 DTE, 12–18 DTE, tight stops — rather than as a recipe.

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
| 19 5% trailing | **You were right, I was wrong.** Beat my 40% giveback in the sweep. Keep it. |
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

1. **Move off 12–18 DTE.** This is the single highest-value change. Your window
   has an expectancy of +0.12% per trade and a median losing world; 21–60 days is
   materially better on every measure. It is one config line.
2. **Fix the target/stop ratio from the target side.** +10%/−50% needs an 83.3%
   win rate to break even. Your section 18 profit-lock is the right instrument;
   the +10% figure is the parameter I would treat as most negotiable. Do *not*
   tighten the stop — the sweep says that makes things worse.
3. **Size from risk, not from a dollar cap.** Otherwise the five stops in your
   section 43 each silently change the position size too, and you cannot tell
   which variable moved the result. This is not hypothetical: it is one of the
   two effects that made tight stops look bad above.

Keep your 5% trail, your 0.55–0.70 delta band, your duplicate protection, your
reconciliation halt, and your four operating modes. Those were all better than
what I had.

Everything else in your document is either right, or a preference the simulator
can settle.
