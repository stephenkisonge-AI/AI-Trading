"""Single source of truth for regime classification and entry-setup evaluation.

The regime logic here is ported line-for-line from scripts/compute_regime.py
and must produce identical results — the regression diff in Phase 3 Step 5
asserts this.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

STRATEGY_DOC_PATH = Path(__file__).parent.parent / "Crypto Strategy.md"

if not STRATEGY_DOC_PATH.exists():
    available = [p.name for p in STRATEGY_DOC_PATH.parent.glob("*.md")]
    raise FileNotFoundError(
        f"Strategy doc not found at {STRATEGY_DOC_PATH}. "
        f"Available .md files in root: {available}"
    )


def _ema_scalar(values: list[float], period: int) -> float | None:
    """SMA-seeded EMA — final value only. Mirrors compute_regime.py.ema()."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _ema_series(values: list[float], period: int) -> list[float | None]:
    """Full SMA-seeded EMA series. Mirrors compute_regime.py.emas_series()."""
    if len(values) < period:
        return [None] * len(values)
    out: list[float | None] = [None] * (period - 1)
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out.append(e)
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def compute_regime_details(daily_df: pd.DataFrame) -> dict:
    """Compute regime label plus all classifier internals as a dict.

    Returns the same fields, in the same order, as scripts/compute_regime.py's
    classify() info dict, with 'regime' appended last. Insertion order is
    preserved so iteration matches the legacy output line ordering.
    """
    closes = [float(c) for c in daily_df["close"].to_list()]

    if len(closes) < 200:
        return {"regime": "INSUFFICIENT DATA"}

    e20 = _ema_scalar(closes, 20)
    e50 = _ema_scalar(closes, 50)
    e200 = _ema_scalar(closes, 200)
    last = closes[-1]
    info: dict = {
        "last_close": last,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "close_vs_ema200_pct": (last - e200) / e200 * 100,
    }

    # Check if EMA50 crossed above EMA200 in the last 10 daily candles
    e50_series = _ema_series(closes, 50)
    e200_series = _ema_series(closes, 200)
    crossed_up_recent = False
    for i in range(max(1, len(closes) - 10), len(closes)):
        prev50, prev200 = e50_series[i - 1], e200_series[i - 1]
        cur50, cur200 = e50_series[i], e200_series[i]
        if all(v is not None for v in (prev50, prev200, cur50, cur200)):
            if prev50 < prev200 and cur50 >= cur200:
                crossed_up_recent = True
                break
    info["ema50_crossed_above_recently"] = crossed_up_recent

    # Choppy neutral: oscillating within 5% of EMA200 over the last 20 candles
    last20_closes = closes[-20:]
    last20_e200 = [v for v in e200_series[-20:] if v is not None]
    within_5pct = (
        len(last20_e200) == 20
        and all(abs(c - e) / e * 100 <= 5 for c, e in zip(last20_closes, last20_e200))
    )

    if last > e200 and (e50 >= e200 or crossed_up_recent):
        regime = "BULLISH"
    elif last > e200 and e50 < e200:
        regime = "IMPROVING_NEUTRAL"
    elif last < e200 and e50 < e200:
        regime = "BEARISH"
    elif within_5pct:
        regime = "CHOPPY_NEUTRAL"
    else:
        regime = "UNCLASSIFIED"

    info["regime"] = regime
    return info


def classify_regime(daily_df: pd.DataFrame) -> str:
    """Return just the regime label — one of BULLISH, IMPROVING_NEUTRAL,
    CHOPPY_NEUTRAL, BEARISH, UNCLASSIFIED, or 'INSUFFICIENT DATA'.
    """
    return compute_regime_details(daily_df)["regime"]


# ---------------------------------------------------------------------------
# Setup evaluators
#
# Mechanical translations of the 8-condition checklists in Crypto Strategy.md.
# Decisions baked in (call them out here so future-me sees the contract):
#   - Pullback proximity:  |close - EMA| / EMA <= 0.01   (1%)
#   - Swing low detection: bar whose low is the min of itself ±3 bars
#       (7-bar window centred on the candidate)
#   - "1H closes back above EMA20" = STRICT: prior 1H close was <= EMA20,
#       current 1H close > EMA20 AND current bar is green (close > open)
#   - Stop placement: exactly at the swing low (Setup A) or breakout level
#       (Setup B). No extra buffer.
#   - No-chasing rule (Setup B): if current price > breakout_level + 2*ATR
#       AND no qualifying retest was found, the breakout is skipped.
#
# All evaluators assume input DataFrames already have indicator columns
# attached via src.indicators.add_indicators().
# ---------------------------------------------------------------------------


