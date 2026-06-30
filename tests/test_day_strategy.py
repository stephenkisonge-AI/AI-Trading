"""Tests for src/day_strategy.py — SPY regime, intraday character, Setup A & B."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import math
import pandas as pd
import pytest

from src.day_strategy import (
    classify_intraday_character,
    classify_regime,
    compute_regime_details,
    evaluate_setup_a,
    evaluate_setup_b,
    pick_winner,
)

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _daily_df(closes: list[float]) -> pd.DataFrame:
    """Daily SPY DataFrame from a list of closes. Indexed by sequential
    calendar dates (the regime classifier doesn't care about the actual
    dates — just the closes-in-order)."""
    return pd.DataFrame({"close": closes})


def _et_dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=_ET)


def _5min_bars(
    session_date: date,
    minute_offsets: list[int],
    *,
    opens, highs, lows, closes, volumes,
    vwaps=None,
    ema9s=None, ema20s=None,
    atrs=None, bar_rvols=None,
) -> pd.DataFrame:
    """Build a 5-min bar DataFrame with all the indicator columns the
    day-strategy evaluators expect. Index is ET, stored as UTC (the
    realistic shape from src/data.py)."""
    base = pd.Timestamp.combine(session_date, time(9, 30)).tz_localize(_ET)
    timestamps = [base + pd.Timedelta(minutes=m) for m in minute_offsets]
    n = len(minute_offsets)
    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "vwap": vwaps if vwaps is not None else [None] * n,
            "ema9": ema9s if ema9s is not None else [None] * n,
            "ema20": ema20s if ema20s is not None else [None] * n,
            "atr14": atrs if atrs is not None else [None] * n,
            "bar_rvol": bar_rvols if bar_rvols is not None else [None] * n,
        },
        index=pd.DatetimeIndex(timestamps, name="timestamp"),
    )
    return df.tz_convert("UTC")


# ---------------------------------------------------------------------------
# Daily regime
# ---------------------------------------------------------------------------


def test_regime_bullish_when_close_above_200_and_50_above_200():
    # Steadily rising closes — 50 SMA pulls above 200 SMA, close above 200.
    df = _daily_df([100.0 + i * 0.5 for i in range(250)])
    assert classify_regime(df) == "BULLISH"


def test_regime_bearish_when_close_below_200():
    df = _daily_df([300.0 - i * 0.5 for i in range(250)])
    assert classify_regime(df) == "BEARISH"


def test_regime_improving_when_close_above_200_but_50_below_200():
    # Long flat baseline, sharp dip, brief recovery. Last 50 dominated by
    # the dip → SMA50 < SMA200; last close back above SMA200 → IMPROVING.
    # Verification: SMA200 ≈ 93.5, SMA50 = 74, last close = 110.
    closes = [100.0] * 200 + [50.0] * 30 + [110.0] * 20
    df = _daily_df(closes)
    assert classify_regime(df) == "IMPROVING"


def test_regime_choppy_when_oscillating_within_5pct_of_sma200():
    # Flat baseline at 100 → SMA200 = 100, last close = 100. Close > SMA200
    # is False (BULLISH/IMPROVING don't apply); all last 20 closes are
    # within 5% of SMA200 → CHOPPY.
    closes = [100.0] * 250
    df = _daily_df(closes)
    assert classify_regime(df) == "CHOPPY"


def test_regime_insufficient_data_under_200_closes():
    df = _daily_df([100.0] * 150)
    assert classify_regime(df) == "INSUFFICIENT_DATA"


def test_compute_regime_details_keys():
    df = _daily_df([100.0 + i * 0.5 for i in range(250)])
    info = compute_regime_details(df)
    assert info["regime"] == "BULLISH"
    assert "sma50" in info and "sma200" in info
    assert "close_vs_sma200_pct" in info
    # Closes are increasing → last close > sma200 → pct positive.
    assert info["close_vs_sma200_pct"] > 0


# ---------------------------------------------------------------------------
# Intraday character
# ---------------------------------------------------------------------------


def test_intraday_bullish_when_close_above_vwap_and_ema9():
    df = _5min_bars(
        date(2026, 5, 27), [0],
        opens=[100], highs=[101], lows=[99], closes=[101], volumes=[1000],
        vwaps=[100.0], ema9s=[100.5], ema20s=[100.0],
    )
    assert classify_intraday_character(df) == "BULLISH"


def test_intraday_bearish_when_close_below_vwap_and_ema9():
    df = _5min_bars(
        date(2026, 5, 27), [0],
        opens=[100], highs=[100], lows=[98], closes=[98], volumes=[1000],
        vwaps=[100.0], ema9s=[99.5], ema20s=[100.0],
    )
    assert classify_intraday_character(df) == "BEARISH"


def test_intraday_mixed_when_one_above_one_below():
    df = _5min_bars(
        date(2026, 5, 27), [0],
        opens=[100], highs=[101], lows=[99], closes=[101], volumes=[1000],
        vwaps=[100.0], ema9s=[101.5], ema20s=[100.0],
    )
    # close (101) > vwap (100), but close (101) < ema9 (101.5) → MIXED
    assert classify_intraday_character(df) == "MIXED"


def test_intraday_insufficient_data_when_empty():
    df = pd.DataFrame(
        {"open": [], "high": [], "low": [], "close": [], "volume": [],
         "vwap": [], "ema9": [], "ema20": [], "atr14": [], "bar_rvol": []},
        index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
    )
    assert classify_intraday_character(df) == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Setup A — qualifying
# ---------------------------------------------------------------------------


def _bullish_spy_daily() -> pd.DataFrame:
    return _daily_df([100.0 + i * 0.5 for i in range(250)])


def _bullish_spy_5min(session_date: date) -> pd.DataFrame:
    return _5min_bars(
        session_date, [0],
        opens=[400], highs=[402], lows=[399], closes=[402], volumes=[1000],
        vwaps=[400.5], ema9s=[401.0], ema20s=[400.0],
    )


def _setup_a_candidate_passing() -> pd.DataFrame:
    """5-min bars: OR bars (vol low) at 9:30/9:35/9:40 then breakout bar at 9:45.
    Most recent bar (breakout): close 105, above ORH 102, ema9>ema20, bar_rvol 2.0,
    atr14 = 1.5 → stop at OR midpoint = (102 + 100) / 2 = 101, stop_dist = 4,
    cap = 1.5 * 1.5 = 2.25. stop_dist > cap → C8 FAILS.
    Need to tune ATR up so stop_dist ≤ cap. Let's set atr14 = 4.0, cap = 6.0, OK.
    But then no-chase: close(105) - trigger(102) = 3 vs no-chase = 1.5 * 4 = 6 → OK.
    """
    return _5min_bars(
        date(2026, 5, 27), [0, 5, 10, 15],
        opens=[100, 101, 101, 102],
        highs=[101, 102, 101.5, 105.5],
        lows=[99.5, 100.5, 100.0, 102.0],
        closes=[100.5, 101.5, 101.0, 105.0],
        volumes=[100, 100, 100, 500],
        vwaps=[100.0, 100.5, 100.5, 101.0],
        ema9s=[100.0, 100.5, 100.8, 102.0],
        ema20s=[99.5, 100.0, 100.2, 101.0],
        atrs=[1.0, 1.0, 1.0, 4.0],
        bar_rvols=[1.0, 1.0, 1.0, 2.0],
    )


def test_setup_a_qualifies_when_all_conditions_pass():
    result = evaluate_setup_a(
        symbol="NVDA",
        now_et=_et_dt(date(2026, 5, 27), time(9, 50)),  # inside [09:45, 10:30)
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min(date(2026, 5, 27)),
        cand_5min_df=_setup_a_candidate_passing(),
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    failed = [c for c in result["conditions"] if not c["passed"]]
    assert result["qualified"], f"failed: {failed}"
    assert result["entry"] == pytest.approx(105.0)
    # OR = bars at 09:30/35/40 → ORH=102, ORL=99.5 → midpoint=100.75.
    assert result["stop"] == pytest.approx(100.75)
    # R = 105 - 100.75 = 4.25; TP1=109.25, TP2=113.5.
    assert result["tp1"] == pytest.approx(109.25)
    assert result["tp2"] == pytest.approx(113.5)


def test_setup_a_fails_outside_time_window():
    result = evaluate_setup_a(
        symbol="NVDA",
        now_et=_et_dt(date(2026, 5, 27), time(10, 45)),  # past 10:30
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min(date(2026, 5, 27)),
        cand_5min_df=_setup_a_candidate_passing(),
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "in_setup_a_time_window" in failed_names


def test_setup_a_fails_on_earnings_blackout():
    result = evaluate_setup_a(
        symbol="NVDA",
        now_et=_et_dt(date(2026, 5, 27), time(9, 50)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min(date(2026, 5, 27)),
        cand_5min_df=_setup_a_candidate_passing(),
        has_position=False,
        in_earnings_blackout=True,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "no_earnings_and_no_gap" in failed_names


def test_setup_a_fails_on_gap_over_4pct():
    result = evaluate_setup_a(
        symbol="NVDA",
        now_et=_et_dt(date(2026, 5, 27), time(9, 50)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min(date(2026, 5, 27)),
        cand_5min_df=_setup_a_candidate_passing(),
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.05,  # 5% > 4% cap
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "no_earnings_and_no_gap" in failed_names


def test_setup_a_fails_when_has_position():
    result = evaluate_setup_a(
        symbol="NVDA",
        now_et=_et_dt(date(2026, 5, 27), time(9, 50)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min(date(2026, 5, 27)),
        cand_5min_df=_setup_a_candidate_passing(),
        has_position=True,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "no_existing_position" in failed_names


def test_setup_a_fails_on_bar_rvol_below_threshold():
    cand = _setup_a_candidate_passing().copy()
    cand.loc[cand.index[-1], "bar_rvol"] = 1.0  # below 1.5
    result = evaluate_setup_a(
        symbol="NVDA",
        now_et=_et_dt(date(2026, 5, 27), time(9, 50)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min(date(2026, 5, 27)),
        cand_5min_df=cand,
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "bar_rvol_above_threshold" in failed_names


def test_setup_a_fails_on_no_chase_violation():
    # Push the breakout close far above ORH so no-chase trips.
    # Breakout candle: close=130 (vs ORH 102). atr=4 → no-chase = 6.
    # 130 - 102 = 28 > 6 → no_chase_violated = True → C8 fails.
    cand = _setup_a_candidate_passing().copy()
    cand.loc[cand.index[-1], "close"] = 130.0
    cand.loc[cand.index[-1], "high"] = 130.5
    result = evaluate_setup_a(
        symbol="NVDA",
        now_et=_et_dt(date(2026, 5, 27), time(9, 50)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min(date(2026, 5, 27)),
        cand_5min_df=cand,
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "stop_at_or_midpoint_within_atr_cap" in failed_names


# ---------------------------------------------------------------------------
# Setup B — qualifying
# ---------------------------------------------------------------------------


def _setup_b_candidate_passing() -> pd.DataFrame:
    """Sequence engineered so all 10 conditions pass:
    - 09:30 bar: high=130 → prior intraday high (for the 2R-target test)
    - 09:40 bar: low=98 dips below VWAP=99 → the touch
    - 09:45 (reclaim): open=100, close=105 (green), above VWAP=99.5, atr=6.0
    Stop = touch_low (98) − 0.25 × ATR (6) = 96.5; stop_dist = 8.5; cap = 9.0.
    R = 8.5 → 2R target = 17 → prior_high must be ≥ 122. Bar 0's high=130 ✓.
    No-chase: 105 − touch_vwap(99) = 6 vs cap 9 → OK.
    """
    return _5min_bars(
        date(2026, 5, 27), [0, 5, 10, 15],
        opens=[105, 103, 100, 100],
        highs=[130, 105, 100, 106],
        lows=[104, 102, 98, 99],
        closes=[105, 103, 99, 105],
        volumes=[100, 100, 100, 300],
        vwaps=[100, 100, 99, 99.5],
        ema9s=[100, 100, 100, 102],
        ema20s=[99, 99, 99, 100],
        atrs=[5.0, 5.0, 5.0, 6.0],
        bar_rvols=[1.0, 1.0, 1.0, 1.5],
    )


def _bullish_spy_5min_strict(session_date: date) -> pd.DataFrame:
    """SPY 5-min that classifies strictly BULLISH for Setup B."""
    return _bullish_spy_5min(session_date)


def test_setup_b_qualifies_when_all_conditions_pass():
    result = evaluate_setup_b(
        symbol="AAPL",
        now_et=_et_dt(date(2026, 5, 27), time(10, 0)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min_strict(date(2026, 5, 27)),
        cand_5min_df=_setup_b_candidate_passing(),
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    failed = [c for c in result["conditions"] if not c["passed"]]
    assert result["qualified"], f"failed: {failed}"
    assert result["entry"] == pytest.approx(105.0)
    # Stop = touch_low (98) - 0.25 * atr (6) = 96.5
    assert result["stop"] == pytest.approx(96.5)


def test_setup_b_fails_in_dead_zone_between_windows():
    result = evaluate_setup_b(
        symbol="AAPL",
        now_et=_et_dt(date(2026, 5, 27), time(12, 30)),  # 11:30-14:00 dead zone
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min_strict(date(2026, 5, 27)),
        cand_5min_df=_setup_b_candidate_passing(),
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "in_setup_b_time_window" in failed_names


def test_setup_b_fails_when_no_vwap_touch_exists():
    # Price has been steadily above VWAP — no pullback touched it.
    cand = _5min_bars(
        date(2026, 5, 27), [0, 5, 10, 15],
        opens=[105, 106, 107, 108],
        highs=[106, 107, 108, 110],
        lows=[105, 106, 107, 108],   # all lows are well above VWAP
        closes=[106, 107, 108, 109],
        volumes=[100, 100, 100, 300],
        vwaps=[100, 100.5, 101, 101.5],
        ema9s=[105, 105.5, 106, 107],
        ema20s=[104, 104.5, 105, 105.5],
        atrs=[2, 2, 2, 2],
        bar_rvols=[1.0, 1.0, 1.0, 1.5],
    )
    result = evaluate_setup_b(
        symbol="AAPL",
        now_et=_et_dt(date(2026, 5, 27), time(10, 0)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min_strict(date(2026, 5, 27)),
        cand_5min_df=cand,
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "vwap_touch_in_recent_pullback" in failed_names


def test_setup_b_fails_when_reclaim_candle_not_green():
    cand = _setup_b_candidate_passing().copy()
    cand.loc[cand.index[-1], "open"] = 106
    cand.loc[cand.index[-1], "close"] = 105  # close < open → red
    result = evaluate_setup_b(
        symbol="AAPL",
        now_et=_et_dt(date(2026, 5, 27), time(10, 0)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min_strict(date(2026, 5, 27)),
        cand_5min_df=cand,
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "green_5min_close_above_vwap" in failed_names


def test_setup_b_rejects_mixed_intraday_character():
    spy = _5min_bars(
        date(2026, 5, 27), [0],
        opens=[400], highs=[402], lows=[399], closes=[402], volumes=[1000],
        vwaps=[400.5], ema9s=[402.5], ema20s=[400.0],  # close < ema9 → MIXED
    )
    result = evaluate_setup_b(
        symbol="AAPL",
        now_et=_et_dt(date(2026, 5, 27), time(10, 0)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=spy,
        cand_5min_df=_setup_b_candidate_passing(),
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )
    assert not result["qualified"]
    failed_names = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert "daily_regime_bullish_and_intraday_bullish" in failed_names


# ---------------------------------------------------------------------------
# Tie-breaker
# ---------------------------------------------------------------------------


def test_pick_winner_setup_a_wins_when_both_qualify():
    a = {"setup": "A", "qualified": True}
    b = {"setup": "B", "qualified": True}
    assert pick_winner(a, b) is a


def test_pick_winner_returns_only_qualifying():
    a = {"setup": "A", "qualified": False}
    b = {"setup": "B", "qualified": True}
    assert pick_winner(a, b) is b


def test_pick_winner_returns_none_when_neither_qualifies():
    a = {"setup": "A", "qualified": False}
    b = {"setup": "B", "qualified": False}
    assert pick_winner(a, b) is None


# ---------------------------------------------------------------------------
# Stop-distance FLOOR (0.3%) — qualifier must not emit setups the execution
# gate will reject as "stop_too_tight_under_0.3pct". Reproduces the live
# NFLX (-0.21%) / AAPL (-0.14%) QUALIFIED→SKIP pairs.
# ---------------------------------------------------------------------------


def _setup_b_with_stop(touch_low: float, atr: float = 0.4) -> pd.DataFrame:
    """Setup-B candidate (entry=100) where every condition passes EXCEPT the
    stop distance, which is governed by `touch_low`:
        stop = touch_low - 0.25*atr ;  stop_dist = 100 - stop.
    Lower touch_low → wider stop. Lets us straddle the 0.3% floor (=0.30 at
    entry 100) while holding the rest of the geometry fixed.
    """
    vwap_at_touch = touch_low + 0.05  # low <= vwap → registers as a touch
    return _5min_bars(
        date(2026, 5, 27), [0, 5, 10, 15],
        opens=[100.5, 100.4, 100.0, 99.6],
        highs=[101.0, 100.5, 100.05, 100.1],   # bar-0 high=101 → prior high
        lows=[100.2, 100.0, touch_low, 99.8],
        closes=[100.4, 100.1, 99.9, 100.0],    # last bar green, > vwap
        volumes=[100, 100, 100, 300],
        vwaps=[100.0, 99.95, vwap_at_touch, 99.9],
        ema9s=[100.0, 100.0, 99.95, 100.1],
        ema20s=[99.7, 99.7, 99.7, 99.8],
        atrs=[atr, atr, atr, atr],
        bar_rvols=[1.0, 1.0, 1.0, 1.5],
    )


def _eval_b(cand):
    return evaluate_setup_b(
        symbol="AAPL",
        now_et=_et_dt(date(2026, 5, 27), time(10, 0)),
        spy_daily_df=_bullish_spy_daily(),
        spy_5min_df=_bullish_spy_5min_strict(date(2026, 5, 27)),
        cand_5min_df=cand,
        has_position=False,
        in_earnings_blackout=False,
        overnight_gap_pct=0.005,
    )


def test_setup_b_rejects_stop_tighter_than_floor():
    # touch_low=99.85 → stop=99.75 → stop_dist=0.25 = 0.25% < 0.3% floor.
    result = _eval_b(_setup_b_with_stop(touch_low=99.85))
    failed = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert not result["qualified"], f"should reject tight stop; failed={failed}"
    # The stop condition must be the (only) reason — the floor is what bites.
    assert failed == {"stop_below_vwap_touch_within_atr_cap"}, failed


def test_setup_b_accepts_stop_just_above_floor():
    # touch_low=99.70 → stop=99.60 → stop_dist=0.40 = 0.40% > 0.3% floor.
    result = _eval_b(_setup_b_with_stop(touch_low=99.70))
    failed = {c["name"] for c in result["conditions"] if not c["passed"]}
    assert result["qualified"], f"adequate stop should still qualify; failed={failed}"
