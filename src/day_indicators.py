"""Intraday indicators specific to the day-trade strategy.

Pure math — no I/O, no Alpaca, no decision logic. The day-watcher calls
these on DataFrames returned by src/data.py (or its day-trade
equivalent) and feeds the output into the setup evaluators.

Input contract for every function:
- `bars` is a pandas DataFrame indexed by timezone-aware DatetimeIndex
  (UTC is fine; we convert to America/New_York internally for session
  arithmetic).
- Required columns: open, high, low, close, volume.
- Bars are typically 1-minute resolution; 5-min works too but the
  RVOL helpers expect the today_bars and history_bars resolutions to
  match (the time-of-day join is exact).

Definitions are pulled directly from Day_Trading_Strategy.md §"Indicators".
"""
from __future__ import annotations

from datetime import date as _date, time as _time

import pandas as pd

_ET = "America/New_York"

# Session start in ET — matches the strategy's "VWAP cumulative from
# 09:30:00 ET, reset daily" definition.
_SESSION_START = _time(9, 30)

# Opening range window — first 15 minutes of regular session.
# The doc draws ORH/ORL at 9:45 ET (line 132 of Day_Trading_Strategy.md).
_OPENING_RANGE_MINUTES = 15


def _require_tz_aware(bars: pd.DataFrame) -> None:
    if bars.index.tz is None:
        raise ValueError(
            "day_indicators expects a tz-aware DatetimeIndex. "
            "Convert UTC bars with df.tz_convert('UTC') first."
        )


def _to_et(bars: pd.DataFrame) -> pd.DataFrame:
    _require_tz_aware(bars)
    return bars.tz_convert(_ET)


def session_vwap(bars: pd.DataFrame) -> pd.Series:
    """Session-cumulative VWAP, reset daily at the ET session boundary.

    `VWAP_t = Σ(typ_price × volume) / Σ(volume)` over all bars in the
    same ET calendar day as bar t, up to and including t.
    `typ_price = (high + low + close) / 3`.

    Returns a pd.Series indexed identically to `bars`. Bars with zero
    volume contribute zero to the sums; if a session's cumulative
    volume is still zero at bar t, VWAP_t is NaN (avoid div-by-zero,
    not a real signal).
    """
    if len(bars) == 0:
        return pd.Series(dtype="float64", index=bars.index)

    et = _to_et(bars)
    typ_price = (et["high"] + et["low"] + et["close"]) / 3.0
    pv = typ_price * et["volume"]
    session_id = et.index.date

    cum_pv = pv.groupby(session_id).cumsum()
    cum_v = et["volume"].groupby(session_id).cumsum()
    vwap_et = cum_pv / cum_v.replace(0, float("nan"))

    # Return in the caller's original index orientation. Values are
    # identical because we only re-tz'd, not re-indexed.
    vwap_et.index = bars.index
    return vwap_et


def opening_range(
    bars: pd.DataFrame,
    session_date_et: _date | None = None,
    minutes: int = _OPENING_RANGE_MINUTES,
) -> tuple[float, float] | None:
    """Return (ORH, ORL) for the given ET session date.

    The opening range is built from regular-session bars whose ET
    timestamp falls in `[09:30, 09:30 + minutes)`. If no bars land in
    that window, returns None.

    If `session_date_et` is None, uses the ET calendar date of the
    most recent bar in `bars`.
    """
    if len(bars) == 0:
        return None

    et = _to_et(bars)
    if session_date_et is None:
        session_date_et = et.index[-1].date()

    session_start_ts = pd.Timestamp.combine(
        session_date_et, _SESSION_START
    ).tz_localize(_ET)
    session_or_end = session_start_ts + pd.Timedelta(minutes=minutes)

    or_bars = et[(et.index >= session_start_ts) & (et.index < session_or_end)]
    if len(or_bars) == 0:
        return None

    return float(or_bars["high"].max()), float(or_bars["low"].min())


def bar_rvol(
    today_bars: pd.DataFrame,
    history_bars: pd.DataFrame,
) -> pd.Series:
    """Per-bar relative volume.

    For each bar in `today_bars` at time-of-day T, divide today's bar
    volume by the mean of `history_bars`' volume at the same T across
    the prior trading days present in `history_bars`.

    Returns a Series indexed like `today_bars`. NaN where the same
    time-of-day slot isn't represented in history (or where the
    historical mean is zero).

    Used in Setup A condition 5 (≥ 1.5×) and Setup B condition 6
    (≥ 1.0×). Bar resolution of `today_bars` and `history_bars` must
    match — typically 5-min for setup-condition checks.
    """
    if len(today_bars) == 0:
        return pd.Series(dtype="float64", index=today_bars.index)

    today_et = _to_et(today_bars)
    hist_et = _to_et(history_bars) if len(history_bars) > 0 else history_bars

    if len(hist_et) == 0:
        return pd.Series(
            [float("nan")] * len(today_et),
            index=today_bars.index,
            dtype="float64",
        )

    hist_by_time = hist_et.groupby(hist_et.index.time)["volume"].mean()

    today_times = today_et.index.time
    avg_volume = pd.Series(
        [hist_by_time.get(t, float("nan")) for t in today_times],
        index=today_bars.index,
        dtype="float64",
    )
    avg_volume = avg_volume.replace(0, float("nan"))

    today_vol = today_et["volume"].copy()
    today_vol.index = today_bars.index
    return today_vol / avg_volume


def session_rvol(
    today_bars: pd.DataFrame,
    history_bars: pd.DataFrame,
) -> pd.Series:
    """Session-cumulative relative volume.

    For each bar in `today_bars` at time-of-day T, divide today's
    cumulative session volume through T by the mean of
    `history_bars`' same-day cumulative volume through T across prior
    trading days.

    Returns a Series indexed like `today_bars`. NaN where the same T
    isn't represented in history or where the historical cumulative
    mean is zero.

    Used as the "dead session" no-trade filter: session_rvol < 0.7 →
    no entries for the rest of the session.
    """
    if len(today_bars) == 0:
        return pd.Series(dtype="float64", index=today_bars.index)

    today_et = _to_et(today_bars)
    hist_et = _to_et(history_bars) if len(history_bars) > 0 else history_bars

    if len(hist_et) == 0:
        return pd.Series(
            [float("nan")] * len(today_et),
            index=today_bars.index,
            dtype="float64",
        )

    # Today's cumulative session volume — restart per ET session day.
    today_cum = today_et["volume"].groupby(today_et.index.date).cumsum()

    # History: per-session cumulative volume, then average across days
    # at each time-of-day slot.
    hist_cum_per_day = hist_et["volume"].groupby(hist_et.index.date).cumsum()
    hist_cum_by_time = hist_cum_per_day.groupby(hist_et.index.time).mean()

    today_times = today_et.index.time
    avg_cum = pd.Series(
        [hist_cum_by_time.get(t, float("nan")) for t in today_times],
        index=today_bars.index,
        dtype="float64",
    )
    avg_cum = avg_cum.replace(0, float("nan"))

    today_cum.index = today_bars.index
    return today_cum / avg_cum
