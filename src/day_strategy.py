"""Single source of truth for day-trade regime classification and setup
evaluation.

Mechanical translation of the 10-condition Setup A (Opening Range
Breakout) and Setup B (VWAP Reclaim Continuation) checklists in
`Day_Trading_Strategy.md`. Pure functions — no I/O, no Alpaca, no
calendar lookup. Caller (the day-watcher) is responsible for
- pulling daily SPY bars and 5-min today bars
- computing indicators (EMA, ATR, VWAP, bar-RVOL, session-RVOL)
- resolving earnings/econ blackouts via src.day_calendar
- threading the overnight-gap percentage in

so that this module stays trivially testable.

The setup evaluators return dicts with the same shape as crypto's
src/strategy.py — see CrYpto Strategy doc + tests for the contract.
"""
from __future__ import annotations

from datetime import date as _date, datetime, time as _time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.day_indicators import opening_range

STRATEGY_DOC_PATH = Path(__file__).parent.parent / "Day_Trading_Strategy.md"

if not STRATEGY_DOC_PATH.exists():
    available = [p.name for p in STRATEGY_DOC_PATH.parent.glob("*.md")]
    raise FileNotFoundError(
        f"Day-trade strategy doc not found at {STRATEGY_DOC_PATH}. "
        f"Available .md files in root: {available}"
    )


# --- Tunable thresholds ---------------------------------------------------
# Each constant maps to a specific clause in Day_Trading_Strategy.md. Keep
# the doc as the source of truth and update both together if you change
# a value here.

# Daily regime — "Choppy" if all closes for the last 20 daily candles are
# within ±5% of the daily 200 SMA. Matches doc §"Daily regime" line.
_CHOPPY_WINDOW_BARS = 20
_CHOPPY_PCT = 0.05

# Daily SMA periods for the regime classifier.
_SMA_LONG = 200
_SMA_SHORT = 50

# Overnight-gap disqualifier — strategy doc line "gapped > 4% overnight".
_OVERNIGHT_GAP_PCT = 0.04

# Setup A time window: ORB only fires in the morning [09:45, 10:30) ET.
_SETUP_A_WINDOW_START = _time(9, 45)
_SETUP_A_WINDOW_END = _time(10, 30)

# Setup B time windows: primary [09:45, 11:30) ET, secondary [14:00, 15:00) ET.
# Strategy doc §"Setup B" condition 2.
_SETUP_B_WINDOWS = (
    (_time(9, 45), _time(11, 30)),
    (_time(14, 0), _time(15, 0)),
)

# Setup A condition 5: bar-RVOL on breakout candle ≥ this multiple.
_SETUP_A_BAR_RVOL_MIN = 1.5

# Setup B condition 6: bar-RVOL on reclaim candle ≥ this multiple.
_SETUP_B_BAR_RVOL_MIN = 1.0

# Stop-distance cap applies to both setups — ≤ this × 5-min ATR(14).
_STOP_ATR_CAP = 1.5

# Setup B condition 8 — buffer below the VWAP touch low expressed as
# ATR multiples. Strategy doc: "plus a 0.25× ATR buffer".
_SETUP_B_STOP_BUFFER_ATR = 0.25

# Reward/risk minimum to enter (both setups). 2R, per the doc.
_RR_MIN = 2.0

# No-chasing rule: skip when current price has run > this × ATR past the
# trigger price without coming back. Doc: "more than 1.5× ATR past the
# entry trigger price without coming back".
_NO_CHASE_ATR_MULT = 1.5

# Setup B condition 3 — how many recent 5-min bars to scan for the VWAP
# touch. Wide enough to span typical pullbacks; narrow enough that we
# don't grab a touch from earlier in the session.
_VWAP_TOUCH_LOOKBACK = 12  # ~60 minutes of 5-min bars

# Session start in ET (matches day_indicators._SESSION_START).
_ET = "America/New_York"
_ET_TZ = ZoneInfo(_ET)
_SESSION_START = _time(9, 30)

