"""Tests for src/funnel.py — Phase 5 telemetry and funnel aggregation.
Instrumentation only: also pins that evaluators emit the structured
fields and observers without changing qualification behavior."""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.funnel import (
    build_scan_record,
    condition_pass_rates,
    funnel_line,
    gate_block_summary,
    group_summary,
    load_records,
    nearest_misses,
    persist_scan_record,
    pullback_summary,
    ranked_bottlenecks,
    reclaim_summary,
)
from src.journal import Journal


def _condition(name, passed, margin=None, observed=None, threshold=None):
    out = {"name": name, "passed": passed, "detail": "", "kind": "state"}
    if margin is not None:
        out["margin"] = margin
    if observed is not None:
        out["observed"] = observed
    if threshold is not None:
        out["threshold"] = threshold
    return out


def _scan_result(symbol="BTC/USD", regime="BULLISH", a_conditions=None,
                 qualified=False, telemetry=None, notes=None):
    return {
        "symbol": symbol,
        "regime": regime,
        "execution_notes": notes or [],
        "setup_a": {
            "setup": "A", "symbol": symbol, "qualified": qualified,
            "conditions": a_conditions or [],
            "signal_bar_ts": "2026-07-11T08:00:00+00:00",
            "telemetry": telemetry or {},
        },
        "setup_b": None,
    }


A_ORDER = [
    "daily_regime_bullish_or_improving",
    "h4_price_above_ema200",
    "pullback_to_ema_and_higher_low_intact",
    "h4_rsi_in_pullback_zone",
    "h1_green_close_reclaims_ema20",
    "h1_volume_above_threshold",
    "stop_below_swing_low_within_atr_cap",
    "no_existing_position",
]


def _a_conditions(passes: list[bool], margins=None):
    margins = margins or [None] * len(passes)
    return [_condition(name, p, margin=m)
            for name, p, m in zip(A_ORDER, passes, margins)]


# ---------------------------------------------------------------------------
# record building + persistence
# ---------------------------------------------------------------------------

def test_build_scan_record_shape():
    results = [_scan_result(a_conditions=_a_conditions([True] * 8),
                            qualified=True)]
    record = build_scan_record(results,
                               scan_ts=datetime(2026, 7, 11, 8, 17,
                                                tzinfo=timezone.utc),
                               run_kind="primary")
    assert record["run_kind"] == "primary"
    assert len(record["evaluations"]) == 1
    evaluation = record["evaluations"][0]
    assert evaluation["qualified"] is True
    assert evaluation["first_failed"] is None
    assert evaluation["all_failed"] == []
    assert len(evaluation["conditions"]) == 8


def test_first_failed_and_all_failed():
    passes = [True, True, False, True, False, True, True, True]
    record = build_scan_record([_scan_result(a_conditions=_a_conditions(passes))])
    evaluation = record["evaluations"][0]
    assert evaluation["first_failed"] == A_ORDER[2]
    assert evaluation["all_failed"] == [A_ORDER[2], A_ORDER[4]]


def test_persist_and_load_roundtrip(tmp_path):
    journal = Journal(tmp_path / "j")
    record = build_scan_record([_scan_result(a_conditions=_a_conditions([True] * 8))])
    assert persist_scan_record(journal, record) is True
    loaded = load_records(journal)
    assert len(loaded) == 1
    assert loaded[0]["evaluations"][0]["symbol"] == "BTC/USD"


# ---------------------------------------------------------------------------
# aggregation math
# ---------------------------------------------------------------------------

def _records(*pass_lists):
    return [build_scan_record([_scan_result(a_conditions=_a_conditions(p))])
            for p in pass_lists]


def test_condition_pass_rates_and_funnel():
    # Scan 1: everything passes. Scan 2: condition 3 fails (so later
    # conditions never count toward the conditional funnel).
    records = _records([True] * 8,
                       [True, True, False, True, True, True, True, True])
    rates = condition_pass_rates(records)
    c3 = rates[("A", A_ORDER[2])]
    assert c3["n"] == 2 and c3["passed"] == 1
    assert c3["rate"] == pytest.approx(0.5)
    assert c3["conditional_rate"] == pytest.approx(0.5)
    c4 = rates[("A", A_ORDER[3])]
    # Raw: passed both scans. Conditional: only scan 1 reaches it.
    assert c4["rate"] == pytest.approx(1.0)
    assert c4["conditional_n"] == 1
    assert c4["conditional_rate"] == pytest.approx(1.0)


