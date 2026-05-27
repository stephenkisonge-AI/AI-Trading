"""Tests for src/day_indicators.py — session VWAP, opening range, bar-RVOL,
session-RVOL.

Inputs are constructed by hand so expected outputs can be derived without
relying on the implementation under test.
"""
from datetime import date, datetime, time, timedelta, timezone

import math
import pandas as pd
import pytest

from src.day_indicators import (
    bar_rvol,
    opening_range,
    session_rvol,
    session_vwap,
)

_ET = "America/New_York"


def _et_minute_bars(
    session_date: date,
    minute_offsets: list[int],
    *,
    opens=None,
    highs=None,
    lows=None,
    closes,
    volumes,
) -> pd.DataFrame:
    """Build a 1-min bar DataFrame indexed at ET, returning bars at
    (09:30 + offset) for each offset in `minute_offsets`. Stored
    internally in UTC so the indicators have to tz-convert (this is the
    realistic shape from src/data.py).
    """
    if opens is None:
        opens = closes
    if highs is None:
        highs = [max(o, c) for o, c in zip(opens, closes)]
    if lows is None:
        lows = [min(o, c) for o, c in zip(opens, closes)]

    base = pd.Timestamp.combine(session_date, time(9, 30)).tz_localize(_ET)
    timestamps = [base + pd.Timedelta(minutes=m) for m in minute_offsets]
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=pd.DatetimeIndex(timestamps, name="timestamp"),
    )
    return df.tz_convert("UTC")


# ---------------------------------------------------------------------------
# session_vwap
# ---------------------------------------------------------------------------


def test_session_vwap_single_bar_equals_typical_price():
    bars = _et_minute_bars(
        date(2026, 5, 26), [0],
        opens=[100.0], highs=[102.0], lows=[98.0], closes=[100.0], volumes=[1000],
    )
    vwap = session_vwap(bars)
    assert vwap.iloc[0] == pytest.approx((102 + 98 + 100) / 3)


def test_session_vwap_volume_weighted():
    # Two bars, one with 3x the volume — VWAP should be pulled toward it.
    bars = _et_minute_bars(
        date(2026, 5, 26), [0, 1],
        opens=[100, 110], highs=[100, 110], lows=[100, 110], closes=[100, 110],
        volumes=[1000, 3000],
    )
    vwap = session_vwap(bars)
    expected_t2 = (100 * 1000 + 110 * 3000) / (1000 + 3000)
    assert vwap.iloc[1] == pytest.approx(expected_t2)
    # First bar's VWAP is just its typical price.
    assert vwap.iloc[0] == pytest.approx(100.0)


def test_session_vwap_resets_at_session_boundary():
    # Day 1: one bar with price 100, vol 1000.
    # Day 2: one bar with price 200, vol 1000.
    # Day 2's VWAP should be 200, not (100 + 200) / 2 = 150.
    d1 = _et_minute_bars(date(2026, 5, 26), [0], closes=[100.0], volumes=[1000])
    d2 = _et_minute_bars(date(2026, 5, 27), [0], closes=[200.0], volumes=[1000])
    bars = pd.concat([d1, d2])
    vwap = session_vwap(bars)
    assert vwap.iloc[0] == pytest.approx(100.0)
    assert vwap.iloc[1] == pytest.approx(200.0)


def test_session_vwap_zero_volume_bars_contribute_nothing():
    # A zero-volume bar between two normal bars shouldn't move VWAP.
    bars = _et_minute_bars(
        date(2026, 5, 26), [0, 1, 2],
        closes=[100, 200, 110], volumes=[1000, 0, 1000],
    )
    vwap = session_vwap(bars)
    expected_t3 = (100 * 1000 + 110 * 1000) / 2000
    assert vwap.iloc[2] == pytest.approx(expected_t3)


def test_session_vwap_first_bar_zero_volume_is_nan():
    bars = _et_minute_bars(
        date(2026, 5, 26), [0],
        closes=[100.0], volumes=[0],
    )
    vwap = session_vwap(bars)
    assert math.isnan(vwap.iloc[0])


