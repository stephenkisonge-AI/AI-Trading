# Phase 6 — Replay Experiments Report

Date: 2026-07-13. Branch: `phase6-replay`. Data: Alpaca crypto bars,
replay window **2024-01-01 → 2026-07-13** (30.5 months), universe
BTC/ETH/SOL/LINK/AVAX. Harness: `src/replay.py`,
`scripts/phase6_replay.py`, `scripts/phase6_stop_replay.py`,
`scripts/phase6_supplement.py`; raw results in `docs/phase6/*.json`.

Phase 6's mandate (set when Phases 0–5 merged): quantify, before any
rule is frozen in, (1) the stop-limit offset candidates flagged in
`ExecConfig`, and (2) Variant B — promoting the Phase 5 telemetry
reclaim-window observer into a qualifying condition. Crypto
auto-execution stays `WATCHER_AUTO_EXECUTE=false` pending Phases 6–7.

---

## Executive verdict

1. **Experiment 1 — keep `stop_limit_offset_pct = 0.005`.** The
   current 0.5% band filled 99.4–100% of 350 replayed stop triggers
   with ~14 bps mean slippage. Tighter bands (0.1–0.25%) send 2–4% of
   events to the gap watchdog with 380–550 bps tails; wider bands buy
   nothing. No change recommended.
2. **Experiment 2 — Variant B works as a sampler but does not create
   edge.** It recovers the between-scan reclaims the live telemetry
   sees being missed (signals: 6 → 16 over the window, ≈2.7×), with
   per-trade expectancy statistically indistinguishable from the
   current rule — and both are **deeply negative** (≈ −1.5R net per
   trade). Do not adopt yet; there is nothing worth sampling more of
   until finding 3 is addressed.
3. **New finding — the strategy has no minimum stop distance, and
   fees eat it alive.** Replayed structural stops (recent 4H swing
   low) land 0.11–0.9% below entry in 9 of 16 trades. At 0.25%/side
   fees on the $500-capped notional, that is 0.55–4.5R of fee drag per
   trade. The one trade that hit **both TP1 and TP2 still lost money
   net** (−0.70R on a 0.20% stop). This is structural: it holds under
   the optimistic fill bound and does not depend on Variant B.
4. **Keep `WATCHER_AUTO_EXECUTE=false`.** The replayed strategy fails
   its own expectancy gate (Crypto Strategy.md: stop the experiment if
   net expectancy < +0.2R). Phase 7 should decide an entry rework
   (fee-aware stop floor at minimum), not re-arm execution.

---

## Method and fidelity

- Every 4H boundary in the window is replayed through the
  **production evaluator** (`src.strategy.evaluate_setup_a`) on
  watcher-faithful frames: trailing **249 completed bars** per
  timeframe (`get_bars(limit=250)` minus the in-progress candle),
  indicators attached by the production `add_indicators`. 26,069
  scans across the 5 symbols (SOL scans start later — the classifier
  needs 200 completed daily bars of Alpaca history first).
- **Variant definitions.** EXACT = current cond5 (the single most
  recent closed 1H bar is a strict green EMA20 reclaim). WINDOW =
  Variant B (a strict reclaim on any of the 4 completed 1H bars since
  the previous scan, not invalidated by a later 1H close back below
  its own EMA20 — exactly the Phase 5 `reclaim_window_hit` observer).
- **Books.** Per-symbol sequential: flat → enter on first qualifying
  scan → manage to close → resume. Cross-symbol caps (2 positions,
  BTC/ETH one slot, 1 entry/day) not applied; they affect both
  variants alike.
- **Fill model (conservative).** Entry at the signal bar's close.
  Stops fill on any 1H touch (gap opens fill at the open); TPs need a
  1H **close** at/through the level (a wick is not an executable bid —
  matches the stale-quote rule). Same-bar conflicts: stop first.
  Exits per the strategy doc: TP1 +1.5R sell 50% → breakeven stop;
  TP2 +3R sell 25% → runner trails HWM − 2×ATR(14) on 4H, exits on 4H
  close < EMA20; 10-day time stop; per-symbol BEARISH regime exit.
  Fees 0.25%/side.
