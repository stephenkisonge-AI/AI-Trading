"""Compute daily EMAs and regime label for BTC/USD and ETH/USD from saved bars."""
import json
import sys
from pathlib import Path


def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def emas_series(values, period):
    """Return full EMA series aligned to input length (None until period reached)."""
    if len(values) < period:
        return [None] * len(values)
    out = [None] * (period - 1)
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out.append(e)
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def classify(closes):
    if len(closes) < 200:
        return "INSUFFICIENT DATA", {}
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    last = closes[-1]
    info = {
        "last_close": last,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "close_vs_ema200_pct": (last - e200) / e200 * 100,
    }
    # Check if EMA50 crossed above EMA200 in last 10 daily candles
    e50_series = emas_series(closes, 50)
    e200_series = emas_series(closes, 200)
    crossed_up_recent = False
    for i in range(max(1, len(closes) - 10), len(closes)):
        prev50, prev200 = e50_series[i - 1], e200_series[i - 1]
        cur50, cur200 = e50_series[i], e200_series[i]
        if all(v is not None for v in (prev50, prev200, cur50, cur200)):
            if prev50 < prev200 and cur50 >= cur200:
                crossed_up_recent = True
                break
    info["ema50_crossed_above_recently"] = crossed_up_recent

    # Choppy neutral check: oscillating within 5% of EMA200 over 20 candles
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
    return regime, info


def main(path):
    raw = json.loads(Path(path).read_text())
    bars = raw["bars"]
    for sym in ("BTC/USD", "ETH/USD"):
        if sym not in bars:
            print(f"\n{sym}: no data")
            continue
        closes = [b["c"] for b in bars[sym]]
        print(f"\n=== {sym} — {len(closes)} daily candles ===")
        if closes:
            print(f"First: {bars[sym][0]['t']}  Last: {bars[sym][-1]['t']}")
        regime, info = classify(closes)
        for k, v in info.items():
            if isinstance(v, float):
                print(f"  {k}: {v:.4f}")
            else:
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main(sys.argv[1])
