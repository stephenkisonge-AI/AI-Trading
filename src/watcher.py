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

from src.data import _assert_paper_mode, get_bars, get_client, get_positions
from src.indicators import add_indicators
from src.notifier import (
    format_protected_entry,
    format_scan_summary,
    format_setup_alert,
    send_alert,
)
from src.strategy import classify_regime, evaluate_setup_a, evaluate_setup_b
from src.trader import auto_execute_enabled, summarize_lifecycle
from src.entry_gates import evaluate_entry_gates
from src.execution import load_exec_config
from src.swing_exits import manage_swing_trades, open_protected_trade
from src.swing_runtime import (
    build_synced_journal,
    quote_for,
    regime_for,
    runner_ctx_for,
)

# Universe — single source of truth in src/universe.py (shared with the
# trader so both strands agree on which positions are "ours").
from src.universe import CRYPTO_SYMBOLS as SYMBOLS

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


def _drop_in_progress_candle(df, period: timedelta,
                             now_utc: datetime | None = None):
    """Keep only CLOSED bars — a bar is closed once start + period <= now.

    The strategy doc is explicit: evaluate on closed candles. The cron
    fires at :17 of 00/04/08/12/16/20 UTC, so the most recent bar of
    each timeframe is normally mid-window and gets cut. But the cut is
    by timestamp, not blindly `iloc[:-1]` (the pre-hygiene behavior):
    on a thin symbol with zero trades so far in the current window
    Alpaca returns no partial bar at all, and dropping the last bar
    then silently evaluated one completed bar behind (Phase 6 finding).
    Crypto bars are stamped with their bucket START on clean UTC
    boundaries (daily at 00:00 UTC), same convention as the day-trade
    strand's _drop_in_progress_bars.
    """
    if len(df) == 0:
        return df
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff = now_utc - period
    return df[df.index <= cutoff].copy()


# Setup A (pullback continuation) is RETIRED from live scanning as of
# 2026-07-19: the Phase 6/7 replays showed negative expectancy in every
# configuration tested, gross of fees included (docs/PHASE7_SETUP_B_REPLAY.md).
# The evaluator and its tests remain in the repo for a future rework —
# flip this to re-arm scanning after a replay proves a reworked Setup A.
SCAN_SETUP_A = False


