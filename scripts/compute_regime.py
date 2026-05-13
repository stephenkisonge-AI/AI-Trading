"""Thin CLI wrapper that delegates regime classification to src.strategy.

Reads a bars JSON file (same shape as the old standalone script) and prints
the same output. The real logic now lives in src/strategy.py — see Phase 3
of build-watcher-system.md for the refactor rationale.
"""
import json
import sys
from pathlib import Path

# Allow `python scripts/compute_regime.py ...` from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from src.strategy import compute_regime_details


def main(path: str) -> None:
    raw = json.loads(Path(path).read_text())
    bars = raw["bars"]
    for sym in ("BTC/USD", "ETH/USD"):
        if sym not in bars:
            print(f"\n{sym}: no data")
            continue
        sym_bars = bars[sym]
        closes = [b["c"] for b in sym_bars]
        print(f"\n=== {sym} — {len(closes)} daily candles ===")
        if sym_bars:
            print(f"First: {sym_bars[0]['t']}  Last: {sym_bars[-1]['t']}")
        df = pd.DataFrame({"close": closes})
        info = compute_regime_details(df)
        for k, v in info.items():
            if isinstance(v, float):
                print(f"  {k}: {v:.4f}")
            else:
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main(sys.argv[1])
