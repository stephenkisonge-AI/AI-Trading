"""Phase 6, Experiment 2 — Variant B reclaim-window replay.

Usage:
    python scripts/phase6_replay.py [--start 2024-01-01] [--end 2026-07-13]
        [--symbols BTC/USD,ETH/USD,...] [--cache-dir .replay_cache]
        [--setup A|B] [--out docs/phase6/experiment2_results.json]

Fetches daily/4H/1H history (with local CSV caching), replays every
4-hourly scan through the production Setup A evaluator, runs both entry
variants' per-symbol books, and writes a JSON results file plus a
printed summary. Analysis only — no orders, no state-repo writes.

--setup B (Phase 7) replays the production Setup B evaluator instead:
one variant, booked under "exact"; default output moves to
docs/phase6/setup_b_results.json so Experiment 2's results survive.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from src.replay import (
    FRAME_LEN,
    SimTrade,
    replay_symbol,
    summarize_trades,
)
from src.universe import CRYPTO_SYMBOLS

# Warmup so the first scan of the window still gets FRAME_LEN completed
# bars (plus slack for thin-volume missing bars).
_WARMUP = {
    "1Day": timedelta(days=FRAME_LEN + 130),
    "4Hour": timedelta(hours=4 * (FRAME_LEN + 60)),
    "1Hour": timedelta(hours=FRAME_LEN + 96),
}


def fetch_bars_range(symbol: str, timeframe: str, start: datetime,
                     end: datetime, cache_dir: Path) -> pd.DataFrame:
    """Ranged historical fetch with CSV cache. Same columns as
    src.data.get_bars: [open, high, low, close, volume], UTC index."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = (f"{symbol.replace('/', '')}_{timeframe}_"
           f"{start:%Y%m%d}_{end:%Y%m%d}.csv")
    path = cache_dir / key
    if path.exists():
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        return df

    from alpaca.data.historical.crypto import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    tf = {
        "1Day": TimeFrame(amount=1, unit=TimeFrameUnit.Day),
        "4Hour": TimeFrame(amount=4, unit=TimeFrameUnit.Hour),
        "1Hour": TimeFrame(amount=1, unit=TimeFrameUnit.Hour),
        "1Min": TimeFrame(amount=1, unit=TimeFrameUnit.Minute),
    }[timeframe]
    client = CryptoHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    )
    barset = client.get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols=symbol, timeframe=tf, start=start, end=end))
    df = barset.df
    if df is None or df.empty:
        raise RuntimeError(f"No bars for {symbol} {timeframe} "
                           f"{start:%Y-%m-%d}..{end:%Y-%m-%d}")
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df.to_csv(path)
    return df


def _trade_dict(t: SimTrade) -> dict:
    d = asdict(t)
    d["signal_ts"] = str(t.signal_ts)
    d["exit_ts"] = str(t.exit_ts)
    d["tranches"] = [(f, p, str(ts), r) for f, p, ts, r in t.tranches]
    d["r_gross"] = round(t.r_gross(), 4)
    d["r_net"] = round(t.r_net(), 4)
    return d


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--symbols", default=",".join(CRYPTO_SYMBOLS))
    parser.add_argument("--cache-dir", default=".replay_cache")
    parser.add_argument("--setup", choices=("A", "B"), default="A")
    parser.add_argument("--stop-atr-floor", type=float, default=None,
                        help="Setup B only: widen the stop to at least "
                             "this many ATRs below entry")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    if args.out is None:
        args.out = ("docs/phase6/experiment2_results.json" if args.setup == "A"
                    else "docs/phase6/setup_b_results.json")
    variants = ("exact", "window") if args.setup == "A" else ("exact",)

    load_dotenv()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
           if args.end else datetime.now(timezone.utc))
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cache_dir = Path(args.cache_dir)

    results = {"start": str(start), "end": str(end), "symbols": symbols,
               "setup": args.setup, "stop_atr_floor": args.stop_atr_floor,
               "per_symbol": {}, "combined": {}}
    all_trades: dict[str, list[SimTrade]] = {v: [] for v in variants}
    all_stop_hits: list[dict] = []
    signal_counts = {"exact": 0, "window": 0, "window_only": 0, "scans": 0}

    for symbol in symbols:
        print(f"[replay] {symbol}: fetching history...", flush=True)
        daily = fetch_bars_range(symbol, "1Day", start - _WARMUP["1Day"],
                                 end, cache_dir)
        h4 = fetch_bars_range(symbol, "4Hour", start - _WARMUP["4Hour"],
                              end, cache_dir)
        h1 = fetch_bars_range(symbol, "1Hour", start - _WARMUP["1Hour"],
                              end, cache_dir)
        print(f"[replay] {symbol}: {len(daily)}d/{len(h4)}x4H/{len(h1)}x1H "
              f"bars; scanning...", flush=True)

        out = replay_symbol(
            symbol, daily, h4, h1, start, end,
            progress=lambda sym, ts: print(f"[replay]   {sym} @ {ts:%Y-%m-%d}",
                                           flush=True),
            setup=args.setup, stop_atr_floor=args.stop_atr_floor)
        sigs = out["signals"]
        signal_counts["scans"] += len(sigs)
        signal_counts["exact"] += sum(s.exact for s in sigs)
        signal_counts["window"] += sum(s.window for s in sigs)
        signal_counts["window_only"] += sum(
            s.window and not s.exact for s in sigs)

        sym_result = {"scans": len(sigs)}
        for variant in variants:
            trades = out["trades"][variant]
            all_trades[variant].extend(trades)
            sym_result[variant] = {
                "signals": sum(getattr(s, variant) for s in sigs),
                "summary": summarize_trades(trades),
                "trades": [_trade_dict(t) for t in trades],
            }
            for t in trades:
                all_stop_hits.extend(t.stop_hits)
        results["per_symbol"][symbol] = sym_result
        counts_msg = f"[replay] {symbol}: exact={sym_result['exact']['signals']}"
        if "window" in sym_result:
            counts_msg += f" window={sym_result['window']['signals']}"
        print(counts_msg + " signals", flush=True)

    results["signal_counts"] = signal_counts
    for variant in variants:
        results["combined"][variant] = summarize_trades(all_trades[variant])
    results["stop_hits"] = all_stop_hits

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=1, default=str))
    print(f"\n[replay] results written to {out_path}")

    header = ("EXPERIMENT 2 SUMMARY" if args.setup == "A"
              else "SETUP B REPLAY SUMMARY")
    print(f"\n=== {header} "
          f"({start:%Y-%m-%d} .. {end:%Y-%m-%d}) ===")
    print(f"scans evaluated:      {signal_counts['scans']}")
    print(f"exact signals:        {signal_counts['exact']}")
    if args.setup == "A":
        print(f"window signals:       {signal_counts['window']} "
              f"(+{signal_counts['window_only']} only via window)")
    for variant in variants:
        s = results["combined"][variant]
        print(f"\n--- variant {variant.upper()} ---")
        if s["n"] == 0:
            print("  no trades")
            continue
        print(f"  trades:      {s['n']} ({s['n_truncated']} open at data end)")
        print(f"  win rate:    {s['win_rate'] * 100:.1f}%")
        print(f"  mean R net:  {s['mean_r_net']:+.3f} "
              f"(gross {s['mean_r_gross']:+.3f})")
        print(f"  total R net: {s['total_r_net']:+.2f}")
        print(f"  best/worst:  {s['best_r']:+.2f} / {s['worst_r']:+.2f}")
        print(f"  exits:       {s['exit_mix']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
