"""Telegram alert sender. Failures never crash the caller — the watcher
must always continue scanning other symbols even if Telegram is down.
"""
from __future__ import annotations

import os
import sys

import requests

_TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_TIMEOUT_SEC = 10


def send_alert(message: str) -> bool:
    """POST `message` to Telegram as plain text. Returns True on success,
    False on any failure. Errors are written to stderr — they do NOT raise.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "[notifier] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping send",
            file=sys.stderr,
        )
        return False
    try:
        resp = requests.post(
            _TELEGRAM_SEND_URL.format(token=token),
            data={"chat_id": chat_id, "text": message},
            timeout=_TELEGRAM_TIMEOUT_SEC,
        )
        if not resp.ok:
            print(
                f"[notifier] Telegram returned {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:
        print(f"[notifier] Telegram send failed: {exc}", file=sys.stderr)
        return False


def format_setup_alert(result: dict, regime: str, auto_execute: bool = False) -> str:
    """Build the Telegram message for a qualifying setup result.

    When `auto_execute` is True, the footer line tells the user the
    watcher will attempt execution on its own (a follow-up "ENTRY
    PLACED" or "ENTRY BLOCKED" alert will land shortly).
    """
    setup_name = "Pullback" if result["setup"] == "A" else "Breakout Retest"
    header = f"🔔 SETUP QUALIFIED — {result['symbol']} (Setup {result['setup']} — {setup_name})"

    passed_count = sum(1 for c in result["conditions"] if c["passed"])
    total = len(result["conditions"])
    checklist_lines = [f"Regime: {regime} ✓", f"{passed_count}/{total} conditions passed:"]
    for c in result["conditions"]:
        tick = "✓" if c["passed"] else "✗"
        checklist_lines.append(f"{tick} {c['name']}: {c['detail']}")

    entry = result.get("entry")
    stop = result.get("stop")
    atr = result.get("atr")
    levels_lines: list[str] = []
    if entry is not None and stop is not None:
        risk_pct = (entry - stop) / entry * 100
        tp1 = entry + 1.5 * (entry - stop)
        tp2 = entry + 3.0 * (entry - stop)
        levels_lines = [
            "",
            "Suggested levels:",
            f"Entry: ~{entry:.4f}",
            f"Stop:  ~{stop:.4f} ({-risk_pct:.2f}%)",
            f"TP1:   ~{tp1:.4f} (+1.5R)",
            f"TP2:   ~{tp2:.4f} (+3R)",
        ]
        if atr is not None:
            stop_dist_atr = (entry - stop) / atr
            levels_lines.append(f"Stop dist: {stop_dist_atr:.2f}x 4H ATR(14)")

    if auto_execute:
        footer = ["", "→ Auto-execute is ON. Bundle order incoming."]
    else:
        footer = ["", "→ Open Claude Code → ask the Alpaca MCP to confirm and execute."]

    return "\n".join([header, ""] + checklist_lines + levels_lines + footer)


def format_entry_placed(symbol: str, setup: str, exec_result: dict) -> str:
    """Telegram message confirming an auto-execution bundle was placed."""
    filled_qty = exec_result.get("entry_filled_qty")
    filled_avg = exec_result.get("entry_filled_avg_price")
    notional = (filled_qty * filled_avg) if (filled_qty and filled_avg) else None
    complete = exec_result.get("protective_orders_complete")
    icon = "📥" if complete else "⚠️"
    suffix = "" if complete else " — PROTECTIVE ORDERS INCOMPLETE"

    lines = [
        f"{icon} ENTRY PLACED — {symbol} (Setup {setup}){suffix}",
        "",
        f"Filled qty:  {filled_qty}",
        f"Avg price:   {filled_avg}",
        f"Notional:    ${notional:.2f}" if notional is not None else "Notional:    n/a",
        "",
        f"Stop:        {exec_result.get('stop_price')} "
        f"(order_id: {exec_result.get('stop_order_id') or 'FAILED'})",
        f"TP1 (+1.5R): {exec_result.get('tp1_price')} "
        f"(order_id: {exec_result.get('tp1_order_id') or 'FAILED'})",
        f"TP2 (+3R):   {exec_result.get('tp2_price')} "
        f"(order_id: {exec_result.get('tp2_order_id') or 'FAILED'})",
    ]
    if exec_result.get("errors"):
        lines.append("")
        lines.append("Errors:")
        for e in exec_result["errors"]:
            lines.append(f"  - {e}")
        if not complete:
            lines.append("")
            lines.append("⚠️ INTERVENE: position is open but protective coverage is partial.")
    return "\n".join(lines)


def format_entry_blocked(symbol: str, setup: str, reason: str) -> str:
    """Telegram message when a qualified setup was blocked at a safety
    gate. Currently NOT sent as its own alert (per Phase 5a scoping —
    silent skip). Included here so the scan summary can render it.
    """
    return f"🚫 ENTRY BLOCKED — {symbol} (Setup {setup}): {reason}"


def format_scan_summary(
    scan_results: list[dict],
    errors: list[str],
    run_started,
    next_scan_eat: str,
    run_kind: str = "primary",
) -> str:
    """End-of-run pulse message sent on every scan.

    Header switches on outcome so the user can see at a glance whether to
    act: 🟢 green light (a setup qualified), 🔴 stand down (clean scan, no
    entries), ⚠️ errors (something failed mid-scan). A `(FALLBACK)` tag is
    appended when `run_kind == "fallback"` — i.e. the primary cron missed
    and the safety-net cron picked it up later.
    """
    any_qualified = any(
        r["setup_a"]["qualified"] or r["setup_b"]["qualified"]
        for r in scan_results
    )
    if errors:
        header = "⚠️ DAILY SCAN — ERRORS"
    elif any_qualified:
        header = "🟢 DAILY SCAN — GREEN LIGHT"
    else:
        header = "🔴 DAILY SCAN — STAND DOWN"
    if run_kind == "fallback":
        header += " (FALLBACK)"

    ts = run_started.strftime("%Y-%m-%d %H:%M UTC")
    lines = [header, ts, ""]

    for r in scan_results:
        sym = r["symbol"]
        regime = r["regime"]
        qualifying = [s for s in (r["setup_a"], r["setup_b"]) if s["qualified"]]
        if qualifying:
            tags = ", ".join(f"Setup {q['setup']} qualified" for q in qualifying)
            lines.append(f"• {sym} — {regime} · {tags}")
        else:
            lines.append(f"• {sym} — {regime} · no entry")
        # Per-symbol execution outcome (auto-execute mode only)
        for note in r.get("execution_notes", []):
            lines.append(f"   ↳ {note}")

    if errors:
        lines.append("")
        lines.append("Errors:")
        for e in errors:
            lines.append(f"  - {e}")

    lines.append("")
    lines.append(f"Next scan: {next_scan_eat}")
    if any_qualified:
        lines.append("→ See detailed alert(s) above.")
    elif not errors:
        lines.append("Long-only spec → no action while bearish.")

    return "\n".join(lines).rstrip()
