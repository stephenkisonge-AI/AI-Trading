# My Crypto Strategy — Final Version

This is the strategy I want you to run for me on Alpaca paper trading. It's
synthesized from mainstream crypto trading consensus (Binance Academy,
Cowen's cycle work, CryptoCred's TA, Theseus TA paper, Duke investor's
guide) plus rules I've worked through. Read it carefully and acknowledge
back to me before we trade. If anything is ambiguous, ask.

---

## Hard constraints — non-negotiable

- **Paper trading only.** Live trading is OFF. Do not even propose a live
  trade until I explicitly tell you to flip the mode.
- **Max notional per trade: $500.** No exceptions, no "this one is special."
- **No leverage. No shorts.** Long-only spot crypto.
- **No trade without all five components defined upfront:** entry, stop,
  take-profit, position size, and a written thesis.
- **Capital preservation > capital growth.** A skipped trade is free.
  A bad trade isn't.
- **Confirmation gate:** In LIVE mode, you never place an order without
  showing me the full proposal and getting an explicit 'go' from me.
  Every single time. In PAPER mode, the gate can be bypassed by setting
  `WATCHER_AUTO_EXECUTE=true` so the GitHub-Actions watcher executes
  qualifying setups on its own — the mechanical 8-condition checklist
  IS the gate. The flag is honored only when `ALPACA_PAPER_TRADE=True`;
  live mode auto-execute is hard-refused in code. See §"Paper auto-execution
  mode" below for what runs automatically and what is still deferred.

---

## Architecture — two independent tracks

The strategy runs on two separate tracks. They don't share capital
allocations, they don't share signals, they don't interfere.

### Track 1 — Active swing trading (default ON)
Discretionary entries based on multi-timeframe technical setups.
This is where we work. Most of this document is about this track.

### Track 2 — DCA accumulation (default OFF)
Weekly fixed-dollar buys of BTC and ETH only. Not for trading,
for accumulation. Only enable when I explicitly say so. When enabled:
- 60% BTC / 40% ETH split
- Weekly cadence (same day every week)
- Maximum $100/week combined unless I raise it
- Held indefinitely — these are NOT swing positions

For now, treat Track 2 as disabled. Don't act on it.

---

## Track 1: Swing trading

### Asset universe (in priority order)

Trade only these pairs. Add new pairs only if I approve.

1. **BTC/USD** — primary
2. **ETH/USD** — primary
3. **SOL/USD** — alt L1, narrative-differentiated from BTC/ETH
4. **LINK/USD** — oracle infra, sometimes decouples on coin-specific news
5. **AVAX/USD** — alt L1, different consensus / subnet model

Cap at 5 for now. Adding pairs increases setup frequency but does NOT
reduce risk — everything in crypto is 70%+ correlated to BTC in down
moves. More pairs = more decisions to ignore correctly during chop, not
more diversification. No further additions until we have 30 closed
trades of paper results across this universe.

If a pair fails our no-trade conditions (spread > 0.5%, missing candles,
repeated API errors) for 3+ consecutive scans, drop it from the active
set and tell me — we either fix the data issue or remove the pair.

### Timeframes — three layers

- **Daily** — market regime filter. Are we in a bull, bear, or chop
  environment? This decides if we trade at all.
- **4-hour** — setup confirmation. This is where pullbacks and
  breakouts form.
- **1-hour** — entry timing. This is where we pull the trigger.

You must check all three before any entry.

### Indicators

- EMA 20, EMA 50, EMA 200 (on each timeframe)
- RSI(14) (on each timeframe)
- ATR(14) — for stop sizing
- Volume SMA(20) — for confirmation
- Recent swing highs/lows visible on the 4H chart

### Market regime classification (Daily chart)

Run this check first, before anything else. Output the regime label:

- **Bullish:** Daily close > EMA 200 AND EMA 50 ≥ EMA 200 (or crossing
  upward in the last 10 daily candles).
- **Improving neutral:** Daily close > EMA 200 BUT EMA 50 < EMA 200
  (early recovery).
- **Choppy neutral:** Daily price oscillating within 5% of EMA 200
  with no clear direction over 20 candles.
- **Bearish:** Daily close < EMA 200 AND EMA 50 < EMA 200.

Trading rules by regime:
- **Bullish:** Both Setup A and Setup B are armed.
- **Improving neutral:** Only Setup A (pullbacks). No breakout chasing.
- **Choppy neutral:** No new entries. Manage open positions only.
- **Bearish:** No new swing entries. If DCA is enabled, it continues.
  Period.

