"""One-off scan driven by saved MCP bar payloads.

Loads daily / 4H / 1H crypto bars from JSON files (whatever MCP's
get_crypto_bars saved), runs the same indicator + setup evaluation
the watcher uses, and prints a compact summary.

Not part of the production loop — this is for Claude-driven on-demand
scans inside Claude Code when the MCP returns too much data to inline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.indicators import add_indicators  # noqa: E402
from src.strategy import (  # noqa: E402
    classify_regime,
    compute_regime_details,
    evaluate_setup_a,
    evaluate_setup_b,
)


def load_bars(path: Path) -> dict[str, pd.DataFrame]:
    raw = json.loads(path.read_text())
    out: dict[str, pd.DataFrame] = {}
    for symbol, bars in raw["bars"].items():
        df = pd.DataFrame(bars)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        out[symbol] = df
    return out


def drop_in_progress(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) > 0:
        return df.iloc[:-1].copy()
    return df


def main(daily_path: str, h4_path: str, h1_path: str) -> int:
    daily_by_sym = load_bars(Path(daily_path))
    h4_by_sym = load_bars(Path(h4_path))
    h1_by_sym = load_bars(Path(h1_path))

    symbols = list(daily_by_sym.keys())
    print(f"Scanning {len(symbols)} symbols: {symbols}\n")

    for symbol in symbols:
        daily = drop_in_progress(daily_by_sym[symbol])
        h4 = add_indicators(drop_in_progress(h4_by_sym[symbol]))
        h1 = add_indicators(drop_in_progress(h1_by_sym[symbol]))

        details = compute_regime_details(daily)
        regime = details["regime"]
        last_close = daily["close"].iloc[-1]

        setup_a = evaluate_setup_a(daily, h4, h1, symbol, has_position=False)
        setup_b = evaluate_setup_b(daily, h4, h1, symbol, has_position=False)

        print(f"=== {symbol} ===")
        print(
            f"  Daily: close={last_close:.4f} regime={regime} "
            f"e50={details.get('ema50'):.4f} e200={details.get('ema200'):.4f} "
            f"close_vs_e200={details.get('close_vs_ema200_pct'):+.2f}%"
        )
        for setup_result in (setup_a, setup_b):
            tag = f"Setup {setup_result['setup']}"
            ok = "QUALIFIED" if setup_result["qualified"] else "skip"
            passed = sum(1 for c in setup_result["conditions"] if c["passed"])
            total = len(setup_result["conditions"])
            print(f"  {tag}: {ok} ({passed}/{total} conditions)")
            if not setup_result["qualified"]:
                failed = [c for c in setup_result["conditions"] if not c["passed"]]
                for c in failed:
                    print(f"      x {c['name']}: {c['detail']}")
            else:
                print(f"      entry={setup_result['entry']:.4f} "
                      f"stop={setup_result['stop']:.4f} "
                      f"atr={setup_result['atr']:.4f}")
        print()

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: one_off_scan.py <daily.json> <4h.json> <1h.json>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