def test_session_vwap_empty_bars():
    bars = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": []},
        index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
    )
    vwap = session_vwap(bars)
    assert len(vwap) == 0


def test_session_vwap_requires_tz_aware():
    bars = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
        index=pd.DatetimeIndex(["2026-05-26 14:30"], name="timestamp"),  # naive
    )
    with pytest.raises(ValueError, match="tz-aware"):
        session_vwap(bars)


# ---------------------------------------------------------------------------
# opening_range
# ---------------------------------------------------------------------------


def test_opening_range_covers_first_15_minutes():
    # Bars at +0, +5, +10 (all in OR) and +15, +20 (outside OR).
    bars = _et_minute_bars(
        date(2026, 5, 26), [0, 5, 10, 15, 20],
        closes=[100, 100, 100, 100, 100],
        highs=[110, 115, 112, 200, 99],   # 200 is outside OR
        lows=[95, 90, 96, 5, 99],          # 5 is outside OR
        volumes=[100, 100, 100, 100, 100],
    )
    orh, orl = opening_range(bars, session_date_et=date(2026, 5, 26))
    assert orh == 115.0  # highest high inside [09:30, 09:45)
    assert orl == 90.0   # lowest low inside [09:30, 09:45)


def test_opening_range_excludes_945_bar():
    # The 15-min window is right-exclusive: a bar exactly at 09:45 is NOT
    # part of the opening range. Strategy doc: "the first 15 minutes
    # (9:30–9:45)" — 9:45 is the boundary, not inside.
    bars = _et_minute_bars(
        date(2026, 5, 26), [0, 15],
        closes=[100, 100],
        highs=[110, 999],
        lows=[90, 1],
        volumes=[100, 100],
    )
    orh, orl = opening_range(bars)
    assert orh == 110
    assert orl == 90


def test_opening_range_returns_none_when_no_bars_in_window():
    bars = _et_minute_bars(
        date(2026, 5, 26), [15, 20, 30],   # all post-OR
        closes=[100, 100, 100], volumes=[1, 1, 1],
    )
    assert opening_range(bars, session_date_et=date(2026, 5, 26)) is None


def test_opening_range_empty_bars():
    bars = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": []},
        index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
    )
    assert opening_range(bars) is None


def test_opening_range_uses_most_recent_session_when_unspecified():
    # Bars span two sessions. With session_date_et=None we should look at
    # the latter session's OR, not aggregate across.
    d1 = _et_minute_bars(date(2026, 5, 26), [0], closes=[100], highs=[999], lows=[1], volumes=[1])
    d2 = _et_minute_bars(date(2026, 5, 27), [0, 5], closes=[200, 200], highs=[210, 205], lows=[195, 198], volumes=[1, 1])
    bars = pd.concat([d1, d2])
    orh, orl = opening_range(bars)
    assert orh == 210
    assert orl == 195


# ---------------------------------------------------------------------------
# bar_rvol — per-bar time-of-day relative volume
# ---------------------------------------------------------------------------


def _hist_bars_volume_by_time(
    session_dates: list[date],
    minute_offset: int,
    volumes_per_day: list[int],
) -> pd.DataFrame:
    """Build a history DataFrame with one bar per session, all at the
    same ET time-of-day. Useful for testing the time-of-day join.
    """
    parts = []
    for d, v in zip(session_dates, volumes_per_day):
        parts.append(_et_minute_bars(d, [minute_offset], closes=[100.0], volumes=[v]))
    return pd.concat(parts)


def test_bar_rvol_simple_ratio():
    # History across 4 days at 09:35 ET: volumes 1000, 2000, 1500, 1500
    # (mean = 1500). Today's bar at 09:35 ET has volume 3000 → RVOL 2.0.
    history = _hist_bars_volume_by_time(
        [date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21), date(2026, 5, 22)],
        minute_offset=5,
        volumes_per_day=[1000, 2000, 1500, 1500],
    )
    today = _et_minute_bars(date(2026, 5, 26), [5], closes=[100.0], volumes=[3000])
    rv = bar_rvol(today, history)
    assert rv.iloc[0] == pytest.approx(2.0)