### Entry Setup A — Pullback continuation

Long entry triggers only when ALL of these are true:

1. Daily regime is **bullish** or **improving neutral**.
2. On the 4H chart, price > EMA 200.
3. Price has pulled back to within 1% of the 4H EMA 20 OR EMA 50,
   without breaking the most recent 4H higher low.
4. On the 4H chart, RSI(14) is between **35 and 50** (the oversold-but-
   not-broken zone — concrete, mechanical, no "turning upward" subjective
   reading).
5. On the 1H chart, the most recent closed candle is green AND closes
   back above the 1H EMA 20.
6. Volume on that 1H entry candle ≥ 0.8× the 20-period average volume
   (filter out dead-zone moves).
7. A logical stop placement exists: below the most recent 4H swing low,
   AND that stop distance is no more than 1.5× the 4H ATR(14).
8. No existing position in this asset.

### Entry Setup B — Breakout retest

Long entry triggers only when ALL of these are true:

1. Daily regime is **bullish** (not neutral — breakouts in chop fail).
2. Price has broken above a clear 4H resistance zone or the 20-period
   high in the last 10 candles.
3. The breakout 4H candle closed above the level (no wicks-only).
4. Volume on the breakout candle ≥ 1.2× the 20-period average volume.
5. RSI(14) on the 4H chart is between 50 and 70 (momentum confirmed,
   not exhausted).
6. Price has retested the broken level (now support) on the 1H chart
   and held — meaning the 1H low touched within 0.5% of the level
   and the next 1H candle closed green above it.
7. A logical stop placement exists: just below the retested level,
   AND that stop distance is no more than 1.5× the 4H ATR(14).
8. No existing position in this asset.

**No chasing rule:** If the breakout has already moved more than
2× ATR above the breakout level without a retest, we missed it.
Skip and wait for the next setup.

### Position sizing — R-based, capped

Calculate in this exact order:

1. Define **stop distance %** = (entry - stop) / entry × 100.
2. Define **risk dollars** = 1% of current account equity.
3. Required position size (notional) = risk dollars / stop distance %.
4. **Cap that at $500 notional.**
5. If the calculated position would be < $50 notional, skip the trade
   (not enough room to be meaningful after fees).
6. If stop distance > 8%, skip the trade — the entry is too far from
   support, the trade has poor risk/reward by definition.

Include estimated fees (~0.25% per side on Alpaca crypto) in the
calculation. Round notional down to a quantity Alpaca will accept.

**Fee-aware R reality:** Round-trip fees + slippage cost ~0.5% of
notional. On a 3% stop that's about 0.17R of friction. TP1 at +1.5R
nominal is really ~+1.3R net. Set the orders at nominal levels (the
orders use prices, not R), but evaluate strategy expectancy on NET R
after fees — not the headline number.

### Stops, targets, and trade management

**Initial stop:** Whichever is tighter:
- 1.5× ATR(14) below entry on the 4H chart
- Just below the most recent 4H swing low

**Reward/risk minimum:** 2R required to enter. If we can't see a
realistic path to 2R based on the next overhead resistance, skip.

**Exit architecture (one broker-held stop + application-managed TPs).**
At any moment the position has exactly ONE resting sell order at
Alpaca: a stop-limit for the full current position quantity. TP1/TP2
are *levels the application watches*, not resting limit orders. This
replaced the earlier simultaneous bundle (stop 100% + TP1 50% + TP2
25% resting at once — up to 175% of the position in open sells), which
violated the sell-quantity invariant below. Implementation:
`src/swing_exits.py`.

- **At entry:** market entry → reconcile the actual fill → recompute
  risk and TP levels from the actual average fill price → place one
  stop-limit for exactly the filled quantity → verify Alpaca accepted
  it and its remaining quantity equals the position. If protection
  cannot be verified, the trade is marked RECOVERY_REQUIRED, new
  entries freeze, and a CRITICAL alert fires. TP1/TP2 are persisted to
  the journal as intended levels only.
- **TP1 at +1.5R** (actual-fill R): when a *fresh* executable bid
  (quote age ≤ 2 min; the bid is the executable side for a long's
  sell) reaches the level: persist the intent → confirm position and
  stop → cancel the stop and poll to a *confirmed terminal* state →
  re-read the position → market-sell the 50% tranche → re-read → place
  a replacement stop-limit at **breakeven** for exactly the remaining
  quantity → verify accepted + quantity. The window with no broker-held
  stop is measured and journaled on every transition.