def test_ranked_bottlenecks_orders_by_conditional_rate():
    records = _records(
        [True, True, False, True, True, True, True, True],
        [True, True, False, True, True, True, True, True],
        [True, True, True, False, True, True, True, True],
    )
    ranked = ranked_bottlenecks(records)
    # c4 reaches the funnel only once (scan 3) and fails it → 0/1,
    # ranking below c3's 1/3.
    assert ranked[0]["condition"] == A_ORDER[3]
    assert ranked[0]["conditional_rate"] == pytest.approx(0.0)
    assert ranked[1]["condition"] == A_ORDER[2]
    assert ranked[1]["conditional_rate"] == pytest.approx(1 / 3)


def test_nearest_misses_sorted_by_smallest_shortfall():
    margins_a = [None, None, -0.02, None, None, None, None, None]
    margins_b = [None, None, -0.30, None, None, None, None, None]
    records = _records(
        [True, True, False, True, True, True, True, True],
        [True, True, False, True, True, True, True, True],
    )
    records[0]["evaluations"][0]["conditions"] = _a_conditions(
        [True, True, False, True, True, True, True, True], margins_a)
    records[1]["evaluations"][0]["conditions"] = _a_conditions(
        [True, True, False, True, True, True, True, True], margins_b)
    misses = nearest_misses(records)
    assert misses[0]["margin"] == pytest.approx(-0.02)  # closest first
    assert misses[1]["margin"] == pytest.approx(-0.30)
    # Passing conditions and margin-less failures are never "misses".
    assert all(m["margin"] < 0 for m in misses)


def test_reclaim_summary_counts_missed_events():
    seen = _scan_result(a_conditions=_a_conditions([True] * 8),
                        telemetry={"reclaim_exact": True,
                                   "reclaim_window_hit": None})
    missed = _scan_result(symbol="ETH/USD",
                          a_conditions=_a_conditions([True] * 4 + [False] + [True] * 3),
                          telemetry={"reclaim_exact": False,
                                     "reclaim_window_hit": "2026-07-11T06:00:00+00:00"})
    nothing = _scan_result(symbol="SOL/USD",
                           a_conditions=_a_conditions([True] * 4 + [False] + [True] * 3),
                           telemetry={"reclaim_exact": False,
                                      "reclaim_window_hit": None})
    records = [build_scan_record([seen, missed, nothing])]
    summary = reclaim_summary(records)
    assert summary["exact_reclaims_observed"] == 1
    assert summary["reclaims_missed_between_scans"] == 1
    assert summary["missed_pct"] == pytest.approx(50.0)
    assert summary["missed_events"][0]["symbol"] == "ETH/USD"


def test_pullback_summary():
    both = _scan_result(telemetry={"pullback_close_rule": True,
                                   "pullback_range_touch": True})
    range_only = _scan_result(symbol="ETH/USD",
                              telemetry={"pullback_close_rule": False,
                                         "pullback_range_touch": True})
    records = [build_scan_record([both, range_only])]
    summary = pullback_summary(records)
    assert summary["both"] == 1
    assert summary["range_touch_only"] == 1
    assert summary["range_would_add"] == 1


def test_gate_block_summary_reads_execution_notes():
    blocked = _scan_result(qualified=True,
                           a_conditions=_a_conditions([True] * 8),
                           notes=["Setup A blocked: [spread] spread 0.9% exceeds cap"])
    executed = _scan_result(symbol="ETH/USD", qualified=True,
                            a_conditions=_a_conditions([True] * 8),
                            notes=["Setup A PROTECTED:  qty=0.5 @ 3000"])
    records = [build_scan_record([blocked, executed])]
    summary = gate_block_summary(records)
    assert summary["qualified_signals"] == 2
    assert sum(summary["gate_blocks"].values()) == 1
    assert summary["execution_outcomes"].get("protected") == 1


def test_group_summaries():
    records = [build_scan_record(
        [_scan_result(qualified=True, a_conditions=_a_conditions([True] * 8)),
         _scan_result(symbol="ETH/USD", regime="BEARISH",
                      a_conditions=_a_conditions([False] * 8))],
        scan_ts=datetime(2026, 7, 11, 8, 17, tzinfo=timezone.utc))]
    by_symbol = group_summary(records, "symbol")
    assert by_symbol["BTC/USD"]["qualified"] == 1
    assert by_symbol["ETH/USD"]["qualified"] == 0
    by_regime = group_summary(records, "regime")
    assert by_regime["BEARISH"]["evaluations"] == 1
    by_day = group_summary(records, "day")
    assert by_day["2026-07-11"]["evaluations"] == 2
    by_week = group_summary(records, "week")
    assert by_week["2026-W28"]["evaluations"] == 2


