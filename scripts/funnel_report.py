"""Phase 5 funnel report — aggregates scan telemetry, changes nothing.

Usage:
    python scripts/funnel_report.py [--journal-dir PATH] [--limit N]

Reads <strand>/telemetry.jsonl from the journal root (STATE_REPO_DIR /
JOURNAL_DIR / --journal-dir) and prints:
  * pass rate + conditional (funnel) pass rate per condition
  * ranked bottlenecks
  * nearest misses
  * exact vs missed-between-scans reclaim events (sampling limitation)
  * close-only vs candle-range pullback detection
  * risk-gate blocks and execution outcomes for qualified signals
  * qualification summaries by setup / symbol / regime / day / week
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.funnel import (
    condition_pass_rates,
    gate_block_summary,
    group_summary,
    load_records,
    nearest_misses,
    pullback_summary,
    ranked_bottlenecks,
    reclaim_summary,
    CONDITION_ORDER,
)
from src.journal import GitJournal, Journal, journal_from_env


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal-dir", default=None)
    parser.add_argument("--limit", type=int, default=10,
                        help="rows for nearest-miss / bottleneck lists")
    args = parser.parse_args(argv)

    if args.journal_dir:
        root = Path(args.journal_dir)
        journal = GitJournal(root) if (root / ".git").exists() else Journal(root)
    else:
        journal = journal_from_env()
    if journal is None:
        print("funnel_report: no journal configured", file=sys.stderr)
        return 2

    records = load_records(journal)
    print(f"=== FUNNEL REPORT ({len(records)} scan records) ===")
    if not records:
        print("No telemetry collected yet — records accrue one per scan.")
        return 0
    print(f"first scan: {records[0]['scan_ts']}")
    print(f"last scan:  {records[-1]['scan_ts']}")

    rates = condition_pass_rates(records)
    for setup, order in CONDITION_ORDER.items():
        print(f"\n--- Setup {setup}: condition funnel ---")
        print(f"{'condition':44} {'pass':>8} {'| after preceding':>18}")
        for name in order:
            entry = rates.get((setup, name))
            if entry is None:
                continue
            print(f"{name:44} {_pct(entry['rate']):>8} "
                  f"{_pct(entry['conditional_rate']):>12} "
                  f"(n={entry['conditional_n']})")

    print("\n--- Ranked bottlenecks (lowest funnel pass rate) ---")
    for row in ranked_bottlenecks(records)[:args.limit]:
        print(f"  {row['setup']}/{row['condition']}: "
              f"{_pct(row['conditional_rate'])} of {row['conditional_n']}")

    print("\n--- Nearest misses ---")
    for miss in nearest_misses(records, limit=args.limit):
        print(f"  {miss['scan_ts'][:16]} {miss['symbol']} "
              f"{miss['setup']}/{miss['condition']}: short by "
              f"{-miss['margin'] * 100:.2f}% "
              f"(observed {miss['observed']}, threshold {miss['threshold']})")

    reclaims = reclaim_summary(records)
    print("\n--- Reclaim sampling (Setup A, 4h scanner vs 1H events) ---")
    print(f"  exact reclaims observed:        {reclaims['exact_reclaims_observed']}")
    print(f"  reclaims missed between scans:  {reclaims['reclaims_missed_between_scans']}")
    missed_pct = reclaims["missed_pct"]
    print(f"  missed percentage:              "
          f"{'n/a' if missed_pct is None else f'{missed_pct:.1f}%'}")
    for event in reclaims["missed_events"][:args.limit]:
        print(f"    missed: {event['symbol']} reclaim bar {event['reclaim_bar_ts']} "
              f"(scan {event['scan_ts'][:16]})")

    pullbacks = pullback_summary(records)
    print("\n--- Pullback detection (Setup A) ---")
    print(f"  close-rule only:   {pullbacks['close_rule_only']}")
    print(f"  range-touch only:  {pullbacks['range_touch_only']} "
          f"(candle-range interaction would add these)")
    print(f"  both:              {pullbacks['both']}")

    gates = gate_block_summary(records)
    print("\n--- Qualified signals → execution ---")
    print(f"  qualified signals: {gates['qualified_signals']}")
    for reason, count in sorted(gates["gate_blocks"].items(),
                                key=lambda kv: -kv[1]):
        print(f"  blocked ({count}x): {reason}")
    for outcome, count in sorted(gates["execution_outcomes"].items()):
        print(f"  outcome {outcome}: {count}")

    for by in ("setup", "symbol", "regime", "day", "week"):
        print(f"\n--- Qualification by {by} ---")
        groups = group_summary(records, by)
        for key in sorted(groups):
            entry = groups[key]
            print(f"  {key}: {entry['qualified']}/{entry['evaluations']} qualified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
