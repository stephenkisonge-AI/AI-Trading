"""Pure-math indicators. No I/O, no Alpaca, no pandas operations that depend on index."""
from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """SMA-seeded EMA, matching scripts/compute_regime.py exactly.

    First (period-1) values are NaN, value at index (period-1) is the
    simple average of the first `period` values, and subsequent values
    apply standard exponential smoothing with k = 2 / (period + 1).
    """
    values = series.to_list()
    n = len(values)
    if n < period:
        return pd.Series([float("nan")] * n, index=series.index, dtype="float64")

    out: list[float] = [float("nan")] * (period - 1)
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out.append(e)
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return pd.Series(out, index=series.index, dtype="float64")


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. First `period` values are NaN; value at index `period`
    uses the simple average of the first `period` gains/losses; subsequent
    values use Wilder smoothing: avg = (prev_avg * (n-1) + current) / n.
    """
    values = series.to_list()
    n = len(values)
    out: list[float] = [float("nan")] * n
    if n <= period:
        return pd.Series(out, index=series.index, dtype="float64")

    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for i in range(1, n):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)

    return pd.Series(out, index=series.index, dtype="float64")


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR. df must have 'high', 'low', 'close' columns. First
    `period` values are NaN; value at index `period` uses the simple
    average of the first `period` true-range values; subsequent values
    use Wilder smoothing.
    """
    highs = df["high"].to_list()
    lows = df["low"].to_list()
    closes = df["close"].to_list()
    n = len(closes)
    out: list[float] = [float("nan")] * n
    if n <= period:
        return pd.Series(out, index=df.index, dtype="float64")

    tr: list[float] = [float("nan")]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr.append(max(hl, hc, lc))

    seed = sum(tr[1 : period + 1]) / period
    out[period] = seed
    a = seed
    for i in range(period + 1, n):
        a = (a * (period - 1) + tr[i]) / period
        out[i] = a

    return pd.Series(out, index=df.index, dtype="float64")


def volume_sma(volume_series: pd.Series, period: int = 20) -> pd.Series:
    """Simple rolling average of volume over `period` bars."""
    return volume_series.rolling(window=period, min_periods=period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with EMA20/50/200, RSI14, ATR14, vol_sma20 columns added.

    Expects columns: open, high, low, close, volume.
    """
    out = df.copy()
    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["ema200"] = ema(out["close"], 200)
    out["rsi14"] = rsi(out["close"], 14)
    out["atr14"] = atr(out, 14)
    out["vol_sma20"] = volume_sma(out["volume"], 20)
    return out