- **Sensitivity bound.** Everything was re-run with the optimistic
  model (wick-touch TPs, booked before same-bar stops). Conclusions
  hold at both bounds.

## Experiment 2 — Variant B reclaim window

Signals and outcomes (conservative model; optimistic in parentheses):

| | EXACT (current rule) | WINDOW (Variant B) |
|---|---|---|
| Signals / trades | 6 | 16 (10 found only via window) |
| Win rate (net) | 0% (0%) | 12.5% (12.5%) |
| Mean R gross | −0.54 (−0.54) | −0.61 (−0.50) |
| Mean R net | **−1.55** (−1.55) | **−1.51** (−1.40) |
| Total R net | −9.3 | −24.1 |
| Exit mix | 6 stop, 1 tp1, 1 tp2 | 16 stop, 3 tp1, 1 tp2 |

Per-symbol signals (exact/window): BTC 3/4, ETH 1/7, SOL 1/2,
LINK 0/1, AVAX 1/2.

**Signal scarcity is not the regime's fault.** Share of days
Setup-A-eligible (BULLISH or IMPROVING_NEUTRAL, production
classifier): BTC 67.7%, ETH 48.5%, LINK 37.1%, SOL 32.5%, AVAX 30.8%.
BTC was eligible two-thirds of a 30-month window and produced **3**
exact signals. The binding constraint is the pullback ∧ RSI ∧ reclaim
∧ volume conjunction — consistent with the live Phase 5 funnel. At
this rate the strategy's own 30-closed-trades expectancy checkpoint
would take a decade to reach under the current rule.

**Fee drag vs stop distance** (window book, conservative; sorted by
stop distance):

| Symbol | Signal | Stop dist | R gross | R net | Fees (R) |
|---|---|---|---|---|---|
| ETH | 2024-05-30 16:00 | 0.11% | −1.00 | −5.48 | 4.48 |
| BTC | 2025-07-06 12:00 | 0.20% | **+1.75** | **−0.70** | 2.45 |
| BTC | 2024-05-28 20:00 | 0.29% | −1.00 | −2.70 | 1.70 |
| BTC | 2024-03-15 20:00 | 0.40% | −1.00 | −2.25 | 1.25 |
| BTC | 2024-10-09 16:00 | 0.69% | −1.00 | −1.72 | 0.72 |
| ETH | 2024-01-21 08:00 | 0.80% | −1.00 | −1.62 | 0.62 |
| SOL | 2025-09-07 04:00 | 0.90% | −1.00 | −1.55 | 0.55 |
| ETH | 2025-09-08 12:00 | 0.90% | +0.75 | +0.19 | 0.56 |
| ETH | 2025-09-21 20:00 | 1.26% | −1.00 | −1.39 | 0.39 |
| ETH | 2025-05-25 20:00 | 1.49% | +0.75 | +0.41 | 0.34 |
| AVAX | 2024-03-31 12:00 | 1.69% | −1.00 | −1.29 | 0.29 |
| SOL | 2025-09-20 08:00 | 1.80% | −1.00 | −1.27 | 0.28 |
| LINK | 2025-09-17 08:00 | 1.84% | −1.00 | −1.27 | 0.27 |
| ETH | 2024-01-07 08:00 | 2.62% | −1.00 | −1.19 | 0.19 |
| ETH | 2024-01-06 00:00 | 3.16% | −1.00 | −1.16 | 0.16 |
| AVAX | 2024-03-23 08:00 | 4.22% | −1.00 | −1.12 | 0.12 |

Two structural problems, one root cause (stop = most recent 4H swing
low with only an ATR *cap*, no floor):

- **Fee drag explodes on tight stops.** The doc's fee-awareness note
  assumed ~3% stops (≈0.17R drag); the replay's median stop distance
  is 0.9%, where drag is 0.55R+.
- **Sub-1% stops sit at 4H noise level** and get swept: 13 of 16
  trades were clean −1R stop-outs (gross win rate 18.75%, far below
  the ~60% that a ±0.75R-ish payoff profile needs).