def test_bar_rvol_no_match_returns_nan():
    # History has bars at 09:35 only; today's bar is at 09:40 → no match.
    history = _hist_bars_volume_by_time(
        [date(2026, 5, 19), date(2026, 5, 20)],
        minute_offset=5, volumes_per_day=[1000, 1000],
    )
    today = _et_minute_bars(date(2026, 5, 26), [10], closes=[100.0], volumes=[3000])
    rv = bar_rvol(today, history)
    assert math.isnan(rv.iloc[0])


def test_bar_rvol_empty_history_all_nan():
    today = _et_minute_bars(date(2026, 5, 26), [5], closes=[100.0], volumes=[3000])
    empty = today.iloc[:0]
    rv = bar_rvol(today, empty)
    assert math.isnan(rv.iloc[0])


def test_bar_rvol_empty_today():
    history = _hist_bars_volume_by_time(
        [date(2026, 5, 19)], minute_offset=5, volumes_per_day=[1000],
    )
    empty = history.iloc[:0]
    rv = bar_rvol(empty, history)
    assert len(rv) == 0


def test_bar_rvol_zero_historical_mean_is_nan():
    history = _hist_bars_volume_by_time(
        [date(2026, 5, 19), date(2026, 5, 20)],
        minute_offset=5, volumes_per_day=[0, 0],
    )
    today = _et_minute_bars(date(2026, 5, 26), [5], closes=[100.0], volumes=[3000])
    rv = bar_rvol(today, history)
    assert math.isnan(rv.iloc[0])


# ---------------------------------------------------------------------------
# session_rvol — cumulative same-time-of-day relative volume
# ---------------------------------------------------------------------------


def test_session_rvol_first_bar_equals_bar_rvol():
    # With only one bar in each session, cumulative == bar volume.
    history = _hist_bars_volume_by_time(
        [date(2026, 5, 19), date(2026, 5, 20)],
        minute_offset=0, volumes_per_day=[1000, 1000],
    )
    today = _et_minute_bars(date(2026, 5, 26), [0], closes=[100.0], volumes=[500])
    rv = session_rvol(today, history)
    assert rv.iloc[0] == pytest.approx(0.5)


def test_session_rvol_accumulates_over_session():
    # History: 3 sessions, each with two bars (09:30 vol=100, 09:35 vol=200).
    # Per-day cumulative: 100 at 09:30, 300 at 09:35.
    # Mean across days: 100 at 09:30, 300 at 09:35.
    hist_parts = []
    for d in [date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21)]:
        hist_parts.append(
            _et_minute_bars(d, [0, 5], closes=[100.0, 100.0], volumes=[100, 200])
        )
    history = pd.concat(hist_parts)

    # Today: 09:30 vol=200 (cum 200, vs avg 100 → RVOL 2.0)
    #        09:35 vol=400 (cum 600, vs avg 300 → RVOL 2.0)
    today = _et_minute_bars(
        date(2026, 5, 26), [0, 5],
        closes=[100, 100], volumes=[200, 400],
    )
    rv = session_rvol(today, history)
    assert rv.iloc[0] == pytest.approx(2.0)
    assert rv.iloc[1] == pytest.approx(2.0)


def test_session_rvol_dead_session_below_threshold():
    # Mirrors the strategy's dead-session filter (< 0.7 → no entries).
    hist_parts = [
        _et_minute_bars(d, [0], closes=[100.0], volumes=[1000])
        for d in [date(2026, 5, 19), date(2026, 5, 20)]
    ]
    history = pd.concat(hist_parts)
    today = _et_minute_bars(date(2026, 5, 26), [0], closes=[100.0], volumes=[500])
    rv = session_rvol(today, history)
    assert rv.iloc[0] == 0.5
    assert rv.iloc[0] < 0.7  # would trip the dead-session filter


def test_session_rvol_empty_history_all_nan():
    today = _et_minute_bars(date(2026, 5, 26), [0, 5], closes=[100, 100], volumes=[100, 200])
    empty = today.iloc[:0]
    rv = session_rvol(today, empty)
    assert math.isnan(rv.iloc[0])
    assert math.isnan(rv.iloc[1])