# --- Tunable thresholds ---------------------------------------------------
# Tune these if real trades show systematic issues. Each one corresponds to
# a specific clause in Crypto Strategy.md — keep that doc as the source of
# truth and update both together if you change a value here.

# Swing-low detection window: bar is a swing low if its low is the minimum
# of itself and ±_SWING_WINDOW neighbors. 3 → 7-bar centred window.
# Smaller = catches shallower pullbacks (more noise, more triggers).
_SWING_WINDOW = 3

# Setup A.3 pullback proximity: |close - EMA| / EMA must be ≤ this fraction.
# 0.01 = 1%, matching the "within 1% of the 4H EMA 20 OR EMA 50" clause.
_PULLBACK_PCT = 0.01

# Setup B.6 retest band: 1H low must land within ±_RETEST_BAND_PCT of the
# broken level to count as a retest touch. 0.005 = 0.5%, matching the
# strategy doc's "within 0.5% of the level".
_RETEST_BAND_PCT = 0.005

# Setup A.7 / Setup B.7 stop cap: stop distance must be ≤ this × 4H ATR(14).
# 1.5 matches "no more than 1.5× the 4H ATR(14)" in both setups.
_STOP_ATR_CAP = 1.5

# Setup B no-chasing rule: if breakout occurred but no qualifying retest
# happened AND current price > breakout_level + _NO_CHASE_ATR_MULT × ATR,
# the trade is rejected as a missed breakout. 2.0 matches "more than 2× ATR
# above the breakout level without a retest" in the strategy doc.
_NO_CHASE_ATR_MULT = 2.0

# Setup B.2 breakout lookback: the prior-period high used as the breakout
# level is the max-high of the preceding _BREAKOUT_LOOKBACK 4H bars. The
# strategy doc says "20-period high" → 20.
_BREAKOUT_LOOKBACK = 20

# Setup B.2 breakout window: scan only the last _BREAKOUT_WINDOW 4H bars
# for a qualifying breakout (older breakouts are stale). Doc says
# "in the last 10 candles" → 10.
_BREAKOUT_WINDOW = 10

# Setup A.4 RSI window: 4H RSI(14) must fall in [_SETUP_A_RSI_MIN,
# _SETUP_A_RSI_MAX] — the "oversold-but-not-broken" pullback zone (35-50).
_SETUP_A_RSI_MIN = 35.0
_SETUP_A_RSI_MAX = 50.0

# Setup B.5 RSI window: 4H RSI(14) must fall in [_SETUP_B_RSI_MIN,
# _SETUP_B_RSI_MAX] — momentum confirmed but not exhausted (50-70).
_SETUP_B_RSI_MIN = 50.0
_SETUP_B_RSI_MAX = 70.0

# Setup A.6 volume floor: 1H entry candle volume ≥ _SETUP_A_VOL_MULT × 20-period
# average ("filter out dead-zone moves"). Doc: 0.8×.
_SETUP_A_VOL_MULT = 0.8

# Setup B.4 volume floor: breakout 4H candle volume ≥ _SETUP_B_VOL_MULT ×
# 20-period average (conviction-confirmation). Doc: 1.2×.
_SETUP_B_VOL_MULT = 1.2
# -------------------------------------------------------------------------


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _find_swing_lows(h4_df: pd.DataFrame, window: int = _SWING_WINDOW) -> list[tuple[int, float]]:
    """Return [(positional_index, low_value), ...] for bars whose low is the
    STRICT minimum over ±`window` neighbors. The most recent `window` bars
    are excluded because their swing-low status isn't yet confirmed (need
    `window` bars after them).

    Strict `<` rather than `<=` so flat regions don't produce phantom swing
    lows; a perfectly flat trough is not a valid pivot.
    """
    lows = h4_df["low"].to_list()
    n = len(lows)
    out: list[tuple[int, float]] = []
    for i in range(window, n - window):
        left = lows[i - window : i]
        right = lows[i + 1 : i + 1 + window]
        if lows[i] < min(left) and lows[i] < min(right):
            out.append((i, lows[i]))
    return out