def test_funnel_line_reports_best_and_missed():
    results = [
        _scan_result(a_conditions=_a_conditions([True] * 6 + [False] * 2)),
        _scan_result(symbol="ETH/USD",
                     a_conditions=_a_conditions([True] * 3 + [False] * 5),
                     telemetry={"reclaim_exact": False,
                                "reclaim_window_hit": "2026-07-11T06:00Z"}),
    ]
    line = funnel_line(results)
    assert "A best 6/8 (BTC/USD)" in line
    assert "1 reclaim(s) missed between scans" in line


# ---------------------------------------------------------------------------
# evaluator instrumentation (structured fields + observers, no rule change)
# ---------------------------------------------------------------------------

def _mk_df(n=260, close=100.0, freq="4h"):
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    rng = np.random.default_rng(7)
    closes = close + np.cumsum(rng.normal(0, 0.2, n))
    df = pd.DataFrame({
        "open": closes - 0.1, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    }, index=idx)
    from src.indicators import add_indicators
    return add_indicators(df)


def test_evaluators_emit_structured_telemetry():
    from src.strategy import evaluate_setup_a, evaluate_setup_b
    daily = _mk_df(freq="1D")
    h4 = _mk_df(freq="4h")
    h1 = _mk_df(freq="1h")
    result_a = evaluate_setup_a(daily, h4, h1, "BTC/USD", has_position=False)
    result_b = evaluate_setup_b(daily, h4, h1, "BTC/USD", has_position=False)

    for result in (result_a, result_b):
        assert "telemetry" in result
        for condition in result["conditions"]:
            assert "kind" in condition
    # Numeric conditions carry observed/threshold/margin when computable.
    by_name = {c["name"]: c for c in result_a["conditions"]}
    ema_cond = by_name["h4_price_above_ema200"]
    assert "observed" in ema_cond and "threshold" in ema_cond
    assert "margin" in ema_cond
    # The reclaim condition is tagged as an event on the 1H timeframe.
    reclaim = by_name["h1_green_close_reclaims_ema20"]
    assert reclaim["kind"] == "event"
    assert reclaim["timeframe"] == "1Hour"
    # Observers present with the documented keys.
    assert set(result_a["telemetry"]) >= {"reclaim_window_hit",
                                          "reclaim_exact",
                                          "pullback_close_rule",
                                          "pullback_range_touch"}
    assert "no_chase_violation" in result_b["telemetry"]


def test_reclaim_window_observer_sees_missed_reclaim():
    """Construct 1H data where a strict reclaim happened 3 bars ago and
    later closes stayed above their EMA20 — the exact-bar rule misses
    it, the window observer must record it."""
    from src.strategy import evaluate_setup_a
    n = 60
    idx = pd.date_range("2026-07-01", periods=n, freq="1h", tz="UTC")
    closes = np.full(n, 100.0)
    opens = np.full(n, 100.0)
    # Depress prices below EMA for a stretch, then a green reclaim bar
    # at n-4, then closes hold above.
    closes[: n - 5] = 95.0
    opens[: n - 5] = 95.2
    closes[n - 5] = 95.0   # prior bar: close <= its EMA
    opens[n - 5] = 95.1
    closes[n - 4] = 101.0  # reclaim bar: green close above EMA
    opens[n - 4] = 96.0
    closes[n - 3:] = 101.5  # holds above; final bar NOT a fresh reclaim
    opens[n - 3:] = 101.2
    df = pd.DataFrame({
        "open": opens, "high": np.maximum(opens, closes) + 0.2,
        "low": np.minimum(opens, closes) - 0.2, "close": closes,
        "volume": np.full(n, 1000.0),
    }, index=idx)
    from src.indicators import add_indicators
    h1 = add_indicators(df)
    daily = _mk_df(freq="1D")
    h4 = _mk_df(freq="4h")
    result = evaluate_setup_a(daily, h4, h1, "BTC/USD", has_position=False)
    telemetry = result["telemetry"]
    assert telemetry["reclaim_exact"] is False
    assert telemetry["reclaim_window_hit"] == str(idx[n - 4])
