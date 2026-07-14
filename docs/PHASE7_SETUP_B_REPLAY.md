# Phase 7 — Setup B Replay Report

Date: 2026-07-14. Data: Alpaca crypto bars, replay window
**2024-01-01 → 2026-07-13** (30.5 months), universe
BTC/ETH/SOL/LINK/AVAX — identical bars (same cache) as Phase 6.
Harness: the Phase 6 machinery with `--setup B`
(`scripts/phase6_replay.py --setup B`, sensitivity via
`scripts/phase6_supplement.py --exp2 docs/phase6/setup_b_results.json
--variant exact`). Raw results: `docs/phase6/setup_b_results.json`,
`docs/phase6/setup_b_supplement.json`.

Mandate (Phase 6 report, agenda item 3): Setup B was never replayed;
replaying it must precede any decision to retire Setup A and rely on
Setup B. This report closes that gap.

---

## Executive verdict

1. **Setup B as specified fails the +0.2R expectancy gate, worse than
   Setup A in R terms.** 26,044 scans → 131 qualifying signals → 85
   sequential-book trades: 20% win rate, mean **−3.88R net** per trade
   (−0.19R gross), total −330R. `WATCHER_AUTO_EXECUTE` stays `false`.
2. **Same root cause as Setup A, amplified: no minimum stop
   distance.** Setup B's stop is the breakout level, and entry (the 4H
   close after a retest) is usually a hair above it: median stop
   distance **0.62%** of entry, 89% of trades under 2%, minimum
   **0.0075%** (≈1 basis point). At 0.25%/side fees the two worst
   trades lost **−67.5R and −61.3R net on −1R gross stop-outs**, and
   one trade that completed the full TP1→TP2 ladder (+1.50R gross)
   still lost −16.0R net.
3. **Unlike Setup A, the entry timing itself shows edge once stops are
   structural.** Full-book gross expectancy straddles zero across fill
   bounds (−0.19R conservative / +0.12R optimistic — Setup A was
   −0.5R at both). The **stop ≥ 2% subset: n=9, 44.4% win, +0.47R
   gross, +0.28R net — identical under both fill models** — and all
   four winners ran the complete TP1→TP2→runner ladder. Setup A's
   floor survivors were all −1R stop-outs; Setup B's carry the book's
   entire positive tail.
