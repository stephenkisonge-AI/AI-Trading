# Baseline — fix/paper-execution-safety

Recorded 2026-07-10 before any code modification on this branch.

## Snapshot

- **Commit hash:** `8ce297d309b1916762e658d8447288f6dbec562a` (`master`, clean working tree, in sync with `origin/master`)
- **Baseline test count:** `pytest -q` → **267 passed**, 1 warning, ~3.5s
- **Python:** local venv 3.14.6; GitHub Actions workflows pin 3.11
- **Deployment decision (Phase 0):** **Option B** — GitHub Actions with a private
  state repository. The local machine is not guaranteed to stay online.

## Current deployment method

GitHub Actions on this public repository (free tier). No self-hosted runners.

| Workflow | Trigger | Purpose |
|---|---|---|
| `watcher.yml` (crypto swing) | GH cron `17 */4 * * *` + `0 14 * * *` fallback; `workflow_dispatch` | scan + alert + (when enabled) auto-execute |
| `day-watcher.yml` (equity day) | `repository_dispatch` from external cron-job.org every 5 min during US market hours; backup native `*/5 13-20 * * 1-5`; `workflow_dispatch` | scan + alert + (when enabled) auto-execute |
| `refresh-day-calendars.yml` | weekly cron | commits `state/earnings.json`, `state/econ_events.json` |

Both trading workflows already use `concurrency` groups with
`cancel-in-progress: false`.

## Entry-scan cadence

- **Crypto strand:** every 4 hours at :17 UTC (6 scans/day) plus a 14:00 UTC
  fallback that fires only if no scheduled run succeeded that day.
- **Day strand:** every 5 minutes during US market hours via external cron
  dispatch (GH native `*/5` schedule is best-effort and drops most ticks).

## Position-management cadence

- **Crypto strand:** management runs at the **top of each 4-hour scan**
  (`manage_open_positions()` before the entry pass). There is no independent,
  more frequent management schedule. Worst-case management latency ≈ 4 h.
- **Day strand:** managed within each 5-minute tick.

## Current exit-order lifecycle (crypto)

`place_entry_bundle()` in `src/trader.py` places, per qualifying setup:

1. Market BUY (GTC), sized from `compute_position_size()` (1% equity risk,
   $500 notional cap, 8% max stop distance).
2. Poll for fill up to 60 s (`_wait_for_fill`). **Timeout raises and the
   bundle aborts — filled/partial quantity is not re-checked.**
3. Stop-limit SELL for 100% of filled qty at the structural stop; limit =
   stop × (1 − 0.5%) (`_STOP_LIMIT_SLIPPAGE_PCT = 0.005`).
4. TP1 limit SELL for 50% of filled qty at +1.5R.
5. TP2 limit SELL for 25% of filled qty at +3R.
6. Final 25% has no resting order — runner phase trails HWM − 2×ATR(14) on
   4H bars, exits on 4H close < EMA20.

Management transitions (`manage_open_positions()`): regime exit (daily
BEARISH → cancel all + market close), 10-day time stop (if TP1 unfilled),
breakeven stop move after TP1, trail raise / runner exit after TP2.

Note: the **simultaneous resting sell orders (stop 100% + TP1 50% + TP2 25%)
can total 175% of the position** — Alpaca tolerates this on crypto paper
today, but the design violates the sell-quantity invariant this branch will
introduce.

## Existing paper/live safety switches

- `ALPACA_PAPER_TRADE` env/secret; `_assert_paper_mode()` aborts the watcher
  unless it is exactly `"True"`; the trading client is constructed with
  `paper=True` from it.
- `WATCHER_AUTO_EXECUTE` (crypto) — `auto_execute_enabled()` requires the
  secret to be `"true"` **and** `ALPACA_PAPER_TRADE == "True"`; anything else
  = alerts-only. Live trading is never automated.
- `WATCHER_DAY_AUTO_EXECUTE`, `WATCHER_DAY_ENABLE_SHORTS` — day-strand
  equivalents (`day_auto_execute_enabled()`, `day_shorts_enabled()`).
- Risk gates before entry (crypto): daily loss cap −2%, weekly loss cap −5%,
  30-day rolling drawdown −10%, max 2 positions, BTC/ETH correlation rule,
  1 entry/day, 0.5% spread cap.

## Known state-persistence method

**None for trade state.** Every scan statelessly reconstructs trade lifecycle
from Alpaca closed-order history (90-day lookback, 500-order page). There is
no journal, no SQLite, no reconciliation artifact. `state/*.json` is
gitignored except the two committed calendar files owned by the
refresh-day-calendars workflow.

## Known order-recovery limitations

- Crypto orders carry **no client order IDs** → retries/duplicates cannot be
  detected. (Day strand stamps `DAY-{setup}-{symbol}-{unix_ts}` — precedent,
  but not deterministic per logical action and not used for recovery.)
- `_wait_for_fill` timeout treats the order as not filled; the unfilled or
  partially filled entry order is left open with no cancel and no
  position-protection pass.
- A partial bundle (entry filled, protective orders failed) only escalates a
  Telegram alert; nothing retries or freezes new entries.
- `_replace_stop` cancels the old stop **before** submitting the new one and
  does not wait for a confirmed terminal cancellation; a failed re-submit
  leaves the position unprotected until a later scan notices.
- `_cancel_open_orders` swallows cancel failures; close orders can race the
  async qty-hold release (mitigated by a 15 s poll, not by confirming
  terminal cancel state).
- Gates fail **open** on infra errors: spread-gate quote failure, weekly-loss
  and drawdown history failures all log to stderr and allow the entry.

## Known strategy-sampling limitations

- Setup A's 1H reclaim condition (`h1_green_close_reclaims_ema20`,
  `src/strategy.py:342-366`) inspects **only the single most recent closed 1H
  bar** — but the scan runs every 4 hours, so reclaims occurring on the other
  ~3 of every 4 one-hour bars are never observed.
- Pullback/interaction conditions evaluate closes only; candle-range (high/
  low) interaction with the EMA is not considered.
- The 14:00 UTC fallback means a dropped scheduler day is sampled once, not 6
  times.

## Auto-execution disabled before modification

- `WATCHER_AUTO_EXECUTE` repository secret set to `"false"` on 2026-07-10
  **before any code change on this branch**.
- Verified from dispatched dry-run
  [run 29122765946](https://github.com/stephenkisonge-AI/AI-Trading/actions/runs/29122765946)
  (2026-07-10 20:50 UTC): log line `[watcher] auto-execute enabled: False`;
  scan completed alerts-only; no secret values printed.
- Open crypto positions at time of freeze: **0**.