- **TP2 at +3R:** same sequence for 25%; the replacement stop keeps the
  breakeven floor.
- **Final 25% (runner):** trail with a 2× ATR stop OR exit on a 4H
  close below the EMA 20, whichever triggers first. Trail raises use
  the same cancel-confirm-replace sequence. Stops never move down.
- **Dust rule:** if a tranche would leave a remainder below Alpaca's
  tradable increment, the entire remaining position is closed instead —
  never leave an unprotectable dust position.
- **Stale/unavailable quote:** no TP transition fires; the protective
  stop stays untouched. A historical candle high is never treated as
  proof a TP was executable at that price.

**Gap watchdog (every management pass):** if a fresh bid is at or below
the stop trigger while the stop-limit is still open/unfilled, the
application cancels the stop (confirmed), market-sells the exact
remaining quantity, confirms flat, and emits a CRITICAL alert. The
watchdog runs at management cadence and is **not** equivalent to a
broker-held stop-market order; Alpaca crypto does not support
stop-market, so a fast gap through the limit band can still fill worse
than modeled.

**Sell-quantity invariant (checked after every transition):** the sum
of *remaining* (unfilled) quantity across all open sell orders for a
symbol must never exceed the current broker position quantity. A
violation freezes new entries and marks the trade for reconciliation.

**Unknown state = stand still:** if the stop's status or the position
quantity cannot be established, nothing is submitted; the trade is
marked RECOVERY_REQUIRED, entries freeze, and reconciliation
(`scripts/reconcile.py`) must pass before entries resume.

**Time stop:** If a trade hasn't hit TP1 within 10 days, close it at
market. Capital should be working.

**Hard exit:** If the daily regime flips from bullish to bearish (daily
close below EMA 200 with confirmation candle), exit ALL open swing
positions at market regardless of P&L.

**Stops never widen.** Ever. They only tighten or move toward profit.

### Risk caps

These are firm limits. Hitting any of them stops trading.

- **Max simultaneous positions:** 2 — but BTC and ETH count as ONE slot
  (correlation ~85%; holding both is one doubled-up bet on crypto, not
  diversification). If BTC is open, ETH setups are skipped until BTC
  closes, and vice versa. SOL, LINK, AVAX freely combinable with any
  other within the 2-slot cap.
- **Max new entries per day:** 1.
- **Daily loss limit:** -2% of account equity (realized + unrealized).
  If hit, stop trading until next UTC day.
- **Weekly loss limit:** -5% of account equity. If hit, stop trading
  until the following Monday UTC.
- **Equity drawdown gate:** If peak-to-trough equity drawdown over any
  rolling window hits -10%, pause and review before any new entries.
  Catches the slow grind the daily/weekly caps miss.
- **Consecutive loss cooldown:** After 3 losing trades in a row, pause
  for 24 hours minimum. Tell me. We review before resuming.
- **No-trade conditions:** Skip any trade if you observe:
  - Bid/ask spread > 0.5% on a major
  - Missing or stale candles in the data
  - API errors during signal evaluation
  - Recent (< 60 min) price gap > 5% with no clear cause

---

## Operating procedure

### Scan cadence

Crypto trades 24/7 on Alpaca, so timing is consistent. Run a full scan
when each 4H candle closes (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC).

For each scan, in this order:

1. **Account check** — call `get_account_info`. Confirm paper mode and
   show current equity, P&L, day's running loss vs the -2% cap.
2. **Position check** — call `get_all_positions`. For each open position,
   check distance to stop, distance to next TP, and time-in-trade vs
   the 10-day cap.
3. **Order check** — call `get_orders(status="open")`. Confirm protective
   orders (stops, TPs) are in place for every open position. If any are
   missing, fix immediately and tell me.
4. **Regime classification** — pull daily bars, compute EMAs, output the
   current regime label.
5. **Setup scan** — for each pair in the universe (BTC, ETH, and SOL if
   enabled), evaluate Setup A and Setup B against the full checklist.
   Tell me what you found.
6. **Risk gate** — verify we haven't hit daily/weekly loss caps or
   consecutive-loss cooldown.
7. **Propose if anything qualified.** Otherwise, summarize and stand down.

### Trade proposal format

When something triggers, present it exactly like this:

