"""One-off diagnostic: aggregate day-watcher skip reasons across recent runs.

Lists day-watcher runs from a date range, pulls each run's log, extracts
the '[day-watcher] scan complete: scanned=N qualified=M skipped={...}'
line, parses the skipped dict, and aggregates totals.

Usage:
    python scripts/diag_skip_reasons.py 2026-06-02 2026-06-03

Output: ranked table of skip reasons with counts.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed


SCAN_RE = re.compile(r"\[day-watcher\] scan complete: scanned=(\d+) qualified=(\d+) skipped=(\{.*\})")


def list_runs(date: str) -> list[dict]:
    """Return repository_dispatch runs for the given UTC date."""
    out = subprocess.check_output([
        "gh", "run", "list", "--workflow=day-watcher.yml",
        f"--created={date}",
        "-L", "200",
        "--json", "databaseId,createdAt,event,conclusion",
    ], text=True)
    runs = json.loads(out)
    return [
        r for r in runs
        if r["event"] == "repository_dispatch" and r["conclusion"] == "success"
    ]


def fetch_log(run_id: int) -> str | None:
    try:
        return subprocess.check_output(
            ["gh", "run", "view", str(run_id), "--log"],
            text=True, stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except Exception as exc:
        print(f"  fetch failed {run_id}: {exc}", file=sys.stderr)
        return None


def parse_skipped(log: str) -> dict | None:
    for line in log.splitlines():
        m = SCAN_RE.search(line)
        if m:
            try:
                return ast.literal_eval(m.group(3))
            except Exception:
                return None
    return None


def main(dates: list[str]) -> int:
    runs = []
    for d in dates:
        rs = list_runs(d)
        runs.extend(rs)
        print(f"[diag] {d}: {len(rs)} successful repository_dispatch runs")

    print(f"[diag] fetching {len(runs)} logs in parallel...")

    skipped_totals = Counter()
    candidate_rejections = Counter()  # per-condition occurrence weighted by symbol count
    intraday_scans_parsed = 0

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch_log, r["databaseId"]): r for r in runs}
        for fut in as_completed(futures):
            log = fut.result()
            if log is None:
                continue
            skipped = parse_skipped(log)
            if skipped is None:
                continue
            intraday_scans_parsed += 1
            for reason, count in skipped.items():
                skipped_totals[reason] += count
                # Decompose the "A:cond1,cond2" / "B:cond1,cond2" form
                if reason in ("dead_session",):
                    candidate_rejections[reason] += count
                    continue
                if ":" in reason:
                    _setup, conds = reason.split(":", 1)
                    for c in conds.split(","):
                        candidate_rejections[c.strip()] += count

    print(f"\n[diag] parsed {intraday_scans_parsed} intraday scans "
          f"(out of {len(runs)} runs; non-intraday ticks have no skip data)")

    total_candidates_skipped = sum(skipped_totals.values())
    print(f"\n=== Aggregate skip reasons (top 15 of {len(skipped_totals)}) ===")
    print(f"Total candidate-rejections across {intraday_scans_parsed} scans: {total_candidates_skipped}")
    for reason, count in skipped_totals.most_common(15):
        pct = count / total_candidates_skipped * 100 if total_candidates_skipped else 0
        print(f"  {count:5d}  ({pct:5.1f}%)  {reason}")

    print(f"\n=== Decomposed by individual condition (top 20 of {len(candidate_rejections)}) ===")
    total_decomposed = sum(candidate_rejections.values())
    for cond, count in candidate_rejections.most_common(20):
        pct = count / total_decomposed * 100 if total_decomposed else 0
        print(f"  {count:5d}  ({pct:5.1f}%)  {cond}")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: diag_skip_reasons.py YYYY-MM-DD [YYYY-MM-DD ...]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