def _find_recent_breakout(h4_df: pd.DataFrame) -> dict | None:
    """Scan the last `_BREAKOUT_WINDOW` 4H bars for one whose high exceeds
    the max-high of the prior `_BREAKOUT_LOOKBACK` bars. Returns the most
    recent qualifying breakout, or None.
    """
    highs = h4_df["high"].to_list()
    closes = h4_df["close"].to_list()
    volumes = h4_df["volume"].to_list()
    n = len(highs)
    if n < _BREAKOUT_LOOKBACK + 1:
        return None

    candidates: list[dict] = []
    scan_start = max(_BREAKOUT_LOOKBACK, n - _BREAKOUT_WINDOW)
    for i in range(scan_start, n):
        prior_high = max(highs[i - _BREAKOUT_LOOKBACK : i])
        if highs[i] > prior_high:
            candidates.append(
                {
                    "index": i,
                    "level": prior_high,
                    "close": closes[i],
                    "volume": volumes[i],
                    "timestamp": h4_df.index[i],
                }
            )
    if not candidates:
        return None
    return candidates[-1]


def _find_retest(h1_df: pd.DataFrame, level: float, after_ts) -> dict | None:
    """Look for a 1H bar after `after_ts` whose low is within
    ±`_RETEST_BAND_PCT` of `level`, followed by a green 1H bar that closes
    above `level`. Returns details of the pair or None.
    """
    band = level * _RETEST_BAND_PCT
    lower = level - band
    upper = level + band

    h1 = h1_df[h1_df.index > after_ts]
    rows = list(h1.iterrows())
    for i in range(len(rows) - 1):
        ts, bar = rows[i]
        if lower <= bar["low"] <= upper:
            next_ts, next_bar = rows[i + 1]
            if next_bar["close"] > next_bar["open"] and next_bar["close"] > level:
                return {
                    "touch_ts": ts,
                    "touch_low": bar["low"],
                    "confirm_ts": next_ts,
                    "confirm_close": next_bar["close"],
                }
    return None


