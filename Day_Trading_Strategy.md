# My Day Trading Strategy — Final Version

This is the strategy I want to run on paper trading for intraday equity
day trading. Universe is six tech mega-caps plus the gold ETF. It uses
the same structural template as my crypto and swing strategies but
shifts to intraday timeframes, VWAP-based setups, same-session exits,
and fully autonomous execution via a GitHub Actions watcher. Read it
carefully and acknowledge back to me before we trade. If anything is
ambiguous, ask.

---

## Reality check — read this first

**This strategy runs autonomously via a GitHub Actions watcher.** The
watcher fires entries on qualifying 10/10 setups, manages stops/TPs,
and exits at hard-stops — all without my intervention. My job is to
**review** what the watcher did at the end of each session, not to
babysit live positions.

**Hard-gated to paper trading.** Auto-execute is refused in code if
`ALPACA_PAPER_TRADE != True`. To go live, two separate switches must
flip in two different files — no single keystroke can move from paper
to live.

**Empirical context I am acknowledging:** Large-scale academic studies
of retail day traders consistently find that the majority lose money
net of fees over time. This is paper trading, so the test is whether
*my rule set*, executed mechanically without emotional interference,
produces positive expectancy across at least 50 closed paper trades —
not whether discretionary day trading is profitable in general. Even
if paper performance is strong, I will sit with results for at least
90 days before considering live capital.

---

## Hard constraints — non-negotiable

- **Paper trading only.** Live trading is OFF. Auto-execute is
  hard-refused in code unless `ALPACA_PAPER_TRADE=True`.
- **All positions closed by 3:55 PM ET.** No exceptions. No overnight
  holds, ever. If anything is open at 3:55 PM, close at market.
- **Regular session only.** No pre-market, no after-hours trading.
- **Time-of-day windows are firm:**
  - 9:30 – 9:45 AM ET: opening range observation. NO entries.
  - 9:45 – 10:30 AM: Setup A (ORB) AND Setup B (VWAP-reclaim) both armed.
  - 10:30 – 11:30 AM: Setup B only.
  - 11:30 AM – 2:00 PM: midday dead zone. NO new entries; manage only.
  - 2:00 – 3:00 PM: Setup B only (secondary window).
  - 3:00 – 3:55 PM: manage only. NO new entries.
  - 3:55 – 4:00 PM: all positions flat.
- **Max notional per trade: $500.**
- **Max 3 trades per session.** Also keeps us within the PDT rule
  ceiling of 3 day-trades per 5 trading days if I ever migrate to a
  live margin account < $25K.
- **No leverage. No options. No shorts.** Long-only spot equities.
- **No trade on earnings day or the day after** for any stock in the
  universe. (ETFs are exempt — GLD has no earnings.)
- **No trade within 30 minutes of a scheduled FOMC announcement, CPI,
  PCE, NFP, or other tier-1 economic release.**
- **No trade without all five components defined upfront:** entry,
  stop, take-profit, position size, and a written thesis. The watcher
  must compute all five before placing any order.
- **Auto-execute kill switch:** Setting `WATCHER_DAY_AUTO_EXECUTE=true`
  in GitHub Secrets enables the autonomous watcher. Anything else
  (unset, "false", empty) keeps the watcher in alerts-only mode where
  Telegram pings me about qualifying setups but no orders are placed.

---

## Architecture — single track

Day trading is the only track. Same-session intraday entries on
selected stocks and the gold ETF. The watcher runs it autonomously.

(The original "Track 2 — Long-term ETF holdings" buy-and-hold idea is
dropped. If I want accumulation later, it gets its own document.)

---

## Asset universe — flat

Seven names. No tiers — the 10-condition setup gate plus the 3-trade
session cap throttle frequency on their own.

| Symbol | Class | Notes |
|---|---|---|
| **NVDA** | Stock | Semiconductors. Highest intraday range, news-sensitive. |
| **TSLA** | Stock | High volatility, headline-driven, widest ATR. |
| **AAPL** | Stock | Clean technicals, orderly moves, deeply liquid. |
| **AMZN** | Stock | Similar to AAPL, occasionally more volatile. |
| **GOOGL** | Stock | Clean but slower than the others. |
| **MSFT** | Stock | Most stable mega-cap, fewer day-tradable setups. |
| **GLD** | ETF | Gold. Macro-news driven (Fed, CPI, geopolitical). Decorrelated from tech. |

