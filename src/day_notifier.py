"""Telegram-alert formatters specific to the day-trade strategy.

Send-side I/O (the HTTP call to Telegram) lives in src/notifier.py.
This module only builds message strings.
"""
from __future__ import annotations

from datetime import datetime


def format_pre_session_summary(
    *,
    now_et: datetime,
    equity: float,
    week_pnl_pct: float,
    daily_regime: str,
    intraday_character: str | None,
    eligible: list[str],
    universe_size: int,
    blocked: list[tuple[str, str]],
    econ_event_today: str | None,
    pmh_pml: dict[str, tuple[float, float] | None] | None = None,
) -> str:
    """Pre-session Telegram summary at 9:25 ET.

    `blocked` is a list of (symbol, reason) tuples for tickers excluded
    from today's eligible set. Reasons typically: 'earnings', 'gap_4pct',
    'stale_calendar'.
    """
    lines: list[str] = []
    lines.append(f"📋 Day-trade pre-session — {now_et.strftime('%a %Y-%m-%d %H:%M ET')}")
    lines.append("")
    lines.append(f"Account: equity ${equity:,.2f} | week P&L {week_pnl_pct:+.2f}%")
    lines.append(f"SPY daily regime: {daily_regime}")
    if intraday_character:
        lines.append(f"Intraday character: {intraday_character}")
    if econ_event_today:
        lines.append(f"⚠️ Econ event today: {econ_event_today} (30-min blackout halo applies)")
    lines.append("")
    lines.append(f"Eligible universe: {len(eligible)}/{universe_size}")
    if eligible:
        lines.append("  " + ", ".join(eligible))
    if blocked:
        lines.append("Blocked:")
        for sym, reason in blocked:
            lines.append(f"  {sym} — {reason}")
    if pmh_pml:
        lines.append("")
        lines.append("Pre-market levels:")
        for sym in eligible:
            level = pmh_pml.get(sym)
            if level is None:
                lines.append(f"  {sym}: PMH/PML unavailable")
            else:
                pmh, pml = level
                lines.append(f"  {sym}: PMH {pmh:.2f} / PML {pml:.2f}")
    return "\n".join(lines)


def format_setup_alert(result: dict, *, daily_regime: str, intraday_character: str) -> str:
    """Per-setup Telegram alert when a candidate qualifies 10/10.

    `result` matches the dict returned by evaluate_setup_a/b in
    src/day_strategy.py.
    """
    setup = result["setup"]
    symbol = result["symbol"]
    entry = result["entry"]
    stop = result["stop"]
    tp1 = result["tp1"]
    tp2 = result["tp2"]
    atr = result.get("atr")
    stop_pct = (entry - stop) / entry * 100 if entry and stop else None

    lines: list[str] = []
    setup_name = "ORB" if setup == "A" else "VWAP Reclaim"
    lines.append(f"🟢 DAY-TRADE QUALIFIED — Setup {setup} ({setup_name}) on {symbol}")
    lines.append("")
    lines.append(f"Regime: SPY daily {daily_regime} | intraday {intraday_character}")
    lines.append("")
    lines.append(f"Entry: ${entry:.2f}")
    if stop is not None and stop_pct is not None:
        lines.append(f"Stop:  ${stop:.2f} ({-stop_pct:.2f}%)")
    if tp1 is not None:
        lines.append(f"TP1:   ${tp1:.2f}  (+1R, sell 50%, then stop → breakeven)")
    if tp2 is not None:
        lines.append(f"TP2:   ${tp2:.2f}  (+2R, sell remaining 50%)")
    if atr:
        lines.append(f"ATR(14, 5-min): {atr:.4f}")
    lines.append("")
    lines.append("Conditions (10/10):")
    for cond in result.get("conditions", []):
        mark = "✓" if cond["passed"] else "✗"
        lines.append(f"  {mark} {cond['name']}")
    lines.append("")
    lines.append("⚠️ ALERTS-ONLY MODE — no orders placed.")
    return "\n".join(lines)


def format_no_qualifying_setups(
    *,
    now_et: datetime,
    candidates_scanned: int,
    skipped_summary: dict[str, int],
) -> str:
    """End-of-scan summary when nothing qualified. Skipped via Telegram
    once per scan tick so absence-of-signal is still visible. (For
    alerts-only mode; in D4 this becomes the per-scan summary.)
    """
    lines: list[str] = []
    lines.append(
        f"⚪ Day-trade scan @ {now_et.strftime('%H:%M ET')} — no qualifying setups"
    )
    lines.append(f"Candidates scanned: {candidates_scanned}")
    if skipped_summary:
        lines.append("Skip reasons:")
        for reason, n in sorted(skipped_summary.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason}: {n}")
    return "\n".join(lines)
