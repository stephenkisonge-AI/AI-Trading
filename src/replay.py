"""Phase 6 — historical replay of the crypto swing strategy.

Analysis-only: nothing here is imported by the watcher, trader, or swing
manager. The replay re-runs the PRODUCTION evaluator
(src.strategy.evaluate_setup_a) over historical bars, slicing at each
4-hourly scan exactly the frames the watcher would have seen (the
trailing 249 completed bars per timeframe — get_bars(limit=250) minus
the in-progress candle that _drop_in_progress_candle removes), then
simulates trade outcomes with the strategy-doc exit rules so the two
Setup A entry variants can be compared on the same history:

    EXACT  — current production rule: the single most recent closed 1H
             bar must be the strict EMA20 reclaim (cond5).
    WINDOW — Variant B: a strict reclaim on ANY of the 4 completed 1H
             bars since the previous scan qualifies, unless a later
             completed 1H close fell back below its own EMA20. This is
             precisely what the Phase 5 telemetry observer
             (reclaim_window_hit) records, promoted to a qualifying
             condition.

Simulator assumptions (kept deliberately conservative, and identical
for both variants so comparison bias mostly cancels):
  * Entry at the close of the signal 4H bar (production enters at
    market ~17s-2min after the bar closes; slippage is capped at 0.5%
    by ExecConfig and ignored here).
  * The broker-held stop fills AT the stop trigger (low <= stop). The
    cost of the stop-limit band gapping through is Experiment 1's
    question, quantified separately by scripts/phase6_stop_replay.py
    from the stop-hit events this simulator emits. A 1H bar OPENING
    at/below the stop is recorded as a gap-open and fills at that open.
  * App-managed TPs fire only when a 1H CLOSE reaches the level — a
    candle high is never treated as an executable bid (matches the
    stale-quote rule in Crypto Strategy.md). Fill at the TP level.
  * Same-bar conflicts resolve pessimistically: stop before TP.
  * Regime exits are evaluated once per completed UTC day (daily bars
    only change then) and fill at the open of the first 1H bar of the
    new day. Time stop (10 days without TP1) fills at that bar's close.
  * Runner phase (post-TP2): on each completed 4H bar, exit at its
    close if close < its EMA20, else trail stop to HWM - 2*ATR(14),
    never lowering. HWM = highest 4H high since the TP2 fill.
  * Fees: 0.25% of notional per side, both entry and every exit
    tranche. R is reported gross and net of fees.

Known fidelity gaps (documented, accepted for Phase 6):
  * _drop_in_progress_candle drops the last RAW bar; if a thin symbol
    had zero trades so far in the current hour Alpaca returns no
    partial bar and production silently evaluates one completed bar
    behind. The replay assumes the partial bar always existed.
  * Production's 1-entry/day, 2-position, and BTC/ETH correlation caps
    span symbols; the replay books are per-symbol sequential (flat ->
    enter -> manage to close -> resume scanning). Cross-symbol slot
    contention affects both variants alike.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.indicators import add_indicators
from src.strategy import (
    _find_swing_lows,
    classify_regime,
    evaluate_setup_a,
)

# Production frame length: get_bars(limit=250) then drop the in-progress
# candle -> 249 completed bars per timeframe at every scan.
FRAME_LEN = 249

# Strategy-doc exit parameters (Crypto Strategy.md — "Stops, targets,
# and trade management"). Kept as module constants so the report can
# cite them; NOT tunables of this experiment.
TP1_R = 1.5          # +1.5R -> sell 50%
TP2_R = 3.0          # +3R   -> sell 25%
TP1_FRAC = 0.50
TP2_FRAC = 0.25
RUNNER_TRAIL_ATR = 2.0
TIME_STOP_DAYS = 10
FEE_PCT = 0.0025     # per side, of notional

_4H = timedelta(hours=4)
_1H = timedelta(hours=1)


def scan_times(start: datetime, end: datetime) -> list[datetime]:
    """Every 4H boundary (00/04/08/12/16/20 UTC) in [start, end]."""
    t = start.astimezone(timezone.utc).replace(minute=0, second=0,
                                               microsecond=0)
    while t.hour % 4 != 0:
        t += _1H
    if t < start:
        t += _4H
    out = []
    while t <= end:
        out.append(t)
        t += _4H
    return out


def frames_at(daily: pd.DataFrame, h4: pd.DataFrame, h1: pd.DataFrame,
              t: datetime) -> tuple[pd.DataFrame, pd.DataFrame,
                                    pd.DataFrame] | None:
    """Slice the three frames exactly as the watcher sees them at scan
    time `t` (a 4H boundary): completed bars only, trailing FRAME_LEN,
    indicators attached to 4H/1H. Returns None while history is too
    shallow for a faithful frame (daily regime needs >= 200 closes).
    """
    day_floor = t.replace(hour=0, minute=0, second=0, microsecond=0)
    d = daily[daily.index < day_floor].tail(FRAME_LEN)
    h4_s = h4[h4.index <= t - _4H].tail(FRAME_LEN)
    h1_s = h1[h1.index <= t - _1H].tail(FRAME_LEN)
    if len(d) < 200 or len(h4_s) < FRAME_LEN or len(h1_s) < FRAME_LEN:
        return None
    return d, add_indicators(h4_s), add_indicators(h1_s)


@dataclass
class Signal:
    """One scan's Setup A evaluation, with both variants' verdicts."""
    symbol: str
    scan_ts: datetime
    regime: str
    exact: bool               # production cond5 qualification
    window: bool              # Variant B qualification
    entry: float | None = None
    stop: float | None = None
    atr: float | None = None
    reclaim_window_hit: str | None = None


def evaluate_scan(daily: pd.DataFrame, h4: pd.DataFrame,
                  h1: pd.DataFrame, symbol: str,
                  scan_ts: datetime) -> Signal:
    """Run the production evaluator once (has_position=False — the
    per-symbol book applies position state afterwards) and derive both
    variants' qualification from it.
    """
    result = evaluate_setup_a(daily, h4, h1, symbol, has_position=False)
    passed = {c["name"]: c["passed"] for c in result["conditions"]}
    others = all(v for k, v in passed.items()
                 if k != "h1_green_close_reclaims_ema20")
    hit = result["telemetry"]["reclaim_window_hit"]
    sig = Signal(
        symbol=symbol,
        scan_ts=scan_ts,
        regime=classify_regime(daily),
        exact=bool(result["qualified"]),
        window=bool(others and hit is not None),
        reclaim_window_hit=hit,
    )
    if sig.exact or sig.window:
        h4_last = h4.iloc[-1]
        sig.entry = float(h4_last["close"])
        sig.atr = float(h4_last["atr14"])
        # cond7 passed for any qualification, so >= 2 swing lows exist
        # and the most recent one is the structural stop.
        sig.stop = _find_swing_lows(h4)[-1][1]
    return sig


# =========================================================================
# Trade outcome simulation
# =========================================================================

@dataclass
class SimTrade:
    symbol: str
    variant: str              # "exact" | "window"
    signal_ts: datetime
    entry: float
    stop0: float              # structural stop at entry
    atr0: float
    tp1: float
    tp2: float
    tranches: list = field(default_factory=list)   # (frac, price, ts, reason)
    events: list = field(default_factory=list)     # transition log
    stop_hits: list = field(default_factory=list)  # for Experiment 1
    exit_ts: datetime | None = None
    truncated: bool = False   # data ended while the trade was open

    @property
    def risk(self) -> float:
        return self.entry - self.stop0

    def r_gross(self) -> float:
        return sum(f * (p - self.entry) for f, p, _, _ in self.tranches) / self.risk

    def r_net(self) -> float:
        notional_out = sum(f * p for f, p, _, _ in self.tranches)
        fees = FEE_PCT * (self.entry + notional_out)
        return (sum(f * (p - self.entry) for f, p, _, _ in self.tranches)
                - fees) / self.risk

    def exit_reasons(self) -> list[str]:
        return [r for _, _, _, r in self.tranches]


def _regime_at(daily: pd.DataFrame, day_floor: datetime) -> str:
    d = daily[daily.index < day_floor].tail(FRAME_LEN)
    if len(d) < 200:
        return "INSUFFICIENT DATA"
    return classify_regime(d)


def _h4_context(h4: pd.DataFrame, boundary: datetime):
    """(close, ema20, atr14) of the 4H bar that COMPLETED at `boundary`,
    with indicators computed on the production-length trailing slice."""
    s = h4[h4.index <= boundary - _4H].tail(FRAME_LEN)
    if len(s) < 21:
        return None
    s = add_indicators(s)
    last = s.iloc[-1]
    if pd.isna(last["ema20"]) or pd.isna(last["atr14"]):
        return None
    return float(last["close"]), float(last["ema20"]), float(last["atr14"])


def simulate_trade(sig: Signal, variant: str, daily: pd.DataFrame,
                   h4: pd.DataFrame, h1: pd.DataFrame) -> SimTrade:
    """Walk 1H bars after the signal, applying the strategy-doc exit
    rules. See module docstring for the fill model and its biases.
    """
    t = SimTrade(
        symbol=sig.symbol, variant=variant, signal_ts=sig.scan_ts,
        entry=sig.entry, stop0=sig.stop, atr0=sig.atr,
        tp1=sig.entry + TP1_R * (sig.entry - sig.stop),
        tp2=sig.entry + TP2_R * (sig.entry - sig.stop),
    )
    stop = sig.stop
    frac = 1.0
    tp1_done = tp2_done = False
    hwm = None                       # 4H high-water mark since TP2 fill
    tp2_ts = None
    last_regime_day = sig.scan_ts.replace(hour=0, minute=0, second=0,
                                          microsecond=0)
    time_stop_at = sig.scan_ts + timedelta(days=TIME_STOP_DAYS)

    def close_out(price: float, ts: datetime, reason: str):
        nonlocal frac
        t.tranches.append((frac, price, ts, reason))
        frac = 0.0
        t.exit_ts = ts

    future = h1[h1.index >= sig.scan_ts]
    for ts, bar in future.iterrows():
        bar_end = ts + _1H
        o, hi, lo, cl = (float(bar["open"]), float(bar["high"]),
                         float(bar["low"]), float(bar["close"]))

        # 1. Broker-held stop — always armed, checked first (pessimistic).
        if o <= stop:
            t.stop_hits.append({"symbol": sig.symbol, "bar_ts": str(ts),
                                "stop": stop, "frac": frac,
                                "gap_open": True, "variant": variant})
            close_out(o, ts, "stop_gap_open")
            break
        if lo <= stop:
            t.stop_hits.append({"symbol": sig.symbol, "bar_ts": str(ts),
                                "stop": stop, "frac": frac,
                                "gap_open": False, "variant": variant})
            close_out(stop, ts, "stop")
            break

        # 2. Regime exit — re-checked when a UTC day completes.
        day_floor = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        if day_floor > last_regime_day:
            last_regime_day = day_floor
            if _regime_at(daily, day_floor) == "BEARISH":
                close_out(o, ts, "regime_exit")
                break

        # 3. Time stop — 10 days without TP1.
        if not tp1_done and bar_end >= time_stop_at:
            close_out(cl, ts, "time_stop")
            break

        # 4. App-managed TPs — need a 1H close at/through the level.
        if not tp1_done and cl >= t.tp1:
            t.tranches.append((TP1_FRAC, t.tp1, ts, "tp1"))
            frac -= TP1_FRAC
            tp1_done = True
            stop = max(stop, t.entry)          # breakeven floor
            t.events.append(("breakeven", str(ts)))
        if tp1_done and not tp2_done and cl >= t.tp2:
            t.tranches.append((TP2_FRAC, t.tp2, ts, "tp2"))
            frac -= TP2_FRAC
            tp2_done = True
            hwm = hi
            tp2_ts = ts
            t.events.append(("runner_start", str(ts)))

        # 5. Runner management on each completed 4H bar.
        if tp2_done and frac > 0 and bar_end.hour % 4 == 0:
            ctx = _h4_context(h4, bar_end)
            if ctx is not None:
                h4_close, h4_ema20, h4_atr = ctx
                if h4_close < h4_ema20:
                    close_out(h4_close, ts, "runner_ema_exit")
                    break
                # HWM = max 4H high since the TP2 fill (strategy doc),
                # seeded with the fill bar's own 1H high.
                s4 = h4[(h4.index >= tp2_ts - _4H) & (h4.index <= bar_end - _4H)]
                if len(s4):
                    hwm = max(hwm, float(s4["high"].max()))
                trail = hwm - RUNNER_TRAIL_ATR * h4_atr
                if trail > stop:
                    stop = trail
                    t.events.append(("trail_raise", str(ts), round(stop, 6)))

    if frac > 0:
        # Data ended with the trade open — mark-to-market at last close.
        last_ts = future.index[-1] if len(future) else sig.scan_ts
        last_cl = float(future.iloc[-1]["close"]) if len(future) else sig.entry
        t.tranches.append((frac, last_cl, last_ts, "open_at_data_end"))
        t.exit_ts = last_ts
        t.truncated = True
    return t


# =========================================================================
# Per-symbol sequential book
# =========================================================================

def replay_symbol(symbol: str, daily: pd.DataFrame, h4: pd.DataFrame,
                  h1: pd.DataFrame, start: datetime, end: datetime,
                  progress=None) -> dict:
    """Evaluate every scan in [start, end] once, then run the two
    variants' books over the shared signal list. Returns
    {"signals": [Signal...], "trades": {"exact": [...], "window": [...]}}.
    """
    signals: list[Signal] = []
    for i, ts in enumerate(scan_times(start, end)):
        sliced = frames_at(daily, h4, h1, ts)
        if sliced is None:
            continue
        d, h4_s, h1_s = sliced
        signals.append(evaluate_scan(d, h4_s, h1_s, symbol, ts))
        if progress and i % 200 == 0:
            progress(symbol, ts)

    trades: dict[str, list[SimTrade]] = {"exact": [], "window": []}
    for variant in ("exact", "window"):
        busy_until: datetime | None = None
        for sig in signals:
            if busy_until is not None and sig.scan_ts <= busy_until:
                continue
            if not getattr(sig, variant):
                continue
            trade = simulate_trade(sig, variant, daily, h4, h1)
            trades[variant].append(trade)
            busy_until = trade.exit_ts
    return {"signals": signals, "trades": trades}


def summarize_trades(trades: list[SimTrade]) -> dict:
    """Aggregate stats for one variant's book."""
    if not trades:
        return {"n": 0}
    rs = [t.r_net() for t in trades]
    wins = [r for r in rs if r > 0]
    complete = [t for t in trades if not t.truncated]
    return {
        "n": len(trades),
        "n_truncated": sum(t.truncated for t in trades),
        "win_rate": len(wins) / len(rs),
        "mean_r_net": sum(rs) / len(rs),
        "mean_r_gross": sum(t.r_gross() for t in trades) / len(trades),
        "total_r_net": sum(rs),
        "best_r": max(rs),
        "worst_r": min(rs),
        "expectancy_complete_only": (
            sum(t.r_net() for t in complete) / len(complete)
            if complete else None),
        "exit_mix": _exit_mix(trades),
    }


def _exit_mix(trades: list[SimTrade]) -> dict:
    mix: dict[str, int] = {}
    for t in trades:
        for reason in t.exit_reasons():
            mix[reason] = mix.get(reason, 0) + 1
    return mix