```
=== ENTRY CANDIDATE: ETH/USD — Setup A (Pullback) ===

Regime check:
  ✓ Daily bullish: close $XXX > EMA200 $XXX, EMA50 > EMA200

Setup conditions (8/8):
  ✓ 4H price $XXX > 4H EMA 200 $XXX
  ✓ Pulled back to 4H EMA 20 ($XXX, distance 0.4%)
  ✓ Higher low intact: prior swing low $XXX still holds
  ✓ 4H RSI = 41.2 (in 35-50 zone)
  ✓ 1H last candle: green, close $XXX above 1H EMA 20 $XXX
  ✓ 1H volume: 1.05× 20-avg
  ✓ Stop distance: 1.2× ATR (acceptable)
  ✓ No open ETH position

Risk math:
  Account equity: $XXXX
  Risk per trade (1%): $XX
  Entry: $XXX
  Stop: $XXX (-3.4%)
  Position size: $500 (capped) → 0.XXX ETH
  TP1: $XXX (+5.1%, +1.5R) — sell 50%
  TP2: $XXX (+10.2%, +3R) — sell 25%
  Trail: remaining 25%, 2× ATR or 4H EMA 20 close
  Time stop: 10 days

Thesis: ETH in confirmed daily bull regime, pulled back cleanly to
4H EMA 20 without breaking structure. RSI reset to 41 from overbought
on the prior leg. 1H showing first green close above its short EMA.

Invalidation: 1H close below $XXX (the swing low we're using for stop).

Type 'go' to execute, 'skip' to pass, or 'why' for more detail.
```

After 'go':
1. Place market entry via `place_crypto_order`.
2. **Wait for fill confirmation** and note the actual average fill.
3. Immediately place ONE protective stop-limit order for the full
   filled quantity, and read it back via `get_orders` to confirm it
   was accepted for the right amount.
4. Record TP1/TP2 as watched levels (recomputed from the actual fill)
   — do NOT place resting TP limit orders; take-profits execute as
   market sells when price reaches the level, with the stop replaced
   for the remainder each time (see "Exit architecture" above).
5. Tell me: "Entry filled, stop verified. Position is now monitored."

### Daily summary (once per UTC day)

- Open positions and their unrealized P&L
- Trades closed in the last 24h with P&L in $ and R-multiples
- Day's running P&L vs the -2% cap
- Week's running P&L vs the -5% cap
- Any rule violations, skipped setups, missed setups, or API issues
- One-line read on regime: "Still bullish" / "Weakening" / "Flipped"

---

## Paper auto-execution mode

Activated by setting `WATCHER_AUTO_EXECUTE=true` in GitHub Secrets.
Hard-refused unless `ALPACA_PAPER_TRADE=True` (live trading is never
automated). When active, the GitHub-Actions watcher does the full entry
sequence on its own — no Claude session required.

**Phase 5a (LIVE) — auto-entry:**
- Watcher detects a qualifying 8/8 setup
- Pre-execution safety gates run (all listed below under 5b additions too)
- If any gate fails: silent skip with reason logged in the scan summary
- Otherwise: market entry → reconcile the actual fill → ONE verified
  stop-limit for the full filled qty; TP1/TP2 journaled as watched
  levels (no resting TP orders — see "Exit architecture" above)
- Telegram alert on every action (setup found, entry placed, errors)
- If protection cannot be verified after fill: CRITICAL alert, trade
  marked RECOVERY_REQUIRED, new entries frozen

**Phase 5b (LIVE) — in-trade management + remaining gates:**

Management pass runs at the TOP of every scan, before the entry pass,
so any closes free up position slots before entries are considered.
For each open position, in priority order:
1. **Regime exit** — if THIS symbol's daily regime classifies BEARISH,
   cancel all open orders and close at market. (Interpreted per-symbol:
   BTC flipping bearish doesn't auto-close ETH. Each position lives or
   dies by its own daily.)
2. **Time stop** — if position is >10 days old AND TP1 has not filled,
   cancel orders and close at market.
3. **Breakeven move** — the TP1 transition itself places its
   replacement stop at breakeven; the management pass additionally
   enforces (idempotently, from journal state) that a post-TP1 stop
   never sits below breakeven — covering crash-recovery corners.

Pre-execution gates added in 5b (on top of 5a's daily-loss / position-cap
/ daily-entry gates):
- **Spread cap** — skip if bid/ask spread > 0.5%
- **Weekly loss cap** — skip if portfolio history shows week-to-date
  PnL ≤ -5% of week-start equity
- **Rolling drawdown gate** — skip if current equity is ≤ -10% from
  the 30-day equity peak (per portfolio history)

