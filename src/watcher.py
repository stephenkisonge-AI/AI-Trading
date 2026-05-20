"""Watcher entry point. Runs every 4 hours on GitHub Actions (at :17 of
00, 04, 08, 12, 16, 20 UTC), evaluates Setup A and Setup B on closed
bars for each symbol, and pings Telegram only when something qualifies.

Never places orders. Trade execution always goes through Claude Code +
Alpaca MCP with manual confirmation.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from src.data import _assert_paper_mode, get_bars, get_positions
from src.indicators import add_indicators
from src.notifier import (
    format_entry_placed,
    format_scan_summary,
    format_setup_alert,
    send_alert,
)
from src.strategy import classify_regime, evaluate_setup_a, evaluate_setup_b
from src.trader import (
    auto_execute_enabled,
    check_safety_gates,
    place_entry_bundle,
)

SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "AVAX/USD"]

# Cron schedule: every 4 hours at :17 UTC (see .github/workflows/watcher.yml).
# Keep these in sync with the cron — they drive the "next scan" line in the
# Telegram summary.
_CRON_UTC_HOURS = (0, 4, 8, 12, 16, 20)
_CRON_UTC_MINUTE = 17
_NAIROBI = timezone(timedelta(hours=3))


def _next_scan_eat(run_started_utc: datetime) -> str:
    """Format the next scheduled scan time as Nairobi-local. Finds the
    next 4-hourly :17 slot after `run_started_utc`; rolls to tomorrow's
    first slot if today's are all in the past.
    """
    today_slots = [
        run_started_utc.replace(hour=h, minute=_CRON_UTC_MINUTE, second=0, microsecond=0)
        for h in _CRON_UTC_HOURS
    ]
    future = [s for s in today_slots if s > run_started_utc]
    if future:
        next_utc = future[0]
    else:
        tomorrow = run_started_utc + timedelta(days=1)
        next_utc = tomorrow.replace(
            hour=_CRON_UTC_HOURS[0],
            minute=_CRON_UTC_MINUTE,
            second=0,
            microsecond=0,
        )
    return next_utc.astimezone(_NAIROBI).strftime("%a %Y-%m-%d %H:%M EAT")


def _alpaca_position_symbol(symbol: str) -> str:
    """Alpaca returns crypto position symbols without the slash."""
    return symbol.replace("/", "")


def _has_open_position(symbol: str, positions) -> bool:
    target = _alpaca_position_symbol(symbol)
    return any(getattr(p, "symbol", None) == target for p in positions)


def _drop_in_progress_candle(df):
    """Drop the most recent bar — at scan time it's still forming.

    The strategy doc is explicit: evaluate on closed candles. The cron
    fires at :17 of 00/04/08/12/16/20 UTC; at every scan time the most
    recent 1H bar is ~17 minutes old, the most recent 4H bar is between
    17 minutes and 4 hours into its window (the :17 minute is always
    inside the current 4H bar, never on its boundary), and the daily
    bar is some hours into its 24-hour window. None are closed.
    """
    if len(df) > 0:
        return df.iloc[:-1].copy()
    return df


def _scan_symbol(symbol: str, positions) -> dict:
    """Pull data and run both setup evaluators for one symbol.

    Returns: {"symbol": ..., "regime": ..., "setup_a": <result>, "setup_b": <result>}
    Raises on any error — caller wraps in try/except to keep scanning others.
    """
    daily_raw = get_bars(symbol, "1Day", limit=250)
    h4_raw = get_bars(symbol, "4Hour", limit=250)
    h1_raw = get_bars(symbol, "1Hour", limit=250)

    daily = _drop_in_progress_candle(daily_raw)
    h4 = add_indicators(_drop_in_progress_candle(h4_raw))
    h1 = add_indicators(_drop_in_progress_candle(h1_raw))

    regime = classify_regime(daily)
    has_position = _has_open_position(symbol, positions)

    setup_a = evaluate_setup_a(daily, h4, h1, symbol, has_position)
    setup_b = evaluate_setup_b(daily, h4, h1, symbol, has_position)

    return {
        "symbol": symbol,
        "regime": regime,
        "has_position": has_position,
        "setup_a": setup_a,
        "setup_b": setup_b,
    }


def main() -> int:
    load_dotenv()
    _assert_paper_mode()

    run_started = datetime.now(timezone.utc)
    print(f"[watcher] started at {run_started.isoformat()}")

    try:
        positions = get_positions()
        print(f"[watcher] open positions: {len(positions)}")
    except Exception as exc:
        print(f"[watcher] FAILED to fetch positions: {exc}", file=sys.stderr)
        send_alert(
            f"⚠️ Watcher errors at {run_started.strftime('%H:%M UTC')} — "
            f"could not fetch positions: {exc}"
        )
        return 0

    auto_exec = auto_execute_enabled()
    print(f"[watcher] auto-execute enabled: {auto_exec}")

    errors: list[str] = []
    scan_results: list[dict] = []
    for symbol in SYMBOLS:
        try:
            result = _scan_symbol(symbol, positions)
            result["execution_notes"] = []
            scan_results.append(result)
            summary = (
                f"[watcher] {symbol}: regime={result['regime']} "
                f"has_position={result['has_position']} "
                f"setup_a_qualified={result['setup_a']['qualified']} "
                f"setup_b_qualified={result['setup_b']['qualified']}"
            )
            print(summary)

            for setup_result in (result["setup_a"], result["setup_b"]):
                if not setup_result["qualified"]:
                    continue

                message = format_setup_alert(
                    setup_result, result["regime"], auto_execute=auto_exec
                )
                sent = send_alert(message)
                print(
                    f"[watcher] sent setup alert for {symbol} "
                    f"Setup {setup_result['setup']}: {sent}"
                )

                if not auto_exec:
                    continue

                # Auto-execution path: safety gates → place bundle → alert
                gate = check_safety_gates(symbol)
                if not gate.allowed:
                    note = f"Setup {setup_result['setup']} blocked: {gate.reason}"
                    print(f"[watcher] {symbol}: {note}")
                    result["execution_notes"].append(note)
                    continue

                entry = setup_result["entry"]
                stop = setup_result["stop"]
                tp1 = entry + 1.5 * (entry - stop)
                tp2 = entry + 3.0 * (entry - stop)
                exec_result = place_entry_bundle(
                    symbol=symbol,
                    entry_price_hint=entry,
                    stop_price=stop,
                    tp1_price=tp1,
                    tp2_price=tp2,
                )

                if exec_result["entry_filled_qty"] is None:
                    note = (
                        f"Setup {setup_result['setup']} execution FAILED before fill: "
                        f"{'; '.join(exec_result['errors']) or 'unknown'}"
                    )
                    print(f"[watcher] {symbol}: {note}", file=sys.stderr)
                    result["execution_notes"].append(note)
                    send_alert(
                        f"⚠️ ENTRY FAILED — {symbol} (Setup {setup_result['setup']})\n\n"
                        f"{'; '.join(exec_result['errors']) or 'no details'}"
                    )
                    continue

                placed_msg = format_entry_placed(symbol, setup_result["setup"], exec_result)
                send_alert(placed_msg)
                note = (
                    f"Setup {setup_result['setup']} EXECUTED: "
                    f"qty={exec_result['entry_filled_qty']} "
                    f"@ {exec_result['entry_filled_avg_price']} "
                    f"(protective_complete={exec_result['protective_orders_complete']})"
                )
                print(f"[watcher] {symbol}: {note}")
                result["execution_notes"].append(note)
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"[watcher] ERROR scanning {symbol}: {exc}\n{tb}", file=sys.stderr)
            errors.append(f"{symbol}: {exc}")

    next_scan_eat = _next_scan_eat(run_started)
    run_kind = os.environ.get("WATCHER_RUN_KIND", "primary")
    summary_msg = format_scan_summary(
        scan_results, errors, run_started, next_scan_eat, run_kind=run_kind
    )
    sent = send_alert(summary_msg)
    print(f"[watcher] sent end-of-run summary (kind={run_kind}): {sent}")

    print(f"[watcher] done at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
