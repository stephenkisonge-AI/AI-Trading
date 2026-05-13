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