def evaluate_setup_a(
    daily_df: pd.DataFrame,
    h4_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    symbol: str,
    has_position: bool,
) -> dict:
    """Evaluate the 8-condition pullback-continuation setup for `symbol`.
    Returns a dict with `qualified`, `conditions` (list of per-check dicts),
    and `entry`/`stop`/`atr` if qualified.
    """
    conditions: list[dict] = []

    regime = classify_regime(daily_df)
    cond1 = regime in ("BULLISH", "IMPROVING_NEUTRAL")
    conditions.append(_check("daily_regime_bullish_or_improving", cond1, f"regime={regime}"))

    h4_last = h4_df.iloc[-1]
    h4_close = float(h4_last["close"])
    h4_ema200 = float(h4_last["ema200"]) if pd.notna(h4_last["ema200"]) else None
    h4_ema20 = float(h4_last["ema20"]) if pd.notna(h4_last["ema20"]) else None
    h4_ema50 = float(h4_last["ema50"]) if pd.notna(h4_last["ema50"]) else None
    h4_rsi = float(h4_last["rsi14"]) if pd.notna(h4_last["rsi14"]) else None
    h4_atr = float(h4_last["atr14"]) if pd.notna(h4_last["atr14"]) else None

    cond2 = h4_ema200 is not None and h4_close > h4_ema200
    conditions.append(
        _check(
            "h4_price_above_ema200",
            cond2,
            f"close={h4_close:.4f} ema200={'NaN' if h4_ema200 is None else f'{h4_ema200:.4f}'}",
        )
    )

    # Pullback to within 1% of EMA20 OR EMA50, AND higher-low structure intact
    dist20 = abs(h4_close - h4_ema20) / h4_ema20 if h4_ema20 else None
    dist50 = abs(h4_close - h4_ema50) / h4_ema50 if h4_ema50 else None
    pullback_ok = (dist20 is not None and dist20 <= _PULLBACK_PCT) or (
        dist50 is not None and dist50 <= _PULLBACK_PCT
    )

    swings = _find_swing_lows(h4_df)
    higher_low_value: float | None = None
    higher_low_intact = False
    if len(swings) >= 2:
        prev_low = swings[-2][1]
        last_low = swings[-1][1]
        higher_low_value = last_low
        higher_low_intact = last_low > prev_low and h4_close > last_low
    cond3 = pullback_ok and higher_low_intact
    detail3 = (
        f"dist_ema20={'NaN' if dist20 is None else f'{dist20 * 100:.3f}%'} "
        f"dist_ema50={'NaN' if dist50 is None else f'{dist50 * 100:.3f}%'} "
        f"hl_intact={higher_low_intact} "
        f"recent_swing_low={'None' if higher_low_value is None else f'{higher_low_value:.4f}'}"
    )
    conditions.append(_check("pullback_to_ema_and_higher_low_intact", cond3, detail3))

    cond4 = h4_rsi is not None and _SETUP_A_RSI_MIN <= h4_rsi <= _SETUP_A_RSI_MAX
    conditions.append(
        _check(
            "h4_rsi_in_pullback_zone",
            cond4,
            f"rsi14={'NaN' if h4_rsi is None else f'{h4_rsi:.2f}'} "
            f"window=[{_SETUP_A_RSI_MIN},{_SETUP_A_RSI_MAX}]",
        )
    )

    # 1H reclaim above EMA20 (STRICT: prior close <= EMA20, current close > EMA20, green bar)
    h1_reclaim_ok = False
    h1_close = h1_open = h1_ema20 = h1_volume = h1_vol_sma = None
    h1_prior_close = h1_prior_ema20 = None
    if len(h1_df) >= 2:
        h1_last = h1_df.iloc[-1]
        h1_prev = h1_df.iloc[-2]
        h1_close = float(h1_last["close"])
        h1_open = float(h1_last["open"])
        h1_volume = float(h1_last["volume"])
        h1_ema20 = float(h1_last["ema20"]) if pd.notna(h1_last["ema20"]) else None
        h1_vol_sma = (
            float(h1_last["vol_sma20"]) if pd.notna(h1_last["vol_sma20"]) else None
        )
        h1_prior_close = float(h1_prev["close"])
        h1_prior_ema20 = (
            float(h1_prev["ema20"]) if pd.notna(h1_prev["ema20"]) else None
        )
        if h1_ema20 is not None and h1_prior_ema20 is not None:
            h1_reclaim_ok = (
                h1_prior_close <= h1_prior_ema20
                and h1_close > h1_ema20
                and h1_close > h1_open
            )
    cond5 = h1_reclaim_ok
    conditions.append(
        _check(
            "h1_green_close_reclaims_ema20",
            cond5,
            f"prior_close={h1_prior_close} prior_ema20={h1_prior_ema20} "
            f"close={h1_close} ema20={h1_ema20} green={h1_close is not None and h1_open is not None and h1_close > h1_open}",
        )
    )

    cond6 = (
        h1_volume is not None
        and h1_vol_sma is not None
        and h1_volume >= _SETUP_A_VOL_MULT * h1_vol_sma
    )
    conditions.append(
        _check(
            "h1_volume_above_threshold",
            cond6,
            f"vol={h1_volume} vol_sma20={h1_vol_sma} threshold={_SETUP_A_VOL_MULT}x",
        )
    )

    stop = higher_low_value
    stop_dist = h4_close - stop if stop is not None else None
    cond7 = (
        stop is not None
        and stop_dist is not None
        and h4_atr is not None
        and stop_dist > 0
        and stop_dist <= _STOP_ATR_CAP * h4_atr
    )
    conditions.append(
        _check(
            "stop_below_swing_low_within_atr_cap",
            cond7,
            f"stop={stop} stop_dist={stop_dist} atr14={h4_atr} cap={_STOP_ATR_CAP}xATR",
        )
    )

    cond8 = not has_position
    conditions.append(_check("no_existing_position", cond8, f"has_position={has_position}"))

    qualified = all(c["passed"] for c in conditions)
    result = {
        "setup": "A",
        "symbol": symbol,
        "qualified": qualified,
        "conditions": conditions,
        "entry": None,
        "stop": None,
        "atr": None,
        # Closed 4H signal bar — deterministic client order IDs derive
        # from this so a rerun of the same signal mints the same IDs.
        "signal_bar_ts": h4_df.index[-1] if len(h4_df) else None,
    }
    if qualified:
        result["entry"] = h4_close
        result["stop"] = stop
        result["atr"] = h4_atr
    return result