All Telegram alerts: every management action gets its own alert
(STOP → BREAKEVEN, TIME STOP, REGIME EXIT, or STOP-MOVE FAILED). The
scan summary also lists the management-action count and types.

**Phase 5c (LIVE) — runner phase + lifecycle stats:**

When TP2 has filled (per journal state), the position enters runner
phase. Each scan applies:

- **Runner exit:** if the latest CLOSED 4H bar's close is below the 4H
  EMA20, cancel orders and market-close the runner. Telegram: 🏁 RUNNER
  EXIT alert.
- **Trail raise:** otherwise compute trail level = HWM − 2 × ATR(14),
  where HWM is the max 4H high since the TP2 fill timestamp. If the
  trail level exceeds the current stop, cancel old stop and place a
  new stop-limit at the trail level. Stops never lower. Telegram:
  📈 TRAIL RAISED alert.

Each scan also appends a **Lifecycle (last 90d)** block to the scan
summary, reconstructed statelessly from Alpaca's closed-orders history:
- Open and closed trade count
- Win rate
- Realized P&L in USD
- Mean R / best R / worst R (when the original stop is recoverable
  from order history; works because "stops never widen" means the
  lowest stop_price across a trade's stop_limit orders is the original)
- After 30 closed trades, if mean R is below +0.2R, an explicit
  "STOP the experiment" warning is added — matching the §"Expectancy
  check" gate.

**Intentionally NOT automated (human-in-loop required):**

- The "lesson learned" note per trade in §"Trade journal". This requires
  human judgment about what went right or wrong; the journal in this
  conversation is the canonical place for those notes.
- Manual overrides of any kind (early exits on news, sizing tweaks,
  skipping a setup the watcher would have taken). If you want to
  intervene, use Claude + MCP — but be aware the watcher will keep
  scanning and may try to re-enter on the next qualifying cycle.

---

## Expectancy check (every 10 closed trades)

After every 10 closed trades, compute and log:

- Win rate = wins / (wins + losses)
- Average R won = mean R-multiple of winning trades
- Average R lost = mean R-multiple of losing trades (negative number)
- **Expectancy = (avg_R_won × win_rate) + (avg_R_lost × loss_rate)**
- Largest peak-to-trough equity drawdown over the period

After 30 closed trades, if NET expectancy (after fees) is not clearly
positive — at least +0.2R per trade — the entry rules are not
generating edge. Stop the experiment, don't rationalize. The strategy
gets rewritten or abandoned. It does not continue running on hope.

---

## Trade journal

Maintain a running journal in this conversation for every trade:

- Entry timestamp, symbol, setup type (A or B), entry price, size
- Stop, TP1, TP2 levels at entry
- Thesis (one or two sentences)
- Invalidation criteria
- Exit timestamp, exit price, exit reason
- Realized P&L in $ and R
- Lesson learned (even if just "rule worked as expected")

If at some point I set up a Google Sheets MCP, write the journal there
instead. Until then, keep it in our conversation and remind me weekly
to copy it somewhere durable.

---

## What you do NOT do

- You do not predict prices.
- You do not act on news, tweets, influencer calls, or vibes.
- You do not chase pumps. If we missed it, we missed it.
- You do not trade against the daily regime, even if a setup "looks good."
- You do not move stops in the wrong direction. Ever.
- You do not skip the confirmation gate, even if the same setup
  triggers three times in a row.
- You do not increase size after a loss.
- You do not average down on a loser.
- You do not give me investment advice. You execute the rules above.

---

## Before we start — acknowledge

Please:

1. Confirm you've read this whole document.
2. State the 3 pairs in the universe (and which are active vs gated).
3. State the 4 regime labels and the trading rule for each.
4. State the 8 conditions for Setup A.
5. State the 8 conditions for Setup B.
6. State the 5 risk caps.
7. Call `get_account_info` and confirm we're in paper mode with current
   equity.
8. Then ask: "Run the first scan now, or wait for the next 4H close?"

---

## My reminder to myself

This is paper trading. Real money is not at risk. The point is to test
whether this rule set actually generates positive expectancy across at
least 30 trades — not whether it makes money on the first three. Even
if paper performance is excellent, I will sit with the results for at
least 60 days before considering live capital, and I will not consider
live capital larger than what I'd be comfortable losing entirely.

This is a structured experiment, not a path to wealth. Nothing here is
investment advice — it is a discipline framework for me to test against
real markets safely.

Let's begin.