A 2% stop-distance floor would have skipped 13 of the 16 trades —
including every fee-catastrophe — but the 3 surviving trades were
still −1R gross stop-outs. **The floor is necessary but not
sufficient**: Setup A's entry timing produced negative expectancy
even gross of fees (−0.5 to −0.6R mean) in this window.

**Variant B verdict:** its diagnosis was correct (live telemetry:
55.6% of reclaims missed between scans; replay: 10 of 16 signals only
visible through the window) — but adopting it now would only triple
the frequency of a losing trade. Park it; re-test after the entry
rework, where its sampling fix may genuinely matter.

## Experiment 1 — stop-limit offset sweep

351 stop-trigger events (16 from replayed trades — deduplicated
across variants — plus 335 synthetic first-breach-of-structure events
for power), each replayed on 1-minute bars per offset × watchdog
delay. Slippage measured against a fill at the stop trigger.

Watchdog delay 30 min (swing-manager cadence upper bound):

| Offset | Limit-fill rate | Mean slip (bps) | p90 | Max |
|---|---|---|---|---|
| 0.10% | 96.3% | 11.0 | 10 | **510** |
| 0.25% | 97.7% | 12.9 | 25 | 200 |
| **0.50% (current)** | **99.4%** | **14.2** | **50** | **118** |
| 1.00% | 100% | 15.1 | 43 | 100 |
| 2.00% | 100% | 16.3 | 41 | 200 |
| 3.00% | 100% | 15.9 | 40 | 300 |

Same shape at 15- and 45-min delays (0.5% reaches 100% fill at 45
min). All 16 real trade-stop events filled via the limit at every
offset (mean realized slippage 4–8 bps) — the tail risk lives in the
violent structure-break events, which is precisely what the band
must survive.

**Reading:** 0.5% sits at the knee. Tighter bands trade ~3 bps of
mean slippage for a 2–4% chance of an unprotected position and a
5–10× worse tail — the exact failure mode the Phase 3 interim report
flagged. Wider bands only raise the worst acceptable fill without
improving the mean. **Recommendation: keep 0.005; no code change.**

## Caveats

- 6 and 16 trades are small samples; treat the expectancy signs (and
  the fee arithmetic, which is exact) as the finding, not the third
  decimal. The fee/stop-floor conclusion holds trade-by-trade.
- Bar-model fills can't see intra-bar sequencing; the
  conservative/optimistic bounds bracket it and agree.
- Alpaca's crypto venue is thin; its prints (and therefore both the
  live watcher and this replay) may differ from consolidated-market
  prices. Production trades this venue, so the replay is faithful to
  production.
- Books are per-symbol; cross-symbol caps would have reduced trade
  counts slightly, same direction for both variants.
- Latent production wrinkle found while building the harness (not
  fixed here): `_drop_in_progress_candle` drops the last raw bar
  blindly. If a thin symbol has zero trades so far in the current
  hour, Alpaca returns no partial bar and the watcher silently
  evaluates one completed bar behind. Worth a guard in a future
  hygiene pass.

## Proposed Phase 7 agenda

1. **Freeze:** `stop_limit_offset_pct` stays 0.005 (Experiment 1).
2. **Hold:** `WATCHER_AUTO_EXECUTE` stays `false` — the replayed
   strategy fails its own +0.2R expectancy gate.
3. **Decide (user):** entry rework for Setup A. Minimum viable
   change: a fee-aware stop-distance floor (e.g., skip entries with
   structural stop < 2% of entry, where round-trip friction ≤ 0.25R).
   The replay shows this is necessary but not sufficient — the
   deeper question is whether pullback timing as specified has edge
   at all. Options: widen stops to a floor of ~1×ATR (changes R
   geometry), loosen the reclaim/volume conjunction (Variant B alone
   is not enough), or retire Setup A and rely on Setup B. Note Setup
   B was NOT replayed (Phase 6's mandate covered Setup A's Variant B
   and the stop offset); replaying it through this harness is cheap
   and should precede any reliance on it.
4. **Re-test:** whatever rework is chosen, re-run this harness (one
   command per experiment) before touching any production rule.
5. **Continue Phase 5 telemetry** — live funnel data agrees with the
   replay's bottleneck shape and remains the check that live behavior
   matches replayed behavior.