# Dead-session no-trade filter — session-RVOL below this aborts entries.
# Strategy doc indicators block.
_DEAD_SESSION_RVOL_MAX = 0.7
# -------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Daily regime classifier (SPY only)
# ---------------------------------------------------------------------------


def _sma_scalar(values: list[float], period: int) -> float | None:
    """Final value of a simple moving average. Returns None if too few values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _sma_series(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)
    out: list[float | None] = [None] * (period - 1)
    rolling = sum(values[: period - 1])  # one short of full window
    for i in range(period - 1, len(values)):
        rolling += values[i]
        out.append(rolling / period)
        rolling -= values[i - period + 1]
    return out


def compute_regime_details(spy_daily_df: pd.DataFrame) -> dict:
    """Compute SPY's daily regime label plus all classifier internals.

    Mirrors the crypto strategy's compute_regime_details() return shape:
    {last_close, sma50, sma200, close_vs_sma200_pct, sma50_crossed_above_recently,
     regime}. Insertion order matches the alert formatter's expected layout.
    """
    closes = [float(c) for c in spy_daily_df["close"].to_list()]
    if len(closes) < _SMA_LONG:
        return {"regime": "INSUFFICIENT_DATA"}

    sma50 = _sma_scalar(closes, _SMA_SHORT)
    sma200 = _sma_scalar(closes, _SMA_LONG)
    last = closes[-1]
    info: dict = {
        "last_close": last,
        "sma50": sma50,
        "sma200": sma200,
        "close_vs_sma200_pct": (last - sma200) / sma200 * 100,
    }

    # Choppy detection: every close in the last _CHOPPY_WINDOW_BARS daily
    # candles is within ±_CHOPPY_PCT of the SMA200 at that bar.
    sma200_series = _sma_series(closes, _SMA_LONG)
    last_window_closes = closes[-_CHOPPY_WINDOW_BARS:]
    last_window_sma200 = [v for v in sma200_series[-_CHOPPY_WINDOW_BARS:] if v is not None]
    in_chop = (
        len(last_window_sma200) == _CHOPPY_WINDOW_BARS
        and all(
            abs(c - s) / s <= _CHOPPY_PCT
            for c, s in zip(last_window_closes, last_window_sma200)
        )
    )

    if last > sma200 and sma50 >= sma200:
        regime = "BULLISH"
    elif last > sma200 and sma50 < sma200:
        regime = "IMPROVING"
    elif in_chop:
        regime = "CHOPPY"
    elif last < sma200:
        regime = "BEARISH"
    else:
        regime = "UNCLASSIFIED"

    info["regime"] = regime
    return info


def classify_regime(spy_daily_df: pd.DataFrame) -> str:
    """Return just the daily regime label."""
    return compute_regime_details(spy_daily_df)["regime"]


# ---------------------------------------------------------------------------
# Intraday character (SPY 5-min)
# ---------------------------------------------------------------------------


def classify_intraday_character(spy_5min_df: pd.DataFrame) -> str:
    """Classify the SPY intraday session as BULLISH / MIXED / BEARISH.

    `spy_5min_df` must include columns `vwap`, `ema9`, `close`. The check
    uses the most recent closed 5-min bar.

    - BULLISH: close > VWAP AND close > EMA9.
    - BEARISH: close < VWAP AND close < EMA9.
    - MIXED: anything else (one above, one below — SPY whipping).

    Returns INSUFFICIENT_DATA if the DataFrame is empty or required cells
    are NaN.
    """
    if len(spy_5min_df) == 0:
        return "INSUFFICIENT_DATA"
    last = spy_5min_df.iloc[-1]
    close = last.get("close")
    vwap = last.get("vwap")
    ema9 = last.get("ema9")
    if pd.isna(close) or pd.isna(vwap) or pd.isna(ema9):
        return "INSUFFICIENT_DATA"

    above_vwap = close > vwap
    above_ema9 = close > ema9
    if above_vwap and above_ema9:
        return "BULLISH"
    if (not above_vwap) and (not above_ema9):
        return "BEARISH"
    return "MIXED"


# ---------------------------------------------------------------------------
# Helpers — VWAP touch detection, time-window membership, no-chase
# ---------------------------------------------------------------------------


def _in_time_window(now_et: datetime, start: _time, end: _time) -> bool:
    """True if now_et's ET clock falls in [start, end).

    Requires tz-aware input — naive datetimes are ambiguous about whether
    they're meant to be ET, UTC, or local, and the strategy doc is
    explicit that all time windows are in ET.
    """
    if now_et.tzinfo is None:
        raise ValueError("now_et must be tz-aware so we can convert to ET")
    et_clock = now_et.astimezone(_ET_TZ).time()
    return start <= et_clock < end


def _in_any_window(now_et: datetime, windows: tuple[tuple[_time, _time], ...]) -> bool:
    return any(_in_time_window(now_et, s, e) for s, e in windows)


def _find_vwap_touch(
    cand_5min_df: pd.DataFrame, lookback: int = _VWAP_TOUCH_LOOKBACK,
) -> dict | None:
    """Scan the last `lookback` closed 5-min bars (excluding the most recent
    bar — that's the reclaim candle being evaluated separately) for a bar
    whose low ≤ VWAP at that bar. Returns the most recent such bar's
    {touch_ts, touch_low, vwap_at_touch} or None.
    """
    if len(cand_5min_df) < 2:
        return None
    # Exclude the most recent (reclaim) bar from the search.
    scan = cand_5min_df.iloc[-(lookback + 1):-1]
    for ts, row in reversed(list(scan.iterrows())):
        low = row.get("low")
        vwap = row.get("vwap")
        if pd.isna(low) or pd.isna(vwap):
            continue
        if low <= vwap:
            return {"touch_ts": ts, "touch_low": float(low), "vwap_at_touch": float(vwap)}
    return None


def _prior_intraday_high(cand_5min_df: pd.DataFrame) -> float | None:
    """Highest high in the session BEFORE the most recent bar. Used to
    test the ≥ 2R target requirement for Setup B."""
    if len(cand_5min_df) < 2:
        return None
    prior = cand_5min_df.iloc[:-1]
    if len(prior) == 0:
        return None
    return float(prior["high"].max())


def _no_chase_violation(
    current_price: float, trigger_price: float, atr_value: float,
) -> bool:
    """True if `current_price` has run > _NO_CHASE_ATR_MULT × ATR past the
    `trigger_price` (upward). Both setups skip when this is True.
    """
    if pd.isna(atr_value) or atr_value <= 0:
        return False
    return (current_price - trigger_price) > _NO_CHASE_ATR_MULT * atr_value


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


# ---------------------------------------------------------------------------
# Setup A — Opening Range Breakout
# ---------------------------------------------------------------------------


def evaluate_setup_a(
    *,
    symbol: str,
    now_et: datetime,
    spy_daily_df: pd.DataFrame,
    spy_5min_df: pd.DataFrame,
    cand_5min_df: pd.DataFrame,
    has_position: bool,
    in_earnings_blackout: bool,
    overnight_gap_pct: float,
) -> dict:
    """Evaluate the 10-condition Opening Range Breakout setup for `symbol`.

    All input DataFrames must already have indicators added. `cand_5min_df`
    must include vwap, ema9, ema20, atr14, bar_rvol columns.

    Returns a dict with `qualified`, `conditions` (list of per-check
    dicts), and `entry`/`stop`/`atr`/`tp1`/`tp2` if qualified.
    """
    conditions: list[dict] = []

    regime = classify_regime(spy_daily_df)
    intraday = classify_intraday_character(spy_5min_df)

    # C1 — Daily regime bullish/improving AND intraday bullish or mixed.
    cond1 = regime in ("BULLISH", "IMPROVING") and intraday in ("BULLISH", "MIXED")
    conditions.append(_check(
        "daily_regime_and_intraday_character",
        cond1, f"regime={regime} intraday={intraday}",
    ))

    # C2 — Within Setup A time window [09:45, 10:30) ET.
    cond2 = _in_time_window(now_et, _SETUP_A_WINDOW_START, _SETUP_A_WINDOW_END)
    conditions.append(_check(
        "in_setup_a_time_window",
        cond2, f"now_et={now_et.isoformat()} window=[09:45,10:30)",
    ))

    # C3 — Opening range exists (we are past 09:45).
    or_levels = opening_range(cand_5min_df, session_date_et=now_et.date())
    cond3 = or_levels is not None
    conditions.append(_check(
        "opening_range_identified",
        cond3, f"or={or_levels}",
    ))

    orh, orl = or_levels if or_levels else (None, None)

    # C4 — Most recent closed 5-min candle closed above ORH.
    last = cand_5min_df.iloc[-1] if len(cand_5min_df) > 0 else None
    last_close = float(last["close"]) if last is not None else None
    cond4 = orh is not None and last_close is not None and last_close > orh
    conditions.append(_check(
        "5min_close_above_orh",
        cond4, f"close={last_close} orh={orh}",
    ))

    # C5 — Bar-RVOL on the breakout candle ≥ _SETUP_A_BAR_RVOL_MIN.
    bar_rvol_val = float(last["bar_rvol"]) if last is not None and pd.notna(last.get("bar_rvol")) else None
    cond5 = bar_rvol_val is not None and bar_rvol_val >= _SETUP_A_BAR_RVOL_MIN
    conditions.append(_check(
        "bar_rvol_above_threshold",
        cond5, f"bar_rvol={bar_rvol_val} threshold={_SETUP_A_BAR_RVOL_MIN}x",
    ))

    # C6 — Price above session VWAP at the moment of breakout.
    vwap_val = float(last["vwap"]) if last is not None and pd.notna(last.get("vwap")) else None
    cond6 = vwap_val is not None and last_close is not None and last_close > vwap_val
    conditions.append(_check(
        "above_session_vwap",
        cond6, f"close={last_close} vwap={vwap_val}",
    ))

    # C7 — 5-min EMA 9 > EMA 20.
    ema9 = float(last["ema9"]) if last is not None and pd.notna(last.get("ema9")) else None
    ema20 = float(last["ema20"]) if last is not None and pd.notna(last.get("ema20")) else None
    cond7 = ema9 is not None and ema20 is not None and ema9 > ema20
    conditions.append(_check(
        "ema9_above_ema20",
        cond7, f"ema9={ema9} ema20={ema20}",
    ))

    # C8 — Stop at OR midpoint, distance ≤ _STOP_ATR_CAP × ATR, and not in
    # no-chase territory.
    atr_val = float(last["atr14"]) if last is not None and pd.notna(last.get("atr14")) else None
    stop = None
    stop_dist = None
    no_chase_violated = False
    if orh is not None and orl is not None and last_close is not None and atr_val:
        stop = (orh + orl) / 2.0
        stop_dist = last_close - stop
        no_chase_violated = _no_chase_violation(last_close, orh, atr_val)
    cond8 = (
        stop is not None
        and stop_dist is not None
        and stop_dist > 0
        and atr_val is not None
        and stop_dist <= _STOP_ATR_CAP * atr_val
        and not no_chase_violated
    )
    conditions.append(_check(
        "stop_at_or_midpoint_within_atr_cap",
        cond8,
        f"stop={stop} stop_dist={stop_dist} atr14={atr_val} "
        f"cap={_STOP_ATR_CAP}xATR no_chase_violation={no_chase_violated}",
    ))

    # C9 — No earnings today/yesterday (skipped for ETFs by caller).
    # Also includes the overnight-gap disqualifier — strategy doc places
    # the gap check in §"Daily regime", but it's per-ticker and most
    # naturally lives in the no-trade gate.
    gap_ok = abs(overnight_gap_pct) <= _OVERNIGHT_GAP_PCT
    cond9 = (not in_earnings_blackout) and gap_ok
    conditions.append(_check(
        "no_earnings_and_no_gap",
        cond9,
        f"earnings_blackout={in_earnings_blackout} gap_pct={overnight_gap_pct} "
        f"gap_cap={_OVERNIGHT_GAP_PCT}",
    ))

    # C10 — No existing position.
    cond10 = not has_position
    conditions.append(_check(
        "no_existing_position", cond10, f"has_position={has_position}",
    ))

    qualified = all(c["passed"] for c in conditions)
    result = {
        "setup": "A",
        "symbol": symbol,
        "qualified": qualified,
        "conditions": conditions,
        "entry": None,
        "stop": None,
        "atr": None,
        "tp1": None,
        "tp2": None,
    }
    if qualified:
        # Entry = last close; TP1 at +1R, TP2 at +2R. R = entry − stop.
        r = last_close - stop
        result["entry"] = last_close
        result["stop"] = stop
        result["atr"] = atr_val
        result["tp1"] = last_close + r
        result["tp2"] = last_close + 2 * r
    return result


# ---------------------------------------------------------------------------
# Setup B — VWAP Reclaim Continuation
# ---------------------------------------------------------------------------


def evaluate_setup_b(
    *,
    symbol: str,
    now_et: datetime,
    spy_daily_df: pd.DataFrame,
    spy_5min_df: pd.DataFrame,
    cand_5min_df: pd.DataFrame,
    has_position: bool,
    in_earnings_blackout: bool,
    overnight_gap_pct: float,
) -> dict:
    """Evaluate the 10-condition VWAP Reclaim Continuation setup."""
    conditions: list[dict] = []

    regime = classify_regime(spy_daily_df)
    intraday = classify_intraday_character(spy_5min_df)

    # C1 — Daily regime bullish AND intraday bullish (no MIXED allowed for B).
    cond1 = regime == "BULLISH" and intraday == "BULLISH"
    conditions.append(_check(
        "daily_regime_bullish_and_intraday_bullish",
        cond1, f"regime={regime} intraday={intraday}",
    ))

    # C2 — Within a Setup B time window.
    cond2 = _in_any_window(now_et, _SETUP_B_WINDOWS)
    conditions.append(_check(
        "in_setup_b_time_window",
        cond2,
        f"now_et={now_et.isoformat()} "
        f"windows=[09:45-11:30, 14:00-15:00)",
    ))

    # C3 — A session pullback touched/dipped below VWAP from above.
    touch = _find_vwap_touch(cand_5min_df)
    cond3 = touch is not None
    conditions.append(_check(
        "vwap_touch_in_recent_pullback",
        cond3, f"touch={touch}",
    ))

    # C4 — Most recent closed 5-min candle is green AND closes back above VWAP.
    last = cand_5min_df.iloc[-1] if len(cand_5min_df) > 0 else None
    last_open = float(last["open"]) if last is not None else None
    last_close = float(last["close"]) if last is not None else None
    last_vwap = float(last["vwap"]) if last is not None and pd.notna(last.get("vwap")) else None
    is_green = last_open is not None and last_close is not None and last_close > last_open
    above_vwap = last_close is not None and last_vwap is not None and last_close > last_vwap
    cond4 = is_green and above_vwap
    conditions.append(_check(
        "green_5min_close_above_vwap",
        cond4, f"open={last_open} close={last_close} vwap={last_vwap} green={is_green}",
    ))

    # C5 — 5-min EMA 9 > EMA 20.
    ema9 = float(last["ema9"]) if last is not None and pd.notna(last.get("ema9")) else None
    ema20 = float(last["ema20"]) if last is not None and pd.notna(last.get("ema20")) else None
    cond5 = ema9 is not None and ema20 is not None and ema9 > ema20
    conditions.append(_check(
        "ema9_above_ema20",
        cond5, f"ema9={ema9} ema20={ema20}",
    ))

    # C6 — Bar-RVOL on reclaim candle ≥ _SETUP_B_BAR_RVOL_MIN.
    bar_rvol_val = float(last["bar_rvol"]) if last is not None and pd.notna(last.get("bar_rvol")) else None
    cond6 = bar_rvol_val is not None and bar_rvol_val >= _SETUP_B_BAR_RVOL_MIN
    conditions.append(_check(
        "bar_rvol_above_threshold",
        cond6, f"bar_rvol={bar_rvol_val} threshold={_SETUP_B_BAR_RVOL_MIN}x",
    ))

    # C8 (computed first — C7 needs the stop) — Stop = touch_low − 0.25×ATR,
    # within 1.5× ATR cap.
    atr_val = float(last["atr14"]) if last is not None and pd.notna(last.get("atr14")) else None
    stop = None
    stop_dist = None
    no_chase_violated = False
    if touch is not None and atr_val is not None and last_close is not None:
        stop = touch["touch_low"] - _SETUP_B_STOP_BUFFER_ATR * atr_val
        stop_dist = last_close - stop
        # No-chase trigger: reclaim happens at touch_vwap; if we are well
        # past that, the entry is stale.
        no_chase_violated = _no_chase_violation(last_close, touch["vwap_at_touch"], atr_val)

    # C7 — Clear prior intraday high above current price giving ≥ 2R.
    prior_high = _prior_intraday_high(cand_5min_df)
    has_2r_target = False
    if prior_high is not None and stop_dist is not None and stop_dist > 0:
        reward = prior_high - last_close
        has_2r_target = reward >= _RR_MIN * stop_dist
    cond7 = has_2r_target
    conditions.append(_check(
        "prior_high_gives_2r_target",
        cond7,
        f"prior_high={prior_high} entry={last_close} stop_dist={stop_dist} rr_min={_RR_MIN}",
    ))

    cond8 = (
        stop is not None
        and stop_dist is not None
        and stop_dist > 0
        and atr_val is not None
        and stop_dist <= _STOP_ATR_CAP * atr_val
        and not no_chase_violated
    )
    conditions.append(_check(
        "stop_below_vwap_touch_within_atr_cap",
        cond8,
        f"stop={stop} stop_dist={stop_dist} atr14={atr_val} "
        f"cap={_STOP_ATR_CAP}xATR no_chase_violation={no_chase_violated}",
    ))

    # C9 — No earnings + no gap.
    gap_ok = abs(overnight_gap_pct) <= _OVERNIGHT_GAP_PCT
    cond9 = (not in_earnings_blackout) and gap_ok
    conditions.append(_check(
        "no_earnings_and_no_gap",
        cond9,
        f"earnings_blackout={in_earnings_blackout} gap_pct={overnight_gap_pct} "
        f"gap_cap={_OVERNIGHT_GAP_PCT}",
    ))

    # C10 — No existing position.
    cond10 = not has_position
    conditions.append(_check(
        "no_existing_position", cond10, f"has_position={has_position}",
    ))

    qualified = all(c["passed"] for c in conditions)
    result = {
        "setup": "B",
        "symbol": symbol,
        "qualified": qualified,
        "conditions": conditions,
        "entry": None,
        "stop": None,
        "atr": None,
        "tp1": None,
        "tp2": None,
    }
    if qualified:
        r = last_close - stop
        result["entry"] = last_close
        result["stop"] = stop
        result["atr"] = atr_val
        result["tp1"] = last_close + r
        result["tp2"] = last_close + 2 * r
    return result


# ---------------------------------------------------------------------------
# Tie-breaker — Setup A wins when both fire on the same name in the same scan
# ---------------------------------------------------------------------------


def pick_winner(setup_a: dict, setup_b: dict) -> dict | None:
    """Return the qualifying setup, with Setup A winning ties.

    If neither qualifies, returns None. If only one qualifies, returns it.
    If both qualify, returns Setup A (its window is narrower and expires
    sooner — Issue #3 decision).
    """
    a_ok = setup_a.get("qualified", False)
    b_ok = setup_b.get("qualified", False)
    if a_ok and b_ok:
        return setup_a
    if a_ok:
        return setup_a
    if b_ok:
        return setup_b
    return None
