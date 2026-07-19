# Phase 8 — Universe Expansion Report

Date: 2026-07-19. Window: **2024-01-01 → 2026-07-19**. Harness: the
Phase 6/7 replay (`scripts/phase6_replay.py --setup B`) over every
Alpaca-tradable /USD pair not already in the universe; the production
evaluator includes the 9-condition checklist (fee floor active), so
every replayed signal is one the live watcher would alert on. Raw
results: `docs/phase8/universe_results.json`,
`docs/phase8/universe_supplement.json`.

Companion decision (same date): **Setup A retired from live scanning**
(`SCAN_SETUP_A = False` in src/watcher.py) — Phase 6/7 evidence, spec
retained for a future rework.

## Screen

36 tradable /USD pairs − current 5 − stable/pegged (USDC, USDT, USDG,
PAXG) = 27 candidates. 11 lack the 200 completed daily bars the regime
classifier needs (listed ~Feb 2026 or later): ADA, ARB, BONK, FIL,
HYPE, LDO, ONDO, POL, RENDER, WIF, SKY — **excluded, revisit when
history accrues** (~6–9 months). 16 replayed: AAVE, BAT, BCH, CRV,
DOGE, DOT, GRT, LTC, PEPE, SHIB, SUSHI, TRUMP, UNI, XRP, XTZ, YFI.

## Replay results (79,858 scans, 41 gate-passing signals, 27 trades)

Net R per trade, conservative / optimistic fill bounds:

| Coin | Signals | Trades | Net R (cons) | Net R (opt) | Verdict |
|---|---|---|---|---|---|
| **DOGE** | 7 | 5 | **+0.96** | **+0.82** | **PASS** — 60% win, 3 full TP ladders, trades span Feb-24→Feb-25 |
| **UNI** | 1 | 1 | **+0.59** | **+0.59** | **PASS** (thin n) |
| **XTZ** | 1 | 1 | **+1.53** | **+1.53** | **PASS** (thin n) |
| XRP | 14 | 7 | −0.06 | +0.19 | FAIL by a hair at the conservative bound — top revisit candidate |
| CRV | 6 | 3 | −0.01 | −0.01 | FAIL (breakeven, negative both bounds) |
| BCH | 4 | 2 | −0.29 | −0.29 | FAIL |
| BAT | 2 | 2 | −1.13 | +0.62 | FAIL — outcome flips on fill model = unreliable |
| AAVE, DOT, GRT, SUSHI | 1–3 | 1–3 | −1.15…−1.23 | same | FAIL (clean stop-outs) |
| LTC, PEPE, SHIB, TRUMP, YFI | 0 | 0 | — | — | no gate-passing signals at all |

Inclusion gate (pre-registered in the plan): non-negative net
expectancy at BOTH fill bounds. DOGE, UNI, XTZ pass. XRP misses by
0.06R at the conservative bound despite being the most active
candidate (14 signals) — the gate is the gate; flagged for revisit
with live-signal evidence.

## Portfolio effect

New `CRYPTO_SYMBOLS`: BTC, ETH, SOL, LINK, AVAX **+ DOGE, UNI, XTZ**.

- Projected floor-passing alert rate: ~3.5/yr (current 5) + ~3.5/yr
  (DOGE 2.7 + UNI 0.4 + XTZ 0.4) ≈ **7/yr — double the old rate**.
  Below the aspirational 10/yr because 13 of 16 candidates failed the
  quality gate — that is the gate doing its job, not a defect. The 11
  too-new listings are the natural growth path.
- Pooled net expectancy of everything the gate admits (9 current-
  universe floor trades + 7 new-coin trades): ≈ **+0.59R** —
  comfortably above the +0.2R bar. Small samples throughout; signs,
  not third decimals.
- Frequency numbers are upper bounds: per-symbol replay books ignore
  the cross-symbol caps (2 concurrent positions, BTC/ETH one slot,
  1 entry/day — all unchanged).

## Observation worth recording (no action taken)

Applying the same per-coin gate retroactively to the CURRENT universe:
SOL and AVAX pass, ETH is breakeven-ish, **LINK is −1.15R at both
bounds (n=3) and BTC produced zero floor-passing signals in 30
months.** Removing incumbents was out of the approved scope; if the
live funnel confirms this pattern, a future phase should consider
demoting LINK/BTC. Scanning them costs nothing meanwhile.

## Verification

- 485-test suite green after the Setup A retirement; universe change
  covered by the same suite (watcher iterates `CRYPTO_SYMBOLS`).
- Live check: one `workflow_dispatch` watcher run over the 8-coin
  universe — clean scan, Setup A reported `retired`, no errors.
- Ongoing: Phase 5 funnel telemetry now accumulates per-coin evidence
  for the XRP/LINK/BTC revisit questions.