4. **The viable-frequency problem is real but not fatal.** 9
   floor-passing trades in 30.5 months ≈ 3.5/year across the universe
   (vs Setup A's ~1). The strategy's 30-closed-trades checkpoint would
   take ~8.5 years at that rate — signal-only live sampling plus a
   wider-stop rework (below) are the levers, not auto-execution.

## Headline numbers

| | Full book | stop ≥ 1% | stop ≥ 2% |
|---|---|---|---|
| Trades | 85 | 26 | 9 |
| Win rate (net) | 20.0% | 34.6% | 44.4% |
| Mean R gross | −0.19 (+0.12) | +0.07 (+0.14) | **+0.47** (+0.47) |
| Mean R net | **−3.88** (−3.57) | −0.25 (−0.18) | **+0.28** (+0.28) |
| Total R net | −329.8 | −6.4 | +2.5 |

Conservative model; optimistic in parentheses. Exit mix (full book):
83 stop (incl. post-TP1 breakeven stops), 2 gap-open, 25 tp1, 16 tp2 —
**zero time-stop or regime exits**: breakout trades resolve fast in
the BULLISH-only regime Setup B requires.

## Per-symbol (conservative)

| Symbol | Signals | Trades | Win | Mean R gross | Mean R net | Total R net |
|---|---|---|---|---|---|---|
| BTC | 65 | 36 | 16.7% | −0.22 | −5.17 | −186.2 |
| ETH | 27 | 21 | 19.0% | −0.36 | −5.15 | −108.1 |
| SOL | 14 | 8 | 37.5% | +0.46 | −0.67 | −5.3 |
| LINK | 19 | 15 | 6.7% | −0.52 | −2.06 | −30.8 |
| AVAX | 6 | 5 | 60.0% | +0.65 | **+0.14** | +0.7 |

The damage concentrates in BTC/ETH, whose breakout levels sit
proportionally closest to price (tightest stops → worst fee drag in
R). The thinner alts, with wider structural distance, are near or
above water even net.

## Stop-distance distribution

min 0.0075% · p25 0.31% · **median 0.62%** · p75 1.17% · p90 2.02% ·
max 6.11%. Under 2%: 76 of 85 trades (89%). Setup A's median was 0.9%
— Setup B is structurally tighter because the retest, by definition,
brings entry back to within ~0.5% of the level before the
confirmation candle.

Worst five trades, all stop-distance artifacts:

| Symbol | Signal | Stop dist | R gross | R net |
|---|---|---|---|---|
| ETH | 2024-02-06 16:00 | 0.008% | −1.00 | −67.50 |
| BTC | 2025-01-18 20:00 | 0.008% | −1.00 | −61.29 |
| BTC | 2024-12-15 20:00 | 0.016% | −3.91 | −35.19 |
| BTC | 2024-08-24 04:00 | 0.029% | **+1.50** | **−16.03** |
| LINK | 2024-12-04 04:00 | 0.033% | −1.00 | −16.02 |

## The stop ≥ 2% book (all 9 trades, both fill models agree)

| Symbol | Signal | Stop dist | R gross | R net | Exits |
|---|---|---|---|---|---|
| AVAX | 2024-01-11 08:00 | 2.04% | +1.50 | +1.25 | tp1→tp2→trail |
| LINK | 2024-02-29 08:00 | 2.25% | −1.00 | −1.22 | stop |
| AVAX | 2024-03-11 12:00 | 4.68% | +2.27 | +2.16 | tp1→tp2→trail |
| ETH | 2024-12-04 20:00 | 2.18% | −1.00 | −1.23 | stop |
| LINK | 2024-12-12 16:00 | 3.07% | −1.00 | −1.16 | stop |
| LINK | 2024-12-13 00:00 | 6.11% | −1.00 | −1.08 | stop |
| ETH | 2025-01-31 16:00 | 2.60% | −1.00 | −1.19 | stop |
| ETH | 2025-08-07 12:00 | 2.51% | +2.38 | +2.18 | tp1→tp2→trail |
| SOL | 2025-09-08 12:00 | 2.02% | +3.06 | +2.81 | tp1→tp2→trail |

Note the 2% threshold was proposed in the Phase 6 report (from Setup
A's fee arithmetic) before this replay ran — it is not fitted to this
data — but n=9 is n=9: treat the sign as the finding, not the +0.28.

## Method notes and caveats

- Same production evaluator (`src.strategy.evaluate_setup_b`), same
  watcher-faithful 249-bar frames, same exit simulator and fee model
  as Phase 6 (the strategy doc's management rules are shared by both
  setups). Setup B has one variant; results are booked under the
  harness's "exact" key.
- Setup B qualification is a persisting *state* (breakout within the
  last 10 4H bars + any prior retest), unlike Setup A's single-bar
  reclaim *event* — hence 131 signals. The per-symbol book absorbs
  most of that (one open position at a time) but re-enters on the
  same still-qualified breakout after a stop-out. Production's
  cross-symbol caps (2 positions, 1 entry/day, BTC/ETH one slot)
  would throttle some of these; e.g. the ETH 2024-02-06 16:00 (−67.5R)
  and 20:00 pair could not both fire in production.
- Small-sample warnings apply throughout; the fee arithmetic is exact
  and holds trade-by-trade.

## Where this leaves Phase 7

1. **Both setups fail as specified; the shared defect is the missing
   stop-distance floor.** A fee-aware entry gate — skip if
   (entry − stop)/entry < 2% — should be added for **both** setups
   before anything else. It removes every catastrophe in both replays
   and costs nothing that was worth keeping (Setup A floor survivors:
   3 trades, all losers; Setup B: keeps the entire positive tail).
2. **Retiring Setup A and relying on floor-gated Setup B is now
   evidence-supported** — B has gross edge where stops are
   structural, A does not anywhere — but at ~3.5 trades/year it
   cannot clear the 30-trade checkpoint on paper alone in reasonable
   time.
3. **A stop-widening variant is worth one more replay:** for Setup B,
   stop = min(entry − 1.0×ATR, breakout level) instead of the level
   itself would convert tight-stop signals into tradeable geometry
   rather than skipping them (more samples, smaller R per unit
   notional). That is a one-experiment rerun through this harness.
4. **Keep `WATCHER_AUTO_EXECUTE=false`.** Nothing here clears the
   +0.2R gate at a usable sample size. The path to more samples is
   signal-only alerting of floor-gated Setup B plus continued Phase 5
   telemetry, not live execution.