No additions until at least 30 closed paper trades have run across
this universe.

### Correlation cap

All six stocks are tech mega-caps with intraday correlation often
above 0.6. GLD is decorrelated from tech but its own moves are
independent enough that holding it alongside a tech name doubles risk
on different vectors rather than diversifying.

- **Maximum 1 open position at a time. Always.**
- A signal on a second name does NOT override an open position.

---

## Timeframes — three layers

- **15-minute chart** — intraday context. Where I judge the session's
  character and identify key levels.
- **5-minute chart** — primary setup timeframe. Where the trade triggers.
- **1-minute chart** — entry execution timing only. NOT for setup logic.

---

## Indicators

- **VWAP** (Volume-Weighted Average Price) — session-cumulative from
  9:30:00 ET, reset daily. `VWAP_t = Σ(typ_price × volume) / Σ(volume)`
  over all 1-min bars from session open through time *t*, where
  `typ_price = (high + low + close) / 3`. Above VWAP = buyers in
  control intraday; below = sellers.
- **EMA 9 and EMA 20 on the 5-min chart** — short-term trend.
- **Opening range** — the high (ORH) and low (ORL) of the first 15
  minutes (9:30–9:45 AM ET). Drawn at 9:45 AM and held all session.
- **Pre-market high / low (PMH / PML)** — drawn as horizontal lines at
  session start. Sourced from yfinance (Yahoo Finance) at the 9:25 ET
  pre-scan tick. Used as descriptive context only — not in any setup
  trigger condition. If yfinance fetch fails, watcher proceeds without
  these levels.
- **ATR(14) on the 5-min chart** — for stop sizing.
- **Bar-RVOL** — for the 5-min bar at time *t*, today's bar volume
  divided by the 20-day average volume of the 5-min bar at the same
  time-of-day slot. Used in setup conditions A.5 and B.6.
- **Session-RVOL** — today's cumulative session volume so far divided
  by the 20-day average cumulative volume at the same minute of
  session. Used as the "dead session" no-trade filter (< 0.7 → no
  entries).

### Data fidelity note

All Alpaca bars on the paper feed are IEX-only (~2-3% of consolidated
market volume). VWAP direction and RVOL ratios self-normalize and
remain valid signals. Absolute VWAP values may differ from TradingView's
by a few cents — this is documented noise, not a bug. We accept this
because (a) SIP is paid, (b) the strategy is paper-mode experimental,
and (c) crossings and ratios track consolidated data closely enough
for 5-min-resolution decisions.

---

## Daily regime — set once at pre-market

Run this check at the 9:25 ET pre-scan. It governs the whole session.

**Broad market (SPY):**
- **Bullish:** SPY daily close > 200 SMA AND 50 SMA ≥ 200 SMA.
  Both setups armed.
- **Improving:** SPY daily close > 200 SMA BUT 50 SMA < 200 SMA.
  Setup A only.
- **Choppy:** SPY oscillating within ±5% of 200 SMA over the last 20
  daily candles, no clear direction. **No trades** — long-only day
  trading in chop is a losing proposition.
- **Bearish:** SPY < 200 SMA. **No trades.** (Long-only with no good
  edge in a bearish regime.)