def evaluate_setup_b(
    daily_df: pd.DataFrame,
    h4_df: pd.DataFrame,
    h1_df: pd.DataFrame,
    symbol: str,
    has_position: bool,
) -> dict:
    """Evaluate the 8-condition breakout-retest setup for `symbol`. Same
    return shape as evaluate_setup_a.
    """
    conditions: list[dict] = []

    regime = classify_regime(daily_df)
    cond1 = regime == "BULLISH"
    conditions.append(_check("daily_regime_bullish", cond1, f"regime={regime}"))

    breakout = _find_recent_breakout(h4_df)
    cond2 = breakout is not None
    conditions.append(
        _check(
            "breakout_above_20period_high_in_last_10",
            cond2,
            f"breakout={breakout if breakout is None else {'index': breakout['index'], 'level': breakout['level']}}",
        )
    )

    if breakout is not None:
        cond3 = breakout["close"] > breakout["level"]
        # Volume threshold on the breakout bar
        bar_vol_sma = (
            float(h4_df["vol_sma20"].iloc[breakout["index"]])
            if pd.notna(h4_df["vol_sma20"].iloc[breakout["index"]])
            else None
        )
        cond4 = bar_vol_sma is not None and breakout["volume"] >= _SETUP_B_VOL_MULT * bar_vol_sma
    else:
        cond3 = False
        bar_vol_sma = None
        cond4 = False
    conditions.append(
        _check(
            "breakout_candle_closed_above_level",
            cond3,
            f"breakout_close={'NA' if breakout is None else breakout['close']} "
            f"level={'NA' if breakout is None else breakout['level']}",
        )
    )
    conditions.append(
        _check(
            "breakout_volume_above_threshold",
            cond4,
            f"vol={'NA' if breakout is None else breakout['volume']} "
            f"vol_sma20={bar_vol_sma} threshold={_SETUP_B_VOL_MULT}x",
        )
    )

    h4_last = h4_df.iloc[-1]
    h4_close = float(h4_last["close"])
    h4_rsi = float(h4_last["rsi14"]) if pd.notna(h4_last["rsi14"]) else None
    h4_atr = float(h4_last["atr14"]) if pd.notna(h4_last["atr14"]) else None
    cond5 = h4_rsi is not None and _SETUP_B_RSI_MIN <= h4_rsi <= _SETUP_B_RSI_MAX
    conditions.append(
        _check(
            "h4_rsi_in_momentum_zone",
            cond5,
            f"rsi14={'NaN' if h4_rsi is None else f'{h4_rsi:.2f}'} "
            f"window=[{_SETUP_B_RSI_MIN},{_SETUP_B_RSI_MAX}]",
        )
    )

    retest = None
    if breakout is not None:
        retest = _find_retest(h1_df, breakout["level"], breakout["timestamp"])
    cond6 = retest is not None
    conditions.append(
        _check(
            "h1_retest_of_level_held",
            cond6,
            f"retest={retest}",
        )
    )

    # No-chasing rule applies even if cond6 is False but breakout exists
    no_chase_violation = False
    if breakout is not None and retest is None and h4_atr is not None:
        if h4_close > breakout["level"] + _NO_CHASE_ATR_MULT * h4_atr:
            no_chase_violation = True

    stop = breakout["level"] if breakout is not None else None
    stop_dist = h4_close - stop if stop is not None else None
    cond7 = (
        stop is not None
        and stop_dist is not None
        and h4_atr is not None
        and stop_dist > 0
        and stop_dist <= _STOP_ATR_CAP * h4_atr
        and not no_chase_violation
    )
    conditions.append(
        _check(
            "stop_below_retested_level_within_atr_cap",
            cond7,
            f"stop={stop} stop_dist={stop_dist} atr14={h4_atr} "
            f"cap={_STOP_ATR_CAP}xATR no_chase_violation={no_chase_violation}",
        )
    )

    cond8 = not has_position
    conditions.append(_check("no_existing_position", cond8, f"has_position={has_position}"))

    qualified = all(c["passed"] for c in conditions)
    result = {
        "setup": "B",
        "symbol": symbol,
        "qualified": qualified,
        "conditions": conditions,
        "entry": None,
        "stop": None,
        "atr": None,
        # Closed 4H signal bar — deterministic client order IDs derive
        # from this so a rerun of the same signal mints the same IDs.
        "signal_bar_ts": h4_df.index[-1] if len(h4_df) else None,
    }
    if qualified:
        result["entry"] = h4_close
        result["stop"] = stop
        result["atr"] = h4_atr
    return result
