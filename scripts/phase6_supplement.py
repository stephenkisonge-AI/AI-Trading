"""Phase 6 supplement — robustness checks behind the replay report.

Usage:
    python scripts/phase6_supplement.py
        [--exp2 docs/phase6/experiment2_results.json]
        [--start 2024-01-01] [--end ...] [--cache-dir .replay_cache]
        [--out docs/phase6/supplement_results.json]

Three questions:
  1. SENSITIVITY — do Experiment 2's conclusions survive the
     OPTIMISTIC fill bound (wick-touch TPs booked before same-bar
     stops)? Re-simulates the same entry signals under both models.
  2. REGIME — how much of the window was even eligible for Setup A
     (BULLISH / IMPROVING_NEUTRAL), per symbol? Uses the production
     classifier on watcher-faithful daily slices.
  3. FEES — decompose each trade's net R into gross R and fee drag,
     against its stop distance, exposing the tight-structural-stop
     problem.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from scripts.phase6_replay import _WARMUP, fetch_bars_range
from src.replay import (
    FRAME_LEN,
    Signal,
    classify_regime,
    simulate_trade,
    summarize_trades,
)
from src.universe import CRYPTO_SYMBOLS


def rebuild_signals(exp2: dict) -> dict[str, list[Signal]]:
    """Reconstruct each variant's entered signals from the results
    JSON. Exits differ under the optimistic model but entries are the
    same signals (sparse enough that book overlap is negligible)."""
    out: dict[str, list[Signal]] = {"exact": [], "window": []}
    for symbol, sym in exp2["per_symbol"].items():
        for variant in ("exact", "window"):
            for tr in sym.get(variant, {}).get("trades", []):
                out[variant].append(Signal(
                    symbol=symbol,
                    scan_ts=pd.Timestamp(tr["signal_ts"]).to_pydatetime(),
                    regime="?",
                    exact=variant == "exact",
                    window=True,
                    entry=tr["entry"], stop=tr["stop0"], atr=tr["atr0"],
                ))
    return out


def regime_distribution(symbol: str, start: datetime, end: datetime,
                        cache_dir: Path) -> dict:
    """Regime label per completed UTC day, via the production
    classifier on trailing FRAME_LEN-day slices."""
    daily = fetch_bars_range(symbol, "1Day", start - _WARMUP["1Day"], end,
                             cache_dir)
    counts: dict[str, int] = {}
    day = start
    while day <= end:
        d = daily[daily.index < day.replace(hour=0, minute=0, second=0,
                                            microsecond=0)]
        d = d.tail(FRAME_LEN)
        label = classify_regime(d) if len(d) >= 200 else "INSUFFICIENT"
        counts[label] = counts.get(label, 0) + 1
        day += timedelta(days=1)
    total = sum(counts.values())
    eligible = (counts.get("BULLISH", 0)
                + counts.get("IMPROVING_NEUTRAL", 0))
    return {"days": total, "counts": counts,
            "setup_a_eligible_pct": 100.0 * eligible / total,
            "setup_b_eligible_pct": 100.0 * counts.get("BULLISH", 0) / total}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exp2", default="docs/phase6/experiment2_results.json")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--cache-dir", default=".replay_cache")
    parser.add_argument("--out", default="docs/phase6/supplement_results.json")
    args = parser.parse_args(argv)

    load_dotenv()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))
    cache_dir = Path(args.cache_dir)
    exp2 = json.loads(Path(args.exp2).read_text())

    # --- 1. fill-model sensitivity -------------------------------------
    signals = rebuild_signals(exp2)
    bars: dict[str, tuple] = {}
    for symbol in {s.symbol for v in signals.values() for s in v}:
        bars[symbol] = (
            fetch_bars_range(symbol, "1Day", start - _WARMUP["1Day"], end,
                             cache_dir),
            fetch_bars_range(symbol, "4Hour", start - _WARMUP["4Hour"], end,
                             cache_dir),
            fetch_bars_range(symbol, "1Hour", start - _WARMUP["1Hour"], end,
                             cache_dir),
        )

    sensitivity: dict = {}
    fee_rows: list[dict] = []
    for variant, sigs in signals.items():
        sensitivity[variant] = {}
        for model, optimistic in (("conservative", False),
                                  ("optimistic", True)):
            trades = []
            for sig in sigs:
                daily, h4, h1 = bars[sig.symbol]
                trades.append(simulate_trade(sig, variant, daily, h4, h1,
                                             optimistic=optimistic))
            sensitivity[variant][model] = summarize_trades(trades)
            for t in trades:
                fee_rows.append({
                    "variant": variant, "model": model,
                    "symbol": t.symbol, "signal_ts": str(t.signal_ts),
                    "stop_distance_pct":
                        100.0 * (t.entry - t.stop0) / t.entry,
                    "r_gross": round(t.r_gross(), 3),
                    "r_net": round(t.r_net(), 3),
                    "fee_drag_r": round(t.r_gross() - t.r_net(), 3),
                    "exits": t.exit_reasons(),
                })

    # --- 2. regime distribution ----------------------------------------
    regimes = {}
    for symbol in CRYPTO_SYMBOLS:
        regimes[symbol] = regime_distribution(symbol, start, end, cache_dir)
        print(f"[supplement] {symbol}: "
              f"A-eligible {regimes[symbol]['setup_a_eligible_pct']:.1f}% "
              f"of days, B-eligible "
              f"{regimes[symbol]['setup_b_eligible_pct']:.1f}%", flush=True)

    results = {"start": str(start), "end": str(end),
               "sensitivity": sensitivity,
               "fee_rows": fee_rows,
               "regimes": regimes}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=1, default=str))
    print(f"[supplement] written to {out_path}\n")

    print("=== FILL-MODEL SENSITIVITY ===")
    for variant in ("exact", "window"):
        for model in ("conservative", "optimistic"):
            s = sensitivity[variant][model]
            if s["n"] == 0:
                continue
            print(f"{variant:>7}/{model:<13} n={s['n']:>3} "
                  f"win={s['win_rate'] * 100:>5.1f}% "
                  f"meanR_net={s['mean_r_net']:+.3f} "
                  f"meanR_gross={s['mean_r_gross']:+.3f} "
                  f"totalR_net={s['total_r_net']:+.2f}")

    print("\n=== FEE DRAG vs STOP DISTANCE (conservative model) ===")
    rows = [r for r in fee_rows
            if r["model"] == "conservative" and r["variant"] == "window"]
    for r in sorted(rows, key=lambda x: x["stop_distance_pct"]):
        print(f"  {r['symbol']:>9} {r['signal_ts'][:16]} "
              f"stop_dist={r['stop_distance_pct']:>5.2f}% "
              f"gross={r['r_gross']:+.2f} net={r['r_net']:+.2f} "
              f"fees={r['fee_drag_r']:.2f}R  {r['exits']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
