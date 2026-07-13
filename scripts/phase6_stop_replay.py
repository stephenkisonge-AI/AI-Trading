"""Phase 6, Experiment 1 — stop-limit offset sweep on 1-minute bars.

Usage:
    python scripts/phase6_stop_replay.py
        [--exp2 docs/phase6/experiment2_results.json]
        [--start 2024-01-01] [--end ...] [--symbols ...]
        [--offsets 0.001,0.0025,0.005,0.01,0.02,0.03]
        [--delays 15,30,45] [--max-events-per-symbol 150]
        [--cache-dir .replay_cache]
        [--out docs/phase6/experiment1_results.json]

Event sources:
  1. TRADE events — every broker-stop trigger the Experiment 2
     simulator emitted (both variants, deduped per symbol+bar+level).
     Faithful but few; carries the trade's original risk so slippage
     can be expressed in R.
  2. SYNTHETIC events — first 1H breach of a recent-structure low
     (src.replay.find_level_cross_events) for statistical power on the
     fill mechanics themselves.

For each event the trigger hour is replayed on 1m bars per candidate
offset x watchdog delay (swing-manager cadence bounds). Analysis only.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from scripts.phase6_replay import fetch_bars_range
from src.replay import find_level_cross_events, simulate_stop_limit
from src.universe import CRYPTO_SYMBOLS

_MAX_DELAY_BUFFER_MIN = 15


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def load_trade_events(exp2_path: Path) -> list[dict]:
    """Stop-hit events from the Experiment 2 results, with the trade's
    entry/stop0 attached so slippage can be R-normalized. Deduped on
    (symbol, bar_ts, stop) — the two variants often share events."""
    data = json.loads(exp2_path.read_text())
    seen: set[tuple] = set()
    events: list[dict] = []
    for symbol, sym in data.get("per_symbol", {}).items():
        for variant in ("exact", "window"):
            for trade in sym.get(variant, {}).get("trades", []):
                for hit in trade.get("stop_hits", []):
                    key = (symbol, hit["bar_ts"], round(hit["stop"], 8))
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append({
                        "symbol": symbol,
                        "bar_ts": pd.Timestamp(hit["bar_ts"]),
                        "stop": hit["stop"],
                        "gap_open": hit.get("gap_open", False),
                        "entry": trade["entry"],
                        "stop0": trade["stop0"],
                        "source": "trade",
                    })
    return events


def synthetic_events(symbol: str, start: datetime, end: datetime,
                     cache_dir: Path, cap: int) -> list[dict]:
    h1 = fetch_bars_range(symbol, "1Hour", start, end, cache_dir)
    events = [
        {"symbol": symbol, "bar_ts": e["bar_ts"], "stop": e["stop"],
         "gap_open": False, "entry": None, "stop0": None,
         "source": "synthetic"}
        for e in find_level_cross_events(
            h1, lookback_hours=80, exclude_recent=4,
            clear_hours=48, dedupe_hours=72)
        if e["stop"] > 0
    ]
    if len(events) > cap:
        step = len(events) / cap
        events = [events[int(i * step)] for i in range(cap)]
    return events


def minute_window(event: dict, max_delay_min: int,
                  cache_dir: Path) -> pd.DataFrame:
    hour_start = event["bar_ts"].to_pydatetime()
    w_start = hour_start - timedelta(minutes=5)
    w_end = (hour_start + timedelta(hours=1)
             + timedelta(minutes=max_delay_min + _MAX_DELAY_BUFFER_MIN))
    return fetch_bars_range(event["symbol"], "1Min", w_start, w_end,
                            cache_dir / "m1")


def sweep(events: list[dict], offsets: list[float], delays: list[int],
          cache_dir: Path) -> dict:
    """{offset -> {delay -> aggregate}} plus per-event rows."""
    rows: list[dict] = []
    skipped = 0
    for i, event in enumerate(events):
        try:
            m1 = minute_window(event, max(delays), cache_dir)
        except RuntimeError:
            skipped += 1
            continue
        hour_start = event["bar_ts"].to_pydatetime()
        for offset in offsets:
            for delay in delays:
                sim = simulate_stop_limit(m1, hour_start, event["stop"],
                                          offset, delay)
                if sim is None:
                    continue
                row = {"symbol": event["symbol"],
                       "bar_ts": str(event["bar_ts"]),
                       "source": event["source"],
                       "gap_open": event["gap_open"],
                       "offset": offset, "delay": delay,
                       "filled_via_limit": sim["filled_via_limit"],
                       "slippage_pct": sim["slippage_pct"]}
                if event["entry"] is not None:
                    risk = event["entry"] - event["stop0"]
                    row["slippage_r"] = (sim["slippage_pct"]
                                         * event["stop"] / risk)
                rows.append(row)
        if i % 50 == 0:
            print(f"[stop-replay] {i + 1}/{len(events)} events", flush=True)
    return {"rows": rows, "skipped_events": skipped}


def aggregate(rows: list[dict], offsets: list[float],
              delays: list[int]) -> dict:
    out: dict = {}
    for offset in offsets:
        out[str(offset)] = {}
        for delay in delays:
            sel = [r for r in rows
                   if r["offset"] == offset and r["delay"] == delay]
            if not sel:
                out[str(offset)][str(delay)] = {"n": 0}
                continue
            slips = sorted(r["slippage_pct"] for r in sel)
            unfilled = [r for r in sel if not r["filled_via_limit"]]
            r_slips = [r["slippage_r"] for r in sel if "slippage_r" in r]
            out[str(offset)][str(delay)] = {
                "n": len(sel),
                "limit_fill_rate": 1 - len(unfilled) / len(sel),
                "mean_slippage_bps": 1e4 * sum(slips) / len(slips),
                "median_slippage_bps": 1e4 * _percentile(slips, 0.5),
                "p90_slippage_bps": 1e4 * _percentile(slips, 0.9),
                "max_slippage_bps": 1e4 * slips[-1],
                "mean_slippage_r": (statistics.mean(r_slips)
                                    if r_slips else None),
            }
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exp2", default="docs/phase6/experiment2_results.json")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--symbols", default=",".join(CRYPTO_SYMBOLS))
    parser.add_argument("--offsets",
                        default="0.001,0.0025,0.005,0.01,0.02,0.03")
    parser.add_argument("--delays", default="15,30,45")
    parser.add_argument("--max-events-per-symbol", type=int, default=150)
    parser.add_argument("--cache-dir", default=".replay_cache")
    parser.add_argument("--out", default="docs/phase6/experiment1_results.json")
    args = parser.parse_args(argv)

    load_dotenv()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    offsets = [float(x) for x in args.offsets.split(",")]
    delays = [int(x) for x in args.delays.split(",")]
    cache_dir = Path(args.cache_dir)

    events: list[dict] = []
    exp2_path = Path(args.exp2)
    if exp2_path.exists():
        trade_events = load_trade_events(exp2_path)
        events.extend(trade_events)
        print(f"[stop-replay] {len(trade_events)} trade stop events "
              f"from {exp2_path}")
    else:
        print(f"[stop-replay] {exp2_path} missing — synthetic events only")

    for symbol in symbols:
        sym_events = synthetic_events(symbol, start, end, cache_dir,
                                      args.max_events_per_symbol)
        events.extend(sym_events)
        print(f"[stop-replay] {symbol}: {len(sym_events)} synthetic events")

    print(f"[stop-replay] simulating {len(events)} events x "
          f"{len(offsets)} offsets x {len(delays)} delays...")
    swept = sweep(events, offsets, delays, cache_dir)
    rows = swept["rows"]

    results = {
        "start": str(start), "end": str(end),
        "offsets": offsets, "delays": delays,
        "n_events": len(events),
        "skipped_events_no_1m_trigger": swept["skipped_events"],
        "aggregate_all": aggregate(rows, offsets, delays),
        "aggregate_trade_only": aggregate(
            [r for r in rows if r["source"] == "trade"], offsets, delays),
        "rows": rows,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=1, default=str))
    print(f"[stop-replay] results written to {out_path}\n")

    for delay in delays:
        print(f"=== watchdog delay {delay} min ===")
        print(f"{'offset':>8} {'n':>5} {'fill%':>7} {'mean bps':>9} "
              f"{'p90 bps':>8} {'max bps':>8}")
        for offset in offsets:
            a = results["aggregate_all"][str(offset)][str(delay)]
            if a["n"] == 0:
                print(f"{offset:>8} {'-':>5}")
                continue
            print(f"{offset:>8} {a['n']:>5} "
                  f"{a['limit_fill_rate'] * 100:>6.1f}% "
                  f"{a['mean_slippage_bps']:>9.2f} "
                  f"{a['p90_slippage_bps']:>8.2f} "
                  f"{a['max_slippage_bps']:>8.2f}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