def _scan_symbol(symbol: str, positions) -> dict:
    """Pull data and run the setup evaluators for one symbol.

    Returns: {"symbol": ..., "regime": ..., "setup_a": <result|None>,
    "setup_b": <result>} — setup_a is None while SCAN_SETUP_A is False.
    Raises on any error — caller wraps in try/except to keep scanning others.
    """
    daily_raw = get_bars(symbol, "1Day", limit=250)
    h4_raw = get_bars(symbol, "4Hour", limit=250)
    h1_raw = get_bars(symbol, "1Hour", limit=250)

    daily = _drop_in_progress_candle(daily_raw, timedelta(days=1))
    h4 = add_indicators(_drop_in_progress_candle(h4_raw, timedelta(hours=4)))
    h1 = add_indicators(_drop_in_progress_candle(h1_raw, timedelta(hours=1)))

    regime = classify_regime(daily)
    has_position = _has_open_position(symbol, positions)

    setup_a = (evaluate_setup_a(daily, h4, h1, symbol, has_position)
               if SCAN_SETUP_A else None)
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

    # Phase 4 — journal + reconciliation preflight. A missing/unsynced
    # journal or a dirty reconciliation FREEZES entries (fail closed);
    # management and alerts continue regardless.
    journal = None
    entries_frozen_reason: str | None = None
    exec_config = None
    # The journal is built even in alerts-only mode so Phase 5 baseline
    # telemetry accumulates before auto-execution is re-enabled.
    journal, journal_error = build_synced_journal()
    if journal_error:
        print(f"[watcher] journal unavailable: {journal_error}",
              file=sys.stderr)
    if auto_exec:
        if journal_error:
            entries_frozen_reason = journal_error
            print(f"[watcher] journal unavailable: {journal_error} — "
                  f"new entries frozen", file=sys.stderr)
        try:
            exec_config = load_exec_config()
        except ValueError as exc:
            entries_frozen_reason = entries_frozen_reason or f"bad exec config: {exc}"
            print(f"[watcher] exec config invalid: {exc} — new entries "
                  f"frozen", file=sys.stderr)
        if journal is not None and entries_frozen_reason is None:
            # Read-only reconciliation before the entry pass (rule 20).
            try:
                from datetime import timedelta as _td
                from alpaca.trading.enums import QueryOrderStatus
                from alpaca.trading.requests import GetOrdersRequest
                from src.reconciliation import reconcile
                client = get_client()
                report = reconcile(
                    journal,
                    client.get_all_positions(),
                    client.get_orders(filter=GetOrdersRequest(
                        status=QueryOrderStatus.OPEN, limit=500)),
                    client.get_orders(filter=GetOrdersRequest(
                        status=QueryOrderStatus.CLOSED,
                        after=datetime.now(timezone.utc) - _td(days=14),
                        limit=500)),
                )
                print(report.render())
                if not report.ok:
                    entries_frozen_reason = (
                        f"reconciliation found {len(report.findings)} "
                        f"mismatch(es)")
                    send_alert(
                        f"⚠️ Reconciliation mismatches — new entries frozen.\n\n"
                        + "\n".join(str(f) for f in report.findings[:10]))
            except Exception as exc:
                entries_frozen_reason = f"reconciliation could not run: {exc}"
                print(f"[watcher] reconciliation failed: {exc}", file=sys.stderr)

    # Management BEFORE the entry pass so closes free position slots.
    # Runs even when entries are frozen — risk reduction is never gated.
    mgmt_actions: list[dict] = []
    if auto_exec and journal is not None and exec_config is not None:
        try:
            mgmt_actions = manage_swing_trades(
                get_client(), journal, config=exec_config,
                alert_fn=send_alert, get_quote_fn=quote_for,
                regime_fn=regime_for, runner_ctx_fn=runner_ctx_for)
            for action in mgmt_actions:
                print(f"[watcher] mgmt action: {action}")
            if mgmt_actions:
                try:
                    positions = get_positions()
                    print(f"[watcher] positions refreshed after mgmt: {len(positions)}")
                except Exception as exc:
                    print(f"[watcher] FAILED to refresh positions after mgmt: {exc}",
                          file=sys.stderr)
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"[watcher] mgmt pass crashed: {exc}\n{tb}", file=sys.stderr)
            send_alert(f"⚠️ Management pass crashed: {exc}")

    errors: list[str] = []
    scan_results: list[dict] = []
    for symbol in SYMBOLS:
        try:
            result = _scan_symbol(symbol, positions)
            result["execution_notes"] = []
            scan_results.append(result)
            setup_a_state = ("retired" if result["setup_a"] is None
                             else result["setup_a"]["qualified"])
            summary = (
                f"[watcher] {symbol}: regime={result['regime']} "
                f"has_position={result['has_position']} "
                f"setup_a_qualified={setup_a_state} "
                f"setup_b_qualified={result['setup_b']['qualified']}"
            )
            print(summary)

            for setup_result in (result["setup_a"], result["setup_b"]):
                if setup_result is None or not setup_result["qualified"]:
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

                # Phase 4 auto-execution path — every gate fails closed.
                if entries_frozen_reason is not None:
                    note = (f"Setup {setup_result['setup']} blocked: "
                            f"entries frozen ({entries_frozen_reason})")
                    print(f"[watcher] {symbol}: {note}")
                    result["execution_notes"].append(note)
                    continue

                def _portfolio_history():
                    from alpaca.trading.requests import GetPortfolioHistoryRequest
                    hist = get_client().get_portfolio_history(
                        GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
                    return hist.equity or []

                gate = evaluate_entry_gates(
                    journal=journal, client=get_client(), symbol=symbol,
                    get_quote_fn=quote_for,
                    portfolio_history_fn=_portfolio_history)
                if not gate.allowed:
                    note = f"Setup {setup_result['setup']} blocked: {gate}"
                    print(f"[watcher] {symbol}: {note}")
                    result["execution_notes"].append(note)
                    continue

                try:
                    equity = float(get_client().get_account().equity)
                except Exception as exc:
                    note = (f"Setup {setup_result['setup']} blocked: equity "
                            f"re-read failed ({exc}) — fail closed")
                    print(f"[watcher] {symbol}: {note}", file=sys.stderr)
                    result["execution_notes"].append(note)
                    continue

                exec_result = open_protected_trade(
                    get_client(), journal, symbol=symbol,
                    setup=setup_result["setup"],
                    signal_ts=setup_result["signal_bar_ts"],
                    planned_entry=setup_result["entry"],
                    structural_stop=setup_result["stop"],
                    equity=equity, config=exec_config,
                    alert_fn=send_alert)

                note = (f"Setup {setup_result['setup']} "
                        f"{exec_result['status'].upper()}: "
                        f"{exec_result.get('detail') or ''} "
                        f"qty={exec_result.get('filled_qty')} "
                        f"@ {exec_result.get('avg_fill_price')}").strip()
                print(f"[watcher] {symbol}: {note}")
                result["execution_notes"].append(note)
                if exec_result["status"] == "protected":
                    send_alert(format_protected_entry(exec_result))
                elif exec_result["status"] in ("aborted",):
                    send_alert(
                        f"⚠️ ENTRY FAILED — {symbol} "
                        f"(Setup {setup_result['setup']}): "
                        f"{exec_result.get('detail') or 'no details'}")
                # "unwound"/"recovery_required" already alerted CRITICAL
                # inside the execution layer; "skipped" is silent.
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"[watcher] ERROR scanning {symbol}: {exc}\n{tb}", file=sys.stderr)
            errors.append(f"{symbol}: {exc}")

    lifecycle_stats: dict = {}
    if auto_exec:
        try:
            lifecycle_stats = summarize_lifecycle()
            print(f"[watcher] lifecycle: {lifecycle_stats}")
        except Exception as exc:
            print(f"[watcher] lifecycle summary failed: {exc}", file=sys.stderr)

    # Phase 5 — scan telemetry (instrumentation only, no rule changes).
    run_kind = os.environ.get("WATCHER_RUN_KIND", "primary")
    funnel_summary_line = ""
    try:
        from src.funnel import build_scan_record, funnel_line, persist_scan_record
        record = build_scan_record(scan_results, scan_ts=run_started,
                                   run_kind=run_kind)
        funnel_summary_line = funnel_line(scan_results)
        print(f"[watcher] {funnel_summary_line}")
        if journal is not None:
            persisted = persist_scan_record(journal, record)
            # OK/FAILED, not True/False — the literal 'True' collides
            # with GH Actions secret masking (ALPACA_PAPER_TRADE=True).
            print(f"[watcher] telemetry persisted: "
                  f"{'OK' if persisted else 'FAILED'}")
    except Exception as exc:
        print(f"[watcher] telemetry failed (non-blocking): {exc}",
              file=sys.stderr)

    next_scan_eat = _next_scan_eat(run_started)
    summary_msg = format_scan_summary(
        scan_results, errors, run_started, next_scan_eat,
        run_kind=run_kind, mgmt_actions=mgmt_actions,
        lifecycle=lifecycle_stats,
    )
    if funnel_summary_line:
        summary_msg = f"{summary_msg}\n\n{funnel_summary_line}"
    sent = send_alert(summary_msg)
    print(f"[watcher] sent end-of-run summary (kind={run_kind}): {sent}")

    print(f"[watcher] done at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
