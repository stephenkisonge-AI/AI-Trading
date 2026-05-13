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


def format_setup_alert(result: dict, regime: str) -> str:
    """Build the Telegram message for a qualifying setup result.

    The format mirrors the example in build-watcher-system.md §3.4: setup
    label, regime tag, 8/8 condition checklist, suggested entry/stop/TPs,
    instruction to confirm via Claude Code + MCP.
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

    footer = [
        "",
        "→ Open Claude Code → ask the Alpaca MCP to confirm and execute.",
    ]

    return "\n".join([header, ""] + checklist_lines + levels_lines + footer)
