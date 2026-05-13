"""Tests for src/strategy.py — regime classification only (Setup A/B come later)."""
import pandas as pd
import pytest

from src.strategy import classify_regime, compute_regime_details


def _df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


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
