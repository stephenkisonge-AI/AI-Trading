"""Tests for src/strategy.py — regime classification and setup evaluators."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.strategy import (
    _find_recent_breakout,
    _find_retest,
    _find_swing_lows,
    classify_regime,
    compute_regime_details,
    evaluate_setup_a,
    evaluate_setup_b,
)


def _df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


def _daily_bullish(n: int = 250) -> pd.DataFrame:
    """Strictly increasing closes → BULLISH regime."""
    return _df([100.0 + i for i in range(n)])


def _daily_bearish(n: int = 250) -> pd.DataFrame:
    """Strictly decreasing closes → BEARISH regime."""
    return _df([400.0 - i for i in range(n)])


def _daily_improving(n: int = 250) -> pd.DataFrame:
    """Pattern that produces IMPROVING_NEUTRAL."""
    return _df([200.0] * 50 + [100.0] * 150 + [130.0] * 50)


def _h4_for_setup_a(
    *,
    close: float = 200.0,
    open_: float = 199.0,
    ema20: float = 199.5,
    ema50: float = 195.0,
    ema200: float = 180.0,
    rsi14: float = 42.0,
    atr14: float = 2.0,
    volume: float = 1000.0,
    vol_sma20: float = 900.0,
    swing_low: float = 197.0,
    prior_swing_low: float = 190.0,
) -> pd.DataFrame:
    """Build a 4H DataFrame whose last bar satisfies Setup A geometry by
    construction. Lows are shaped so two swing lows exist with the most
    recent one higher than the prior one (higher-low structure intact)
    and current close above it.
    """
    n = 250
    ts = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    lows = [close - 1.0] * n
    # Plant two swing lows: prior at index 100, recent at index 200
    # Each swing low needs ±3 neighbors strictly higher
    for i, idx in enumerate((100, 200)):
        value = prior_swing_low if i == 0 else swing_low
        lows[idx] = value
        for j in range(1, 4):
            lows[idx - j] = value + j  # left neighbors higher
            lows[idx + j] = value + j  # right neighbors higher
    df = pd.DataFrame(
        {
            "open": [open_] * n,
            "high": [close + 1.0] * n,
            "low": lows,
            "close": [close] * n,
            "volume": [volume] * n,
            "ema20": [ema20] * n,
            "ema50": [ema50] * n,
            "ema200": [ema200] * n,
            "rsi14": [rsi14] * n,
            "atr14": [atr14] * n,
            "vol_sma20": [vol_sma20] * n,
        },
        index=ts,
    )
    return df


def _h1_for_setup_a(
    *,
    close: float = 200.0,
    open_: float = 199.5,
    ema20: float = 198.0,
    prior_close: float = 197.0,
    prior_ema20: float = 198.0,
    volume: float = 1000.0,
    vol_sma20: float = 900.0,
) -> pd.DataFrame:
    """Last two 1H bars: prior bar at/below EMA20, latest green and reclaiming."""
    n = 50
    ts = pd.date_range("2026-04-01", periods=n, freq="1h", tz="UTC")
    closes = [prior_close - 0.5] * n
    opens = [prior_close - 0.5] * n
    ema20s = [ema20] * n
    closes[-2] = prior_close
    opens[-2] = prior_close
    ema20s[-2] = prior_ema20
    closes[-1] = close
    opens[-1] = open_
    ema20s[-1] = ema20
    df = pd.DataFrame(
        {
            "open": opens,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [volume] * n,
            "ema20": ema20s,
            "ema50": [ema20 - 1] * n,
            "ema200": [ema20 - 5] * n,
            "rsi14": [50.0] * n,
            "atr14": [1.0] * n,
            "vol_sma20": [vol_sma20] * n,
        },
        index=ts,
    )
    return df


def test_regime_insufficient_data_under_200_closes():
    assert classify_regime(_df([100.0] * 100)) == "INSUFFICIENT DATA"


def test_regime_bullish_strictly_increasing():
    # 250 strictly increasing closes — close >> EMA200, EMA50 >> EMA200
    closes = [100.0 + i for i in range(250)]
    assert classify_regime(_df(closes)) == "BULLISH"


def test_regime_bearish_strictly_decreasing():
    # 250 strictly decreasing closes — close << EMA200, EMA50 << EMA200
    closes = [400.0 - i for i in range(250)]
    assert classify_regime(_df(closes)) == "BEARISH"


def test_regime_improving_neutral_recovery_from_downtrend():
    # Sharp early high, long flat valley, recent recovery.
    # At the final bar: close > EMA200 (just barely), EMA50 still < EMA200,
    # and EMA50 has NOT crossed up in the last 10 bars (still below).
    closes = (
        [200.0] * 50          # early high — anchors EMA200 above current levels
        + [100.0] * 150       # long valley — pulls EMA50 down to ~100
        + [130.0] * 50        # recovery — close above EMA200, EMA50 lags
    )
    details = compute_regime_details(_df(closes))
    # Sanity-check the geometry that gives us this regime
    assert details["last_close"] > details["ema200"], (
        f"expected close>EMA200 for IMPROVING_NEUTRAL, "
        f"got close={details['last_close']:.2f} EMA200={details['ema200']:.2f}"
    )
    assert details["ema50"] < details["ema200"], (
        f"expected EMA50<EMA200, got EMA50={details['ema50']:.2f} EMA200={details['ema200']:.2f}"
    )
    assert details["regime"] == "IMPROVING_NEUTRAL"


def test_regime_choppy_neutral_pullback_below_ema200():
    # CHOPPY_NEUTRAL only fires when close < EMA200 AND EMA50 >= EMA200
    # AND the last 20 closes are within 5% of EMA200. Geometry: long flat
    # base, brief rally that lifts EMA200 above 100, then return to 100.
    # At the final bar: close=100 < EMA200~102, EMA50~103 > EMA200~102,
    # and |100 - 102|/102 ≈ 2% over the last 20 bars.
    closes = [100.0] * 200 + [110.0] * 30 + [100.0] * 20
    details = compute_regime_details(_df(closes))
    assert details["last_close"] < details["ema200"]
    assert details["ema50"] >= details["ema200"]
    assert details["regime"] == "CHOPPY_NEUTRAL"


def test_regime_details_includes_expected_keys_in_order():
    closes = [100.0 + i for i in range(250)]
    details = compute_regime_details(_df(closes))
    expected_keys = [
        "last_close",
        "ema20",
        "ema50",
        "ema200",
        "close_vs_ema200_pct",
        "ema50_crossed_above_recently",
        "regime",
    ]
    assert list(details.keys()) == expected_keys


def test_classify_regime_returns_same_label_as_details():
    closes = [100.0 + i for i in range(250)]
    df = _df(closes)
    assert classify_regime(df) == compute_regime_details(df)["regime"]


# ---------------------------------------------------------------------------
# Helper tests: _find_swing_lows, _find_recent_breakout, _find_retest
# ---------------------------------------------------------------------------


def test_find_swing_lows_detects_single_v_bottom():
    # Clear V at index 9 with ±3 neighbors all strictly higher
    lows = [10, 9, 8, 7, 8, 9, 10, 11, 10, 7, 10, 11, 10, 9, 8]
    df = pd.DataFrame({"low": lows})
    result = _find_swing_lows(df, window=3)
    # index 9 is the V bottom
    indices = [idx for idx, _ in result]
    assert 9 in indices
    low_at_9 = next(v for idx, v in result if idx == 9)
    assert low_at_9 == 7


def test_find_swing_lows_excludes_unconfirmed_recent_bars():
    # A low in the last `window` bars cannot be confirmed (no right neighbors)
    n = 30
    lows = [10.0] * n
    lows[28] = 1.0  # very low, but only 1 bar after it → not confirmed
    df = pd.DataFrame({"low": lows})
    result = _find_swing_lows(df, window=3)
    indices = [idx for idx, _ in result]
    assert 28 not in indices


def test_find_swing_lows_higher_low_pattern():
    # Two swing lows: i=10 at low=8, i=20 at low=9 (higher low)
    n = 30
    lows = [12.0] * n
    for j in range(1, 4):
        lows[10 - j] = 9.0
        lows[10 + j] = 9.0
    lows[10] = 8.0
    for j in range(1, 4):
        lows[20 - j] = 10.0
        lows[20 + j] = 10.0
    lows[20] = 9.0
    df = pd.DataFrame({"low": lows})
    result = _find_swing_lows(df, window=3)
    assert len(result) >= 2
    assert result[-1][1] > result[-2][1], "most recent swing low should be higher"


def test_find_recent_breakout_detects_high_above_lookback():
    # 30 bars: flat at 100, then bar 25 spikes to 110 (above 20-period high of 100)
    n = 30
    highs = [100.0] * n
    closes = [99.0] * n
    volumes = [1000.0] * n
    highs[25] = 110.0
    closes[25] = 109.0
    volumes[25] = 2000.0
    ts = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame(
        {"high": highs, "close": closes, "volume": volumes}, index=ts
    )
    result = _find_recent_breakout(df)
    assert result is not None
    assert result["index"] == 25
    assert result["level"] == 100.0
    assert result["close"] == 109.0


def test_find_recent_breakout_returns_none_when_no_break():
    n = 30
    df = pd.DataFrame(
        {"high": [100.0] * n, "close": [99.0] * n, "volume": [1000.0] * n},
        index=pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
    )
    assert _find_recent_breakout(df) is None


def test_find_retest_finds_qualifying_pair():
    # Bars: low touches level=100 within 0.5%, next bar green and closes above
    ts = pd.date_range("2026-01-01", periods=10, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [101.0, 101.0, 101.0, 101.0, 101.0, 100.2, 100.5, 101.0, 101.0, 101.0],
            "low":  [101.0, 101.0, 101.0, 101.0, 101.0,  99.8,  100.4, 101.0, 101.0, 101.0],
            "close":[101.0, 101.0, 101.0, 101.0, 101.0, 100.5, 101.0, 101.0, 101.0, 101.0],
            "high": [101.5, 101.5, 101.5, 101.5, 101.5, 100.6, 101.2, 101.5, 101.5, 101.5],
        },
        index=ts,
    )
    # Pass an "after" timestamp before all bars so the whole series is scanned
    result = _find_retest(df, level=100.0, after_ts=ts[0] - timedelta(hours=1))
    assert result is not None
    assert result["touch_ts"] == ts[5]
    assert result["confirm_ts"] == ts[6]


def test_find_retest_returns_none_when_level_not_touched():
    ts = pd.date_range("2026-01-01", periods=10, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open":  [110.0] * 10,
            "low":   [109.0] * 10,
            "close": [110.5] * 10,
            "high":  [111.0] * 10,
        },
        index=ts,
    )
    assert _find_retest(df, level=100.0, after_ts=ts[0] - timedelta(hours=1)) is None


# ---------------------------------------------------------------------------
# evaluate_setup_a tests — regime gate, has_position gate, qualifying happy path
# ---------------------------------------------------------------------------


def test_setup_a_rejects_when_regime_bearish():
    h4 = _h4_for_setup_a()
    h1 = _h1_for_setup_a()
    result = evaluate_setup_a(_daily_bearish(), h4, h1, "BTC/USD", has_position=False)
    assert result["qualified"] is False
    assert result["setup"] == "A"
    cond1 = next(c for c in result["conditions"] if c["name"] == "daily_regime_bullish_or_improving")
    assert cond1["passed"] is False


def test_setup_a_rejects_when_has_position():
    h4 = _h4_for_setup_a()
    h1 = _h1_for_setup_a()
    result = evaluate_setup_a(_daily_bullish(), h4, h1, "BTC/USD", has_position=True)
    assert result["qualified"] is False
    cond8 = next(c for c in result["conditions"] if c["name"] == "no_existing_position")
    assert cond8["passed"] is False


def test_setup_a_qualifies_on_happy_path():
    h4 = _h4_for_setup_a()
    h1 = _h1_for_setup_a()
    result = evaluate_setup_a(_daily_bullish(), h4, h1, "BTC/USD", has_position=False)
    failed = [c for c in result["conditions"] if not c["passed"]]
    assert not failed, f"unexpected failing conditions: {failed}"
    assert result["qualified"] is True
    assert result["entry"] == pytest.approx(200.0)
    assert result["stop"] == pytest.approx(197.0)
    assert result["atr"] == pytest.approx(2.0)


def test_setup_a_improving_neutral_regime_also_qualifies():
    # Setup A allows BULLISH or IMPROVING_NEUTRAL
    h4 = _h4_for_setup_a()
    h1 = _h1_for_setup_a()
    result = evaluate_setup_a(_daily_improving(), h4, h1, "BTC/USD", has_position=False)
    cond1 = next(c for c in result["conditions"] if c["name"] == "daily_regime_bullish_or_improving")
    assert cond1["passed"] is True


def test_setup_a_rejects_when_h1_prior_close_above_ema20():
    # Stricter "reclaim" check should reject if prior 1H close was already above EMA20
    h4 = _h4_for_setup_a()
    h1 = _h1_for_setup_a(prior_close=199.0)  # already above ema20=198
    result = evaluate_setup_a(_daily_bullish(), h4, h1, "BTC/USD", has_position=False)
    cond5 = next(c for c in result["conditions"] if c["name"] == "h1_green_close_reclaims_ema20")
    assert cond5["passed"] is False


# ---------------------------------------------------------------------------
# evaluate_setup_b tests — regime gate, breakout detection, happy path
# ---------------------------------------------------------------------------


def _h4_for_setup_b(
    *,
    n: int = 250,
    breakout_offset_from_end: int = 5,
    breakout_level: float = 100.0,
    breakout_close: float = 105.0,
    breakout_volume: float = 2000.0,
    final_close: float = 102.0,
    rsi14: float = 60.0,
    atr14: float = 3.0,
    vol_sma20: float = 1000.0,
) -> pd.DataFrame:
    """4H DataFrame with a planted breakout in the last 10 bars."""
    ts = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    highs = [breakout_level] * n  # baseline high = the level
    lows = [breakout_level - 5.0] * n
    closes = [breakout_level - 1.0] * n
    opens = [breakout_level - 1.0] * n
    volumes = [1000.0] * n
    bo_idx = n - breakout_offset_from_end
    highs[bo_idx] = breakout_close + 1.0
    closes[bo_idx] = breakout_close
    volumes[bo_idx] = breakout_volume
    # After breakout, hold near the level
    for i in range(bo_idx + 1, n):
        closes[i] = final_close
        opens[i] = final_close - 0.5
        highs[i] = final_close + 1.0
        lows[i] = final_close - 1.0
    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "ema20": [breakout_level] * n,
            "ema50": [breakout_level] * n,
            "ema200": [breakout_level - 10.0] * n,
            "rsi14": [rsi14] * n,
            "atr14": [atr14] * n,
            "vol_sma20": [vol_sma20] * n,
        },
        index=ts,
    )
    return df


def _h1_for_setup_b_retest(
    *,
    level: float = 100.0,
    after_ts: datetime,
) -> pd.DataFrame:
    """1H bars after `after_ts` containing a retest: touch within 0.5%, then
    green confirmation.
    """
    start = after_ts + timedelta(hours=1)
    ts = pd.date_range(start, periods=10, freq="1h")
    lows = [level + 1.0] * 10
    opens = [level + 1.0] * 10
    closes = [level + 1.0] * 10
    highs = [level + 1.5] * 10
    lows[5] = level - 0.3       # within 0.5% (0.3%)
    opens[5] = level + 0.2
    closes[5] = level + 0.1
    opens[6] = level + 0.1
    closes[6] = level + 0.5     # green confirm above level
    highs[6] = level + 0.6
    df = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000.0] * 10,
            "ema20": [level] * 10,
            "ema50": [level] * 10,
            "ema200": [level - 5.0] * 10,
            "rsi14": [55.0] * 10,
            "atr14": [1.0] * 10,
            "vol_sma20": [900.0] * 10,
        },
        index=ts,
    )
    return df


def test_setup_b_rejects_when_regime_improving_neutral():
    # Setup B requires BULLISH only — not IMPROVING_NEUTRAL
    h4 = _h4_for_setup_b()
    h1 = _h1_for_setup_b_retest(after_ts=h4.index[-5])
    result = evaluate_setup_b(_daily_improving(), h4, h1, "BTC/USD", has_position=False)
    cond1 = next(c for c in result["conditions"] if c["name"] == "daily_regime_bullish")
    assert cond1["passed"] is False


def test_setup_b_rejects_when_no_breakout():
    # 4H bars with no high above 20-period prior high
    n = 250
    ts = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    h4 = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [100.0] * n,
            "low": [99.0] * n,
            "close": [99.5] * n,
            "volume": [1000.0] * n,
            "ema20": [100.0] * n,
            "ema50": [100.0] * n,
            "ema200": [90.0] * n,
            "rsi14": [55.0] * n,
            "atr14": [2.0] * n,
            "vol_sma20": [900.0] * n,
        },
        index=ts,
    )
    h1 = _h1_for_setup_b_retest(after_ts=h4.index[-1])
    result = evaluate_setup_b(_daily_bullish(), h4, h1, "BTC/USD", has_position=False)
    cond2 = next(c for c in result["conditions"] if c["name"] == "breakout_above_20period_high_in_last_10")
    assert cond2["passed"] is False


def test_setup_b_qualifies_on_happy_path():
    h4 = _h4_for_setup_b()
    breakout_idx = -5
    h1 = _h1_for_setup_b_retest(after_ts=h4.index[breakout_idx])
    result = evaluate_setup_b(_daily_bullish(), h4, h1, "BTC/USD", has_position=False)
    failed = [c for c in result["conditions"] if not c["passed"]]
    assert not failed, f"unexpected failing conditions: {failed}"
    assert result["qualified"] is True
    assert result["entry"] == pytest.approx(102.0)
    assert result["stop"] == pytest.approx(100.0)


def test_setup_b_no_chasing_rule_rejects_runaway_breakout():
    # Single big "marubozu" breakout on the LAST bar: low=100, close=120 — so
    # _find_recent_breakout sees exactly one breakout at level=100. ATR=3,
    # threshold for no-chase = 100 + 2×3 = 106; close 120 is way above →
    # no_chase_violation fires AND stop_dist (20) is well above 1.5×ATR (4.5).
    n = 250
    ts = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    highs = [100.0] * n  # prior 20-period high = 100 everywhere
    closes = [99.0] * n
    opens = [99.0] * n
    lows = [98.0] * n
    volumes = [1000.0] * n
    # Plant runaway breakout on the very last bar
    highs[-1] = 121.0
    closes[-1] = 120.0
    opens[-1] = 100.0
    lows[-1] = 100.0
    volumes[-1] = 5000.0
    h4 = pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "ema20": [99.0] * n,
            "ema50": [99.0] * n,
            "ema200": [95.0] * n,
            "rsi14": [55.0] * n,
            "atr14": [3.0] * n,
            "vol_sma20": [900.0] * n,
        },
        index=ts,
    )
    # H1 bars that never come down to touch the level → no retest
    h1_ts = pd.date_range(h4.index[-1] + timedelta(hours=1), periods=10, freq="1h")
    h1 = pd.DataFrame(
        {
            "open":  [120.0] * 10,
            "high":  [121.0] * 10,
            "low":   [119.0] * 10,
            "close": [120.5] * 10,
            "volume": [1000.0] * 10,
            "ema20": [119.0] * 10,
            "ema50": [115.0] * 10,
            "ema200": [100.0] * 10,
            "rsi14": [55.0] * 10,
            "atr14": [1.0] * 10,
            "vol_sma20": [900.0] * 10,
        },
        index=h1_ts,
    )
    result = evaluate_setup_b(_daily_bullish(), h4, h1, "BTC/USD", has_position=False)
    cond7 = next(c for c in result["conditions"] if c["name"] == "stop_below_retested_level_within_atr_cap")
    assert cond7["passed"] is False
    assert "no_chase_violation=True" in cond7["detail"]