**Overnight gap check** (per ticker):
- If a universe ticker has gapped > 4% overnight (yesterday's regular-
  session close vs today's first 5-min bar open), it is OFF the
  eligible list for today. Gap days are gap days, not setup days.

---

## Intraday character — re-checked each scan

The daily regime sets the bias; the intraday character determines
*when* a setup is valid.

- **Bullish session:** SPY above session VWAP AND above its 5-min
  EMA 9. Both setups armed.
- **Mixed session:** SPY whipping across VWAP (last 5-min close on
  opposite side of VWAP from prior 5-min close). Setup A only, and
  only with bar-RVOL > 1.5× on the candidate's breakout candle.
- **Bearish session:** SPY below VWAP AND below 5-min EMA 9. No new
  entries. Manage open position if any.

---

## Entry Setup A — Opening Range Breakout (ORB)

Long entry triggers only when ALL of these are true:

1. Daily regime is bullish or improving; SPY intraday session is
   bullish (or mixed with the elevated-RVOL condition above).
2. We are within the **9:45 – 10:30 AM ET** window.
3. The opening range (9:30–9:45) has been identified. The candidate
   has its ORH and ORL marked.
4. A 5-minute candle **closes** above the ORH (no wicks-only).
5. **Bar-RVOL on the breakout candle ≥ 1.5×**.
6. Price is **above session VWAP** at the moment of breakout.
7. The 5-min EMA 9 is above the EMA 20.
8. A logical stop placement exists: at the **midpoint of the opening
   range**, OR at the ORL, whichever is tighter, AND stop distance
   ≤ 1.5× the 5-min ATR(14).
9. No earnings today or yesterday for the candidate. (Skipped for GLD.)
10. No open position anywhere (correlation cap).

---

## Entry Setup B — VWAP Reclaim Continuation

Long entry triggers only when ALL of these are true:

1. Daily regime is bullish; SPY intraday session is bullish.
2. We are within an entry window:
   - **9:45 – 11:30 AM** (primary), OR
   - **2:00 – 3:00 PM** (secondary).
3. The candidate had a session pullback that touched or dipped below
   session VWAP from above.
4. The most recent **closed 5-min candle is green** AND closes back
   **above session VWAP**.
5. The 5-min EMA 9 is above the EMA 20 (intraday uptrend intact).
6. **Bar-RVOL on the reclaim candle ≥ 1.0×**.
7. There is a clear prior intraday high above current price providing
   a reasonable target (≥ 2R).
8. Stop placement: just below the VWAP touch low, plus a 0.25× ATR
   buffer. Stop distance ≤ 1.5× the 5-min ATR(14).
9. No earnings today or yesterday for the candidate. (Skipped for GLD.)
10. No open position anywhere (correlation cap).

**No chasing rule (both setups):** If the trigger has already moved
more than 1.5× ATR past the entry trigger price without coming back,
skip. The setup is gone.

**Setup A wins ties:** If both setups fire on the same name within
the 9:45–10:30 overlap window in the same scan, take Setup A. (Its
window is narrower and expires sooner.)

**Same-name reentry is allowed.** If a position is opened and fully
exited within the session (any combination of TP1+TP2 fills, time
stop, or hard exit), a fresh qualifying setup on the same name later
the same session is a valid entry — subject to the 3-trade session
cap and 1-position correlation cap.

---

## Position sizing — R-based, capped, conservative

Day trading uses **0.5% risk per trade**, half the swing strategy's
1%, because trade frequency is higher and cumulative session risk
must stay bounded.

1. Stop distance % = (entry − stop) / entry × 100
2. Risk dollars = 0.5% of current account equity
3. Required position size (notional) = risk dollars / stop distance %
4. **Cap at $500 notional.**
5. If calculated position < $50 notional, skip — fees + slippage will
   eat the edge.
6. If stop distance < 0.3%, skip — too tight, normal noise stops us out.
7. If stop distance > 3%, skip — too wide for an intraday move; 2R
   becomes unrealistic in a single session.

Include ~0.05% per-side slippage in the math. Round share count down
to whole shares.

---

## Stops, targets, and trade management

**Initial stop:** A **stop-market** order, placed immediately after
entry fills. Stop-market (not stop-limit) so we are guaranteed an exit
on a fast adverse move.

**Reward/risk minimum:** **2R** required to enter. If the next
intraday resistance — prior high, PMH, or a clear level — does not
allow 2R, skip the trade.

**Take-profit ladder** (simpler than swing — day trades don't get a
long runway):
- **TP1 at +1R:** close 50% of position as a limit order. **Move stop
  to entry (breakeven)** immediately after TP1 fills.
- **TP2 at +2R:** close remaining 50% as a limit order.
- **No trailing on the final piece** — day trading is too short to
  trail effectively. Just take 2R and be done.

**Time stop — measured from fill time, not signal time:**
If the trade hasn't hit TP1 within **30 minutes of fill time** AND
price is not above entry by at least +0.25R, exit at market. Capital
must be working hard or it's not earning its keep intraday.

**Hard exit triggers** (any one closes the trade immediately):
- 3:55 PM ET reached.
- SPY breaks below its session VWAP on a 5-min close after the trade
  was opened in a bullish session.
- The candidate ticker triggers a circuit-breaker halt.

**Stops never widen.** Ever.

---

## Risk caps

- **Max simultaneous positions:** 1.
- **Max trades per session:** 3.
- **Daily loss limit:** **−1.5%** of account equity (realized).
  Hit → stop trading for the session. Tighter than swing's −2% because
  day trades close faster and a bad session can compound.
- **Weekly loss limit:** **−4%** of account equity. Hit → stop until
  the following Monday.
- **Consecutive loss cooldown (session level):** After 2 losing trades
  in the same session, stop for the day. After 3 losing sessions in a
  row, pause for 5 trading days.
- **No-trade conditions** — skip any trade if any are true:
  - Daily regime is choppy or bearish.
  - Overnight gap > 4% on the candidate.
  - Bid/ask spread > 0.05% on the candidate.
  - Earnings today or yesterday (stocks only).
  - Within 30 minutes of FOMC / CPI / PCE / NFP / GDP release per
    `state/econ_events.json`.
  - Session-RVOL < 0.7× (dead session).
  - Outside the defined entry windows.
  - Already 3 trades taken this session.
  - State data files (earnings.json or econ_events.json) are
    stale (> 10 days old).

---

## Operating procedure

### Scan cadence

The day-trade watcher runs every **5 minutes** during the active
session, weekdays only. (Implementation note: GitHub Actions free
tier on a private repo has a 2000 min/month budget; a strict 5-min
cron during the full session may exceed this. We start with a 5-min
cron limited to 9:25–15:55 ET and downgrade to 10-min if we hit the
budget. Cadence is an infrastructure tuning question, not a strategy
question.)

**Pre-session scan (9:25 AM ET):**
1. Account check — confirm paper mode, current equity, week's running
   P&L vs −4% cap.
2. Daily regime — pull SPY 200 SMA / 50 SMA from daily bars.
3. Overnight gap check — flag any universe ticker that gapped > 4%.
4. Earnings filter — read `state/earnings.json`, flag any stock
   reporting today or yesterday.
5. Economic-event filter — read `state/econ_events.json`, mark any
   30-min blackouts for the session.
6. Pre-market H/L fetch — pull PMH/PML for each ticker via yfinance.
7. State the eligible universe for the day in a Telegram alert.

**Opening (9:30–9:45 AM ET):**
- Watcher observes only. Opening range forms. No entries possible.

**Primary window (9:45–11:30 AM ET):**
- 9:45 — mark each ticker's ORH/ORL.
- Every 5 min: evaluate Setup A (until 10:30) and Setup B (all window)
  on each eligible ticker.
- Fire entries via auto-execute when 10/10 qualify.

**Midday (11:30 AM–2:00 PM ET):**
- Watcher manages open position only. No new entries. Time-stop and
  hard-exit checks continue.

**Secondary window (2:00–3:00 PM ET):**
- Setup B only. No ORB after morning.

**Closing (3:00–3:55 PM ET):**
- Watcher manages open position; no new entries.
- At 3:55 PM exactly: any open position is closed at market.

**Post-close (4:00–4:15 PM ET):**
- Watcher emits the daily summary Telegram alert (see below) and
  appends a lifecycle stats block reconstructed from Alpaca order
  history.

### Trade proposal format (Telegram alert when auto-execute fires)

```
=== ENTRY EXECUTED: NVDA — Setup A (ORB) ===

Time: 10:12 AM ET, Tue 2026-05-26

Daily regime:
  ✓ SPY bullish: close $XXX > 200 SMA $XXX
  ✓ 50 SMA $XXX > 200 SMA $XXX
  ✓ No earnings today or tomorrow (next: 2026-08-27)

Intraday character:
  ✓ SPY above session VWAP and 5-min EMA 9
  ✓ No gap > 4% on NVDA overnight (+0.6%)
  ✓ Session-RVOL = 1.12× (healthy)

Setup A conditions (10/10):
  ✓ Time window: 9:45–10:30 AM ET (currently 10:12)
  ✓ Opening range: ORH $XXX, ORL $XXX (range $X.XX wide)
  ✓ 5-min candle closed above ORH at $XXX
  ✓ Bar-RVOL: 2.1×
  ✓ Above VWAP ($XXX)
  ✓ 5-min EMA 9 > EMA 20
  ✓ Stop at OR midpoint $XXX (1.2× ATR distance)
  ✓ No earnings within window
  ✓ No open position
  ✓ Trade #1 of session (cap 3)

Risk math:
  Account equity: $XXXX
  Risk per trade (0.5%): $XX
  Entry: $XXX
  Stop: $XXX (−0.9%)
  Position size: $500 (capped) → X shares
  TP1: $XXX (+0.9%, +1R) — sell 50%, then breakeven stop
  TP2: $XXX (+1.8%, +2R) — sell 50%
  Time stop: 10:42 AM if not at +0.25R or better
  Hard exit: 3:55 PM ET no matter what

Orders placed:
  Entry  — market buy, filled at $XXX (X shares)
  Stop   — stop-market sell at $XXX (X shares)
  TP1    — limit sell at $XXX (X shares)
  TP2    — limit sell at $XXX (X shares)
```

---

## Auto-execution mode

Activated by setting `WATCHER_DAY_AUTO_EXECUTE=true` in GitHub Secrets.
Hard-refused unless `ALPACA_PAPER_TRADE=True`. When active, the
GitHub-Actions watcher does the full entry + management + exit
sequence on its own.

**Phase D5a — auto-entry:**
- Watcher detects a qualifying 10/10 setup.
- Pre-execution safety gates run (all listed in §"Risk caps" + the
  no-trade conditions).
- If any gate fails: silent skip with reason logged in the next scan
  summary.
- Otherwise: **market** entry → wait for fill → **stop-market** + TP1
  limit + TP2 limit placed immediately as resting orders on Alpaca.
- Telegram alert on every action.

**Phase D5b — in-trade management:**
Runs at the TOP of every scan, before the entry pass, so any closes
free up the position slot before new entries are considered.

For the (at most) one open position, in priority order:
- **3:55 PM exit:** if current time ≥ 15:55 ET → market sell all,
  cancel resting orders.
- **Hard exit — SPY breaks VWAP:** if SPY 5-min close below session
  VWAP after our position opened → market sell all.
- **Hard exit — circuit breaker halt** on the position ticker →
  market sell all.
- **TP1 fill detected** (one filled non-stop limit SELL since open) →
  cancel original stop, place new stop-market at entry (breakeven).
- **Time stop:** if (now − fill_time) ≥ 30 min AND current price <
  entry + 0.25R → market sell all, cancel resting orders.
- Otherwise: no action.

All Telegram alerts: every management action gets its own alert
(STOP → BREAKEVEN, TIME STOP, REGIME EXIT, 3:55 CLOSE, HALT EXIT).

**Phase D5c — lifecycle stats:**

Each scan also appends a **Lifecycle (last 90d)** block to the scan
summary, reconstructed statelessly from Alpaca's closed-orders
history. No project-side journal file is maintained — the source of
truth is always Alpaca.

Stats include:
- Open and closed trade count
- Win rate
- Average R-multiple per closed trade
- Largest single-trade win and loss in R
- Average trade duration (time from fill to last exit)
- Setup A vs Setup B trade count and respective expectancies
- Per-symbol trade count and respective expectancies

Subjective fields (thesis, lesson learned, "did I follow the rules?")
are not auto-generated. I add those manually via Claude after each
session if I want to journal further.

---

## External data refresh architecture

Two state files are read by the watcher every scan but never written
by it. A separate weekly job owns them.

**`state/earnings.json`** — upcoming earnings dates for the universe.
Schema:
```json
{
  "refreshed_at": "2026-05-25T06:00:00Z",
  "source": "finnhub_free",
  "earnings": {
    "NVDA": ["2026-08-27"],
    "TSLA": ["2026-07-23"],
    ...
  }
}
```

**`state/econ_events.json`** — upcoming tier-1 economic events for the
next 35 days. Schema:
```json
{
  "refreshed_at": "2026-05-25T06:00:00Z",
  "source": "manual_or_api",
  "events": [
    {"name": "FOMC", "datetime_et": "2026-06-17T14:00:00-04:00"},
    {"name": "CPI",  "datetime_et": "2026-06-11T08:30:00-04:00"},
    ...
  ]
}
```

**Refresh workflow** (`.github/workflows/refresh-day-calendars.yml`):
- Runs every Sunday at 06:00 UTC.
- Pulls earnings for the 6 stock tickers from Finnhub free tier
  (60 req/min — well under limit).
- Pulls FOMC/CPI/PCE/NFP/GDP schedule for the next 35 days.
- Writes both JSON files, commits, pushes.
- Telegram alert ONLY on failure.

**Fail-closed posture:** if either state file is older than 10 days
when the watcher reads it, the watcher refuses ALL new entries and
sends a "stale calendar data" Telegram alert until the refresh job
runs successfully.

---

## End-of-session summary (every trading day, 4:00–4:15 PM ET)

The watcher emits a Telegram alert with:
- Trades taken today: count, P&L in $ and R, win/loss
- Trade-by-trade: setup, entry, exit, reason for exit, R-multiple
- Session P&L vs the −1.5% daily cap
- Week's running P&L vs the −4% weekly cap
- Consecutive loss tally
- Lifecycle (last 90d) block
- Tomorrow's earnings calendar for the universe
- Tomorrow's tier-1 economic events

---

## What you (the watcher / me) do NOT do

- Do not predict prices.
- Do not act on news, analyst calls, social media, premarket movers
  lists, or vibes.
- Do not hold a position overnight. Ever.
- Do not trade pre-market or after-hours.
- Do not enter outside the defined time windows.
- Do not chase breakouts more than 1.5× ATR past trigger.
- Do not trade against the daily regime.
- Do not move stops in the wrong direction. Ever.
- Do not increase size after a loss.
- Do not average down.
- Do not "make back" losses with bigger size or more trades — the
  loss limits exist for this exact moment.
- Do not trade earnings days or economic-release windows.
- Do not override the trade-per-session cap of 3.
- Do not run the watcher with `WATCHER_DAY_AUTO_EXECUTE=true` while
  `ALPACA_PAPER_TRADE=False`. Code enforces this; do not disable the
  enforcement.
- Do not hand-edit `state/earnings.json` or `state/econ_events.json`
  — the weekly refresh job owns those files.
- Do not give me investment advice. Execute the rules above.

---

## Before we start — acknowledge

Please:

1. Confirm you've read this whole document.
2. State the 7 tickers in the universe.
3. State the correlation cap and the trade-per-session cap.
4. State the 6 time-of-day windows.
5. State the 10 conditions for Setup A.
6. State the 10 conditions for Setup B.
7. State the 7 risk caps and the no-trade conditions.
8. State the time stop rule (measured from fill, 30 min, +0.25R bar).
9. State the 3:55 PM hard exit.
10. State the auto-execute kill switch and its paper-only enforcement.
11. State the two state files and the 10-day staleness fail-closed.
12. Call `get_account_info` and confirm we're in paper mode + current
    equity.
13. Ask: "Run the pre-market scan now, or wait for the next session?"

---

## My reminder to myself

This is paper trading. Real money is not at risk. The point is to
test whether this rule set generates positive expectancy across at
least **50 closed day trades** (day-trade sample sizes need to be
larger than swing because per-trade edge is smaller) — not whether it
makes money on the first five. Even if paper performance is strong,
I will sit with results for at least 90 days before considering live
capital, and I will not consider live capital larger than what I am
comfortable losing entirely.

Day trading has a famously poor track record for retail. This
strategy is a structured experiment to see if **disciplined mechanical
execution** of a defined rule set — without my emotions, without
narrative drift, without screen-induced impatience — produces a
different outcome. The honest answer to "should I day trade?" might
end up being "no." That answer is a legitimate output of this
experiment.

The autonomous watcher is the whole point. If I find myself wanting to
intervene mid-session, override the rules, or "improve" the setups
mid-experiment, that itself is a signal — log the urge, don't act on
it, review the data at the end of the 90-day window.

Nothing here is investment advice — it is a discipline framework for
me to test against real markets safely.

Let's begin.
