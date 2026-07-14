"""Phase 5 — scan telemetry records and funnel aggregation.

Instrumentation only: nothing here changes entry rules. Every scan
produces one telemetry record (per-symbol, per-setup, per-condition
structured results plus sampling observers) which is appended to
`<strand>/telemetry.jsonl` in the state repository. The funnel report
(scripts/funnel_report.py) aggregates those records to answer the
Phase 7 evidence questions: which condition is the bottleneck, what
the 4-hour scanner misses between scans, and what the risk gates
block.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from src.journal import Journal, scrub_secrets

TELEMETRY_FILE = "telemetry.jsonl"

# Canonical condition order per setup — conditional pass rates and
# bottleneck ranking depend on evaluation order.
CONDITION_ORDER = {
    "A": [
        "daily_regime_bullish_or_improving",
        "h4_price_above_ema200",
        "pullback_to_ema_and_higher_low_intact",
        "h4_rsi_in_pullback_zone",
        "h1_green_close_reclaims_ema20",
        "h1_volume_above_threshold",
        "stop_below_swing_low_within_atr_cap",
        "stop_distance_above_fee_floor",
        "no_existing_position",
    ],
    "B": [
        "daily_regime_bullish",
        "breakout_above_20period_high_in_last_10",
        "breakout_candle_closed_above_level",
        "breakout_volume_above_threshold",
        "h4_rsi_in_momentum_zone",
        "h1_retest_of_level_held",
        "stop_below_retested_level_within_atr_cap",
        "stop_distance_above_fee_floor",
        "no_existing_position",
    ],
}


def build_scan_record(scan_results: list[dict], scan_ts: Optional[datetime] = None,
                      run_kind: str = "primary") -> dict:
    """One structured telemetry record for a whole scan pass."""
    scan_ts = scan_ts or datetime.now(timezone.utc)
    record = {
        "scan_ts": scan_ts.isoformat(),
        "run_kind": run_kind,
        "evaluations": [],
    }
    for result in scan_results:
        for key in ("setup_a", "setup_b"):
            setup_result = result.get(key)
            if not setup_result:
                continue
            conditions = setup_result.get("conditions", [])
            failed = [c["name"] for c in conditions if not c["passed"]]
            record["evaluations"].append({
                "symbol": setup_result.get("symbol"),
                "setup": setup_result.get("setup"),
                "regime": result.get("regime"),
                "qualified": bool(setup_result.get("qualified")),
                "signal_bar_ts": str(setup_result.get("signal_bar_ts")),
                "first_failed": failed[0] if failed else None,
                "all_failed": failed,
                "conditions": conditions,
                "telemetry": setup_result.get("telemetry") or {},
                "execution_notes": result.get("execution_notes") or [],
            })
    return record


def persist_scan_record(journal: Journal, record: dict) -> bool:
    """Append the record to <strand>/telemetry.jsonl, durably (for
    GitJournal that means committed and pushed). Telemetry loss never
    blocks trading — callers just log the False."""
    path = journal.events_path.parent / TELEMETRY_FILE
    try:
        line = json.dumps(scrub_secrets(record), separators=(",", ":"),
                          default=str)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        print(f"[funnel] telemetry write failed: {exc}", file=sys.stderr)
        return False
    commit = getattr(journal, "_commit_and_push", None)
    if commit is not None:
        return bool(commit("TELEMETRY", None))
    return True


def load_records(journal: Journal) -> list[dict]:
    path = journal.events_path.parent / TELEMETRY_FILE
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    out.sort(key=lambda r: r.get("scan_ts", ""))
    return out


# =========================================================================
# Aggregation
# =========================================================================

def _iter_evaluations(records: list[dict]):
    for record in records:
        for evaluation in record.get("evaluations", []):
            yield record, evaluation


def condition_pass_rates(records: list[dict]) -> dict:
    """{(setup, condition): {"n": int, "passed": int, "rate": float,
    "conditional_n": int, "conditional_passed": int,
    "conditional_rate": float|None}}

    `conditional_*` counts only evaluations where every PRECEDING
    condition (canonical order) passed — the funnel view.
    """
    stats: dict = defaultdict(lambda: {"n": 0, "passed": 0,
                                       "conditional_n": 0,
                                       "conditional_passed": 0})
    for _record, evaluation in _iter_evaluations(records):
        setup = evaluation.get("setup")
        order = CONDITION_ORDER.get(setup, [])
        results = {c["name"]: c["passed"]
                   for c in evaluation.get("conditions", [])}
        preceding_ok = True
        for name in order:
            if name not in results:
                continue
            entry = stats[(setup, name)]
            entry["n"] += 1
            if results[name]:
                entry["passed"] += 1
            if preceding_ok:
                entry["conditional_n"] += 1
                if results[name]:
                    entry["conditional_passed"] += 1
            preceding_ok = preceding_ok and results[name]
    out = {}
    for key, entry in stats.items():
        out[key] = {
            **entry,
            "rate": entry["passed"] / entry["n"] if entry["n"] else None,
            "conditional_rate": (entry["conditional_passed"] / entry["conditional_n"]
                                 if entry["conditional_n"] else None),
        }
    return out


def ranked_bottlenecks(records: list[dict]) -> list[dict]:
    """Conditions ranked by how many otherwise-alive funnels they kill:
    lowest conditional pass rate first (min 1 conditional evaluation)."""
    rates = condition_pass_rates(records)
    ranked = [
        {"setup": setup, "condition": name, **entry}
        for (setup, name), entry in rates.items()
        if entry["conditional_n"] > 0 and entry["conditional_rate"] is not None
    ]
    ranked.sort(key=lambda e: (e["conditional_rate"], -e["conditional_n"]))
    return ranked


def nearest_misses(records: list[dict], limit: int = 10) -> list[dict]:
    """Failed conditions with the smallest normalized shortfall
    (margin closest to zero from below)."""
    misses = []
    for record, evaluation in _iter_evaluations(records):
        for condition in evaluation.get("conditions", []):
            margin = condition.get("margin")
            if condition["passed"] or margin is None or margin >= 0:
                continue
            misses.append({
                "scan_ts": record.get("scan_ts"),
                "symbol": evaluation.get("symbol"),
                "setup": evaluation.get("setup"),
                "condition": condition["name"],
                "margin": margin,
                "observed": condition.get("observed"),
                "threshold": condition.get("threshold"),
            })
    misses.sort(key=lambda m: -m["margin"])  # closest to zero first
    return misses[:limit]


def reclaim_summary(records: list[dict]) -> dict:
    """Setup A sampling limitation: exact-bar reclaims the 4-hourly
    scanner saw vs reclaim events that happened in the 4 intervening
    1H bars (Variant B would have seen them; the current scanner did
    not)."""
    exact = 0
    window_only = 0
    total = 0
    missed_events: list[dict] = []
    for record, evaluation in _iter_evaluations(records):
        if evaluation.get("setup") != "A":
            continue
        telemetry = evaluation.get("telemetry") or {}
        total += 1
        if telemetry.get("reclaim_exact"):
            exact += 1
        elif telemetry.get("reclaim_window_hit"):
            window_only += 1
            missed_events.append({
                "scan_ts": record.get("scan_ts"),
                "symbol": evaluation.get("symbol"),
                "reclaim_bar_ts": telemetry["reclaim_window_hit"],
            })
    return {
        "evaluations": total,
        "exact_reclaims_observed": exact,
        "reclaims_missed_between_scans": window_only,
        "missed_pct": (window_only / (exact + window_only) * 100
                       if (exact + window_only) else None),
        "missed_events": missed_events,
    }


def pullback_summary(records: list[dict]) -> dict:
    """Close-only pullback detection vs candle-range EMA interaction."""
    close_only = 0
    range_only = 0
    both = 0
    for _record, evaluation in _iter_evaluations(records):
        if evaluation.get("setup") != "A":
            continue
        telemetry = evaluation.get("telemetry") or {}
        close_hit = telemetry.get("pullback_close_rule")
        range_hit = telemetry.get("pullback_range_touch")
        if close_hit and range_hit:
            both += 1
        elif close_hit:
            close_only += 1
        elif range_hit:
            range_only += 1
    return {
        "close_rule_only": close_only,
        "range_touch_only": range_only,
        "both": both,
        "range_would_add": range_only,
    }


def gate_block_summary(records: list[dict]) -> dict:
    """Qualified signals vs execution outcomes, from execution_notes
    (which carry gate blocks and entry statuses)."""
    qualified = 0
    blocked: dict[str, int] = defaultdict(int)
    outcomes: dict[str, int] = defaultdict(int)
    for _record, evaluation in _iter_evaluations(records):
        if not evaluation.get("qualified"):
            continue
        qualified += 1
        for note in evaluation.get("execution_notes", []):
            lowered = note.lower()
            if "blocked" in lowered:
                blocked[note.split("blocked:", 1)[-1].strip()[:80]] += 1
            elif "protected" in lowered:
                outcomes["protected"] += 1
            elif "unwound" in lowered:
                outcomes["unwound"] += 1
            elif "aborted" in lowered:
                outcomes["aborted"] += 1
            elif "recovery" in lowered:
                outcomes["recovery_required"] += 1
            elif "skipped" in lowered:
                outcomes["skipped"] += 1
    return {"qualified_signals": qualified,
            "gate_blocks": dict(blocked),
            "execution_outcomes": dict(outcomes)}


def group_summary(records: list[dict], by: str) -> dict:
    """Qualification counts grouped by 'symbol' | 'setup' | 'regime' |
    'scan' | 'day' | 'week'."""
    out: dict = defaultdict(lambda: {"evaluations": 0, "qualified": 0})
    for record, evaluation in _iter_evaluations(records):
        scan_ts = record.get("scan_ts", "")
        if by == "scan":
            key = scan_ts
        elif by == "day":
            key = scan_ts[:10]
        elif by == "week":
            try:
                dt = datetime.fromisoformat(scan_ts)
                key = f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
            except ValueError:
                key = "unknown"
        else:
            key = evaluation.get(by) or "unknown"
        out[key]["evaluations"] += 1
        if evaluation.get("qualified"):
            out[key]["qualified"] += 1
    return dict(out)


def funnel_line(scan_results: list[dict]) -> str:
    """One concise funnel line for the Telegram scan summary."""
    parts = []
    for key, label in (("setup_a", "A"), ("setup_b", "B")):
        best_passed = -1
        best_symbol = ""
        total = 0
        for result in scan_results:
            setup_result = result.get(key)
            if not setup_result:
                continue
            conditions = setup_result.get("conditions", [])
            total = max(total, len(conditions))
            passed = sum(1 for c in conditions if c["passed"])
            if passed > best_passed:
                best_passed = passed
                best_symbol = setup_result.get("symbol", "?")
        if best_passed >= 0:
            parts.append(f"{label} best {best_passed}/{total} ({best_symbol})")
    missed = sum(
        1 for result in scan_results
        if ((result.get("setup_a") or {}).get("telemetry") or {}).get("reclaim_window_hit")
        and not ((result.get("setup_a") or {}).get("telemetry") or {}).get("reclaim_exact")
    )
    if missed:
        parts.append(f"{missed} reclaim(s) missed between scans")
    return "Funnel: " + "; ".join(parts) if parts else "Funnel: no evaluations"
