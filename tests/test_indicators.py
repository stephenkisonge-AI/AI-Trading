"""Tests for src/indicators.py against hand-computable reference values."""
import math

import pandas as pd
import pytest

from src.indicators import add_indicators, atr, ema, rsi, volume_sma


def test_ema_basic_hand_calc():
    # period=3, closes=[1,2,3,4,5]
    # seed at index 2 = (1+2+3)/3 = 2.0, k = 0.5
    # index 3: 4*0.5 + 2*0.5 = 3.0
    # index 4: 5*0.5 + 3*0.5 = 4.0
    result = ema(pd.Series([1, 2, 3, 4, 5], dtype="float64"), 3).to_list()
    assert math.isnan(result[0])
    assert math.isnan(result[1])
    assert result[2] == pytest.approx(2.0)
    assert result[3] == pytest.approx(3.0)
    assert result[4] == pytest.approx(4.0)


def test_ema_matches_compute_regime_logic():
    # Cross-check against the existing scripts/compute_regime.py.ema() implementation
    # on a longer sequence — same SMA seed + same k.
    closes = [float(x) for x in range(1, 51)]  # 1..50
    series = pd.Series(closes, dtype="float64")
    period = 20

    def ref_ema(values, period):
        k = 2.0 / (period + 1)
        e = sum(values[:period]) / period
        for v in values[period:]:
            e = v * k + e * (1 - k)
        return e

    expected_last = ref_ema(closes, period)
    actual_last = ema(series, period).iloc[-1]
    assert actual_last == pytest.approx(expected_last)


def test_ema_returns_nan_when_insufficient_data():
    result = ema(pd.Series([1.0, 2.0]), 5).to_list()
    assert all(math.isnan(v) for v in result)


def test_rsi_all_up_returns_100():
    # Strictly increasing → all gains, zero losses → RSI = 100
    closes = pd.Series([float(x) for x in range(1, 30)], dtype="float64")
    result = rsi(closes, 14).iloc[-1]
    assert result == pytest.approx(100.0)


def test_rsi_all_down_returns_0():
    # Strictly decreasing → zero gains, all losses → RSI = 0
    closes = pd.Series([float(x) for x in range(30, 0, -1)], dtype="float64")
    result = rsi(closes, 14).iloc[-1]
    assert result == pytest.approx(0.0)


def test_rsi_known_alternating_sequence():
    # Alternating +2/-1: gains avg = 1.0, losses avg = 0.5 over first 14 diffs
    # rs = 2.0, rsi = 100 - 100/3 ≈ 66.666...
    closes = pd.Series(
        [10, 12, 11, 13, 12, 14, 13, 15, 14, 16, 15, 17, 16, 18, 17],
        dtype="float64",
    )
    result = rsi(closes, 14).iloc[14]
    assert result == pytest.approx(66.6666666, abs=1e-4)


def test_rsi_nan_before_period():
    closes = pd.Series([float(x) for x in range(1, 30)], dtype="float64")
    result = rsi(closes, 14).to_list()
    for i in range(14):
        assert math.isnan(result[i]), f"index {i} should be NaN"
    assert not math.isnan(result[14])


def test_atr_constant_true_range():
    # Synthetic bars where TR is 1.5 for every bar (HL=1, HC=1.5, LC=0.5)
    # → seed at index 14 should be 1.5
    n = 15
    highs = [10.0 + i for i in range(n)]
    lows = [9.0 + i for i in range(n)]
    closes = [9.5 + i for i in range(n)]
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    result = atr(df, 14).iloc[14]
    assert result == pytest.approx(1.5)


def test_atr_nan_before_period():
    n = 15
    df = pd.DataFrame(
        {
            "high": [10.0 + i for i in range(n)],
            "low": [9.0 + i for i in range(n)],
            "close": [9.5 + i for i in range(n)],
        }
    )
    result = atr(df, 14).to_list()
    for i in range(14):
        assert math.isnan(result[i])
    assert not math.isnan(result[14])


def test_volume_sma_basic():
    # period=3 on [1,2,3,4,5]
    # index 2: (1+2+3)/3 = 2.0, index 3: 3.0, index 4: 4.0
    result = volume_sma(pd.Series([1, 2, 3, 4, 5], dtype="float64"), 3).to_list()
    assert math.isnan(result[0])
    assert math.isnan(result[1])
    assert result[2] == pytest.approx(2.0)
    assert result[3] == pytest.approx(3.0)
    assert result[4] == pytest.approx(4.0)


def test_add_indicators_produces_all_columns():
    # Need ≥200 rows for EMA200 to be non-NaN at the last index
    n = 250
    df = pd.DataFrame(
        {
            "open": [100.0 + i * 0.1 for i in range(n)],
            "high": [101.0 + i * 0.1 for i in range(n)],
            "low": [99.0 + i * 0.1 for i in range(n)],
            "close": [100.5 + i * 0.1 for i in range(n)],
            "volume": [1000.0 + i for i in range(n)],
        }
    )
    out = add_indicators(df)
    for col in ("ema20", "ema50", "ema200", "rsi14", "atr14", "vol_sma20"):
        assert col in out.columns, f"missing column: {col}"
        assert not math.isnan(out[col].iloc[-1]), f"{col} last value is NaN"


def test_add_indicators_does_not_mutate_input():
    n = 250
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1000.0] * n,
        }
    )
    original_cols = set(df.columns)
    _ = add_indicators(df)
    assert set(df.columns) == original_cols
