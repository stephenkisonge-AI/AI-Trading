"""Tests for src/notifier.py — focused on format_setup_alert.
send_alert is exercised manually in Phase 5.1; we don't hit Telegram in CI.
"""
from src.notifier import format_setup_alert


def _qualified_result(setup: str) -> dict:
    return {
        "setup": setup,
        "symbol": "ETH/USD",
        "qualified": True,
        "entry": 3245.00,
        "stop": 3145.00,
        "atr": 50.0,
        "conditions": [
            {"name": "daily_regime_bullish_or_improving", "passed": True, "detail": "regime=BULLISH"},
            {"name": "h4_price_above_ema200", "passed": True, "detail": "close=3245 ema200=3120"},
            {"name": "pullback_to_ema_and_higher_low_intact", "passed": True, "detail": "dist_ema20=0.4%"},
            {"name": "h4_rsi_in_pullback_zone", "passed": True, "detail": "rsi14=41.20"},
            {"name": "h1_green_close_reclaims_ema20", "passed": True, "detail": "close>ema20 and green"},
            {"name": "h1_volume_above_threshold", "passed": True, "detail": "1.05x"},
            {"name": "stop_below_swing_low_within_atr_cap", "passed": True, "detail": "1.2x ATR"},
            {"name": "no_existing_position", "passed": True, "detail": "has_position=False"},
        ],
    }


def test_format_alert_includes_setup_label_and_symbol():
    msg = format_setup_alert(_qualified_result("A"), regime="BULLISH")
    assert "ETH/USD" in msg
    assert "Setup A" in msg
    assert "Pullback" in msg


def test_format_alert_setup_b_label_is_breakout_retest():
    msg = format_setup_alert(_qualified_result("B"), regime="BULLISH")
    assert "Setup B" in msg
    assert "Breakout Retest" in msg


def test_format_alert_includes_regime_and_condition_count():
    msg = format_setup_alert(_qualified_result("A"), regime="BULLISH")
    assert "Regime: BULLISH" in msg
    assert "8/8 conditions passed" in msg


def test_format_alert_includes_entry_stop_and_tp_levels():
    msg = format_setup_alert(_qualified_result("A"), regime="BULLISH")
    assert "Entry: ~3245" in msg
    assert "Stop:" in msg
    # TP1 at entry + 1.5R = 3245 + 1.5*(3245-3145) = 3245 + 150 = 3395
    assert "3395" in msg
    # TP2 at entry + 3R = 3245 + 300 = 3545
    assert "3545" in msg


def test_format_alert_includes_atr_distance_when_atr_provided():
    msg = format_setup_alert(_qualified_result("A"), regime="BULLISH")
    # stop_dist = 100, atr = 50 → 2.00x
    assert "2.00x 4H ATR" in msg


def test_format_alert_includes_claude_code_instruction():
    msg = format_setup_alert(_qualified_result("A"), regime="BULLISH")
    assert "Claude Code" in msg
    assert "Alpaca MCP" in msg


def test_format_alert_lists_all_eight_conditions():
    msg = format_setup_alert(_qualified_result("A"), regime="BULLISH")
    lines = msg.splitlines()
    tick_lines = [ln for ln in lines if ln.startswith("✓") or ln.startswith("✗")]
    assert len(tick_lines) == 8
