"""Day-trade watcher — alerts-only entry point.

D3 deliverable: orchestrates per-scan data pull → indicator compute →
setup eval → Telegram alert. No order placement. Auto-execute (D4)
lands later.

Cron invocation (set via .github/workflows/day-watcher.yml in D5):
- Pre-session scan at 9:25 ET — eligibility check + Telegram summary.
- Intraday scan every 5 minutes during 9:45-15:55 ET — setup eval.
- Watcher exits cleanly outside those windows.
"""
from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from src.data import _assert_paper_mode, get_client
from src.day_calendar import (
    ECON_BLACKOUT_MINUTES,
    is_earnings_blackout,
    is_econ_blackout,
    is_stale,
    load_earnings,
    load_econ_events,
)
from src.day_data import get_pre_market_high_low, get_stock_bars
from src.day_indicators import bar_rvol, session_rvol, session_vwap
from src.day_notifier import (
    format_end_of_session_summary,
    format_pre_session_summary,
    format_setup_alert,
)
from src.day_strategy import (
    classify_intraday_character,
    classify_regime,
    evaluate_setup_a,
    evaluate_setup_b,
    pick_winner,
)
from src.day_trader import (
    check_pre_execution_gates,
    day_auto_execute_enabled,
    manage_open_positions,
    place_entry_bundle,
    summarize_day_lifecycle,
)
from src.heartbeat import ping_heartbeat
from src.indicators import atr, ema
from src.notifier import send_alert

# Universe — single source of truth in src/universe.py. STOCKS is the
# subset subject to the earnings-blackout filter (ETFs exempt).
from src.universe import UNIVERSE, STOCKS_WITH_EARNINGS as STOCKS

ET = ZoneInfo("America/New_York")

# Phase windows in ET. Pre-session is a single-tick window so only the
# 9:25 ET cron firing sends a summary — earlier ticks (9:00-9:20) are
# PHASE_OUTSIDE and no-op. This keeps the Telegram thread quiet:
# previously the watcher fired the same pre-session summary 6 times
# every morning.
_PRE_SESSION_START = time(9, 25)
_PRE_SESSION_END = time(9, 30)
_INTRADAY_SCAN_START = time(9, 45)
# Ends at market close (16:00 ET). The 15:55 ET tick MUST land inside the
# intraday window so manage_open_positions can fire the 3:55 PM hard close
# (src/day_trader.py:_HARD_CLOSE_TIME_ET). Setup detectors gate themselves
# to their own earlier time windows, so no new entries are placed in the
# 15:55-16:00 range — only management actions execute.
_INTRADAY_SCAN_END = time(16, 0)

# End-of-session summary window — single 5-min tick. The 15:50 ET cron
# firing sends one daily wrap-up alert (skip patterns + lifecycle stats).
# Other intraday ticks no-op on Telegram and log only to stdout.
_END_OF_SESSION_START = time(15, 50)
_END_OF_SESSION_END = time(15, 55)

# Scan cadence — drives the "first tick in blackout window" dedup below.
_SCAN_INTERVAL_MINUTES = 5


@dataclass(frozen=True)
class Phase:
    name: str
    is_actionable: bool


PHASE_PRE_SESSION = Phase("pre_session", True)
PHASE_OPENING_OBSERVE = Phase("opening_observe", False)
PHASE_INTRADAY_SCAN = Phase("intraday_scan", True)
PHASE_OUTSIDE = Phase("outside", False)


def determine_phase(now_et: datetime) -> Phase:
    """Map current ET time to a scan phase. Watcher runs only during
    actionable phases — cron schedules outside windows are a no-op.
    """
    if now_et.tzinfo is None:
        raise ValueError("now_et must be tz-aware")
    clock = now_et.astimezone(ET).time()
    if _PRE_SESSION_START <= clock < _PRE_SESSION_END:
        return PHASE_PRE_SESSION
    if _PRE_SESSION_END <= clock < _INTRADAY_SCAN_START:
        return PHASE_OPENING_OBSERVE
    if _INTRADAY_SCAN_START <= clock < _INTRADAY_SCAN_END:
        return PHASE_INTRADAY_SCAN
    return PHASE_OUTSIDE


# ---------------------------------------------------------------------------
# Data composition
# ---------------------------------------------------------------------------


def _add_5min_indicators(
    today_bars: pd.DataFrame,
    history_bars: pd.DataFrame,
) -> pd.DataFrame:
    """Attach the per-bar indicators the day-strategy evaluators need:
    vwap, ema9, ema20, atr14, bar_rvol. session_rvol is fetched
    separately and applied as a pre-filter, not as a column.
    """
    out = today_bars.copy()
    out["vwap"] = session_vwap(out)
    out["ema9"] = ema(out["close"], 9)
    out["ema20"] = ema(out["close"], 20)
    out["atr14"] = atr(out, 14)
    out["bar_rvol"] = bar_rvol(out, history_bars)
    return out


def _drop_in_progress_bars(
    df: pd.DataFrame,
    now_utc: datetime,
    bar_minutes: int = 5,
) -> pd.DataFrame:
    """Keep only CLOSED bars. Alpaca stamps bars with their bucket START
    and the aggregate endpoint includes the partial in-progress bucket,
    so a bar is closed only once `start + bar_minutes <= now`.

    The strategy doc is explicit that setups evaluate on closed candles —
    without this, a mid-bar spike above ORH could trigger an entry on a
    candle that later closes back below the level.
    """
    if len(df) == 0:
        return df
    cutoff = now_utc - timedelta(minutes=bar_minutes)
    return df[df.index <= cutoff]


def _drop_today_daily(daily_df: pd.DataFrame, now_et: datetime) -> pd.DataFrame:
    """Drop today's (in-progress) daily bar from a 1Day pull.

    The IEX feed returns a partial daily bar for the current session, so
    `iloc[-1]` of an unfiltered pull is today's LIVE price — which breaks
    both the overnight-gap math (gap vs "yesterday's close" that is
    actually today's price ≈ 0) and the closed-candle regime doctrine.
    """
    if len(daily_df) == 0:
        return daily_df
    if not isinstance(daily_df.index, pd.DatetimeIndex) or daily_df.index.tz is None:
        return daily_df
    today = now_et.astimezone(ET).date()
    keep = [d < today for d in daily_df.index.tz_convert(ET).date]
    return daily_df[keep]


def _overnight_gap_pct(
    yesterday_daily_df: pd.DataFrame,
    today_5min_df: pd.DataFrame,
) -> float:
    """(today_open − yesterday_close) / yesterday_close.

    Returns 0.0 when either side is missing so the gap-filter doesn't
    falsely trip on data-availability quirks.
    """
    if len(yesterday_daily_df) < 1 or len(today_5min_df) < 1:
        return 0.0
    yc = float(yesterday_daily_df["close"].iloc[-1])
    to = float(today_5min_df["open"].iloc[0])
    if yc == 0:
        return 0.0
    return (to - yc) / yc


def _is_first_blackout_tick(
    now: datetime,
    econ_name: str,
    econ_payload,
    window_minutes: int = ECON_BLACKOUT_MINUTES,
) -> bool:
    """True when `now` falls in the FIRST scan tick of the named event's
    blackout halo. The halo spans ±window_minutes around the release and
    covers ~12 five-minute ticks — Telegram gets one alert (this tick),
    the rest log to stdout only.
    """
    now_utc = now.astimezone(timezone.utc)
    for name, event_ts in econ_payload.events:
        if name != econ_name:
            continue
        window_start = event_ts - timedelta(minutes=window_minutes)
        into_window = now_utc - window_start
        if timedelta(0) <= into_window < timedelta(minutes=_SCAN_INTERVAL_MINUTES):
            return True
    return False


# ---------------------------------------------------------------------------
# Scan paths
# ---------------------------------------------------------------------------


def _resolve_eligibility(
    *,
    now_et: datetime,
    earnings_payload,
    econ_payload,
) -> tuple[list[str], list[tuple[str, str]], str | None]:
    """Determine the eligible universe for today.

    Returns (eligible_symbols, blocked_with_reasons, econ_event_name_today).
    A symbol is blocked if it has earnings today/yesterday OR if either
    calendar payload is stale (then EVERY symbol is blocked; D-doc's
    fail-closed rule).
    """
    today = now_et.astimezone(ET).date()
    eligible: list[str] = []
    blocked: list[tuple[str, str]] = []

    # Stale-calendar fail-closed: blocks the whole session. Staleness is
    # measured relative to the scan's own clock (now_et), not wall-clock —
    # identical in production (now_et is derived from now), but keeps the
    # check deterministic for tests and correct for any historical replay.
    if is_stale(earnings_payload, now=now_et) or is_stale(econ_payload, now=now_et):
        return [], [(s, "stale_calendar") for s in UNIVERSE], None

    # Econ blackout flag (informational here; the actual time-window
    # gating happens in the per-scan path).
    today_econ_events = [
        name for name, ts in econ_payload.events
        if ts.astimezone(ET).date() == today
    ]
    econ_event_today = today_econ_events[0] if today_econ_events else None

    for sym in UNIVERSE:
        if sym in STOCKS and is_earnings_blackout(sym, today, earnings_payload):
            blocked.append((sym, "earnings"))
            continue
        eligible.append(sym)

    return eligible, blocked, econ_event_today


def run_pre_session(now_et: datetime) -> dict:
    """Pre-session scan at 9:25 ET. Pulls SPY daily for regime, computes
    eligibility, sends Telegram summary. Pre-market H/L is best-effort.
    """
    earnings_payload = load_earnings()
    econ_payload = load_econ_events()
    eligible, blocked, econ_event_today = _resolve_eligibility(
        now_et=now_et,
        earnings_payload=earnings_payload,
        econ_payload=econ_payload,
    )

    # SPY daily regime — 260 calendar days back gives ≥200 trading days.
    # Closed candles only: today's partial daily bar is dropped.
    spy_daily = _drop_today_daily(get_stock_bars(
        "SPY", "1Day",
        start=now_et - timedelta(days=400),
        end=now_et,
    ), now_et)
    regime = classify_regime(spy_daily)

    # Account snapshot for the alert body.
    equity = 0.0
    try:
        client = get_client()
        account = client.get_account()
        equity = float(account.equity)
    except Exception:
        # Best-effort — pre-session alert still fires even without account.
        pass

    pmh_pml: dict[str, tuple[float, float] | None] = {}
    today = now_et.astimezone(ET).date()
    for sym in eligible:
        pmh_pml[sym] = get_pre_market_high_low(sym, today)

    msg = format_pre_session_summary(
        now_et=now_et.astimezone(ET),
        equity=equity,
        week_pnl_pct=0.0,  # plumbing for D4 lifecycle stats
        daily_regime=regime,
        intraday_character=None,
        eligible=eligible,
        universe_size=len(UNIVERSE),
        blocked=blocked,
        econ_event_today=econ_event_today,
        pmh_pml=pmh_pml,
    )
    send_alert(msg)
    return {
        "phase": "pre_session",
        "regime": regime,
        "eligible": eligible,
        "blocked": blocked,
        "econ_event_today": econ_event_today,
    }


def _scan_candidate(
    *,
    symbol: str,
    now_et: datetime,
    spy_daily_df: pd.DataFrame,
    spy_5min_with_indicators: pd.DataFrame,
    cand_5min_today: pd.DataFrame,
    cand_5min_history: pd.DataFrame,
    cand_daily: pd.DataFrame,
    has_position: bool,
    in_earnings_blackout: bool,
) -> dict:
    """Run both setup evaluators on `symbol` and return the chosen result
    + skip-reason summary line.
    """
    cand_5min = _add_5min_indicators(cand_5min_today, cand_5min_history)
    gap_pct = _overnight_gap_pct(cand_daily, cand_5min_today)

    # Dead-session pre-filter — session-RVOL on the candidate is a
    # session-level brake. We compute it for the candidate (not SPY)
    # because RVOL is per-symbol.
    s_rvol = session_rvol(cand_5min_today, cand_5min_history)
    dead_session = (
        len(s_rvol) > 0 and pd.notna(s_rvol.iloc[-1])
        and float(s_rvol.iloc[-1]) < 0.7
    )

    if dead_session:
        return {
            "symbol": symbol,
            "qualified_setup": None,
            "skip_reason": "dead_session",
            "session_rvol": float(s_rvol.iloc[-1]),
        }

    setup_a = evaluate_setup_a(
        symbol=symbol, now_et=now_et,
        spy_daily_df=spy_daily_df, spy_5min_df=spy_5min_with_indicators,
        cand_5min_df=cand_5min, has_position=has_position,
        in_earnings_blackout=in_earnings_blackout,
        overnight_gap_pct=gap_pct,
    )
    setup_b = evaluate_setup_b(
        symbol=symbol, now_et=now_et,
        spy_daily_df=spy_daily_df, spy_5min_df=spy_5min_with_indicators,
        cand_5min_df=cand_5min, has_position=has_position,
        in_earnings_blackout=in_earnings_blackout,
        overnight_gap_pct=gap_pct,
    )
    winner = pick_winner(setup_a, setup_b)

    if winner is None:
        # Pick the closer-to-qualifying setup as the primary skip reason.
        a_pass = sum(1 for c in setup_a["conditions"] if c["passed"])
        b_pass = sum(1 for c in setup_b["conditions"] if c["passed"])
        if a_pass >= b_pass:
            failed = [c["name"] for c in setup_a["conditions"] if not c["passed"]]
            return {
                "symbol": symbol,
                "qualified_setup": None,
                "skip_reason": f"A:{','.join(failed[:2])}",
                "setup_a": setup_a, "setup_b": setup_b,
            }
        failed = [c["name"] for c in setup_b["conditions"] if not c["passed"]]
        return {
            "symbol": symbol,
            "qualified_setup": None,
            "skip_reason": f"B:{','.join(failed[:2])}",
            "setup_a": setup_a, "setup_b": setup_b,
        }
    return {
        "symbol": symbol,
        "qualified_setup": winner,
        "skip_reason": None,
        "setup_a": setup_a, "setup_b": setup_b,
    }


def run_intraday_scan(now_et: datetime) -> dict:
    """Single intraday scan tick. Pulls SPY + each candidate, evaluates,
    fires Telegram alerts for qualifiers.
    """
    earnings_payload = load_earnings()
    econ_payload = load_econ_events()
    eligible, blocked, _ = _resolve_eligibility(
        now_et=now_et,
        earnings_payload=earnings_payload,
        econ_payload=econ_payload,
    )

    # Hard stop if econ blackout window is active right now. The halo spans
    # ~12 ticks; only the first one alerts Telegram — the rest would be
    # identical noise (same policy as the removed per-tick no-setups alert).
    in_econ_blackout, econ_name = is_econ_blackout(now_et, econ_payload)
    if in_econ_blackout:
        msg = (
            f"⏸ Day-trade scan @ {now_et.astimezone(ET).strftime('%H:%M ET')} "
            f"— SKIPPED, in econ blackout window ({econ_name})."
        )
        if _is_first_blackout_tick(now_et, econ_name, econ_payload):
            send_alert(msg)
        else:
            print(f"[day-watcher] {msg}")
        return {"phase": "intraday_scan", "skipped": "econ_blackout", "event": econ_name}

    if not eligible:
        # Stale calendar already blocks everything.
        return {"phase": "intraday_scan", "skipped": "no_eligible_universe"}

    # SPY pulls — once per scan. Setup evaluation sees CLOSED candles only:
    # today's partial daily bar and the in-progress 5-min bucket are dropped.
    now_utc = now_et.astimezone(timezone.utc)
    spy_daily = _drop_today_daily(get_stock_bars(
        "SPY", "1Day",
        start=now_et - timedelta(days=400),
        end=now_et,
    ), now_et)
    today_start_utc = now_et.astimezone(ET).replace(
        hour=9, minute=30, second=0, microsecond=0
    ).astimezone(timezone.utc)
    spy_5min_today_raw = get_stock_bars(
        "SPY", "5Min", start=today_start_utc, end=now_et,
    )
    spy_5min_today = _drop_in_progress_bars(spy_5min_today_raw, now_utc)
    spy_5min_hist = get_stock_bars(
        "SPY", "5Min",
        start=now_et - timedelta(days=30), end=today_start_utc,
    )
    spy_5min_with_ind = _add_5min_indicators(spy_5min_today, spy_5min_hist)
    # Management pass gets the RAW pull — manage_position drops the final
    # (possibly in-progress) bar itself; pre-dropping here would delay
    # SPY-VWAP-break detection by one extra tick.
    spy_5min_mgmt = _add_5min_indicators(spy_5min_today_raw, spy_5min_hist)

    # Position lookup — one slot enforced strategy-wide; we ask the
    # broker rather than relying on internal state, matching crypto.
    try:
        client = get_client()
    except Exception as exc:
        send_alert(f"⚠️ day-watcher could not init client: {exc}")
        return {"phase": "intraday_scan", "error": "client_init_failed"}

    # D5b — management pass runs BEFORE the entry scan so any closes
    # free the position slot in time for entry gates to see it.
    mgmt_actions: list[dict] = []
    if day_auto_execute_enabled():
        mgmt_actions = manage_open_positions(
            now_et=now_et,
            spy_5min_with_indicators=spy_5min_mgmt,
            client=client,
        )
        for act in mgmt_actions:
            symbol = act.get("symbol", "?")
            action = act.get("action", "?")
            reason = act.get("reason", "")
            err = act.get("error")
            if err:
                send_alert(f"⚠️ MGMT ERROR — {symbol} {action}: {err}")
            elif action == "breakeven_move":
                send_alert(
                    f"📍 STOP → BREAKEVEN — {symbol} stop moved to "
                    f"${act.get('stop_price'):.2f} (qty {act.get('qty')})."
                )
            elif action == "hard_close_355pm":
                send_alert(
                    f"🕒 3:55 PM EXIT — {symbol} closed at market. "
                    f"({reason}). Cancelled {len(act.get('cancelled_orders', []))} resting orders."
                )
            elif action == "hard_exit_spy_vwap_break":
                send_alert(
                    f"🚪 SPY-VWAP HARD EXIT — {symbol} closed at market. "
                    f"({reason}). Cancelled {len(act.get('cancelled_orders', []))} resting orders."
                )
            elif action == "time_stop":
                send_alert(
                    f"⏱ TIME STOP — {symbol} closed at market. "
                    f"({reason}). Cancelled {len(act.get('cancelled_orders', []))} resting orders."
                )

    # Re-fetch positions after management — closes may have freed the slot.
    # Scoped to the day universe: crypto swing holdings share this paper
    # account and must not permanently block the day strand.
    try:
        positions = client.get_all_positions()
        has_any_position = any(
            getattr(p, "symbol", None) in UNIVERSE for p in positions
        )
    except Exception as exc:
        send_alert(f"⚠️ day-watcher could not fetch positions: {exc}")
        return {"phase": "intraday_scan", "error": "positions_fetch_failed"}

    # D5c — lifecycle stats reconstructed from order history. Computed
    # ONCE per scan and threaded into the entry gate (the expectancy
    # circuit breaker reads expectancy_warning from this dict). Stats
    # are appended to the end-of-scan no-qualifying-setups Telegram.
    try:
        lifecycle = summarize_day_lifecycle(client=client)
    except Exception as exc:
        lifecycle = {"error": f"summarize crashed: {exc}", "days_back": 90}

    today_et = now_et.astimezone(ET).date()
    scan_results: list[dict] = []
    skipped_counter: dict[str, int] = {}

    for sym in eligible:
        try:
            cand_today = _drop_in_progress_bars(get_stock_bars(
                sym, "5Min", start=today_start_utc, end=now_et,
            ), now_utc)
            cand_hist = get_stock_bars(
                sym, "5Min",
                start=now_et - timedelta(days=30), end=today_start_utc,
            )
            cand_daily = _drop_today_daily(get_stock_bars(
                sym, "1Day",
                start=now_et - timedelta(days=5), end=now_et,
            ), now_et)
            in_earnings = (
                sym in STOCKS
                and is_earnings_blackout(sym, today_et, earnings_payload)
            )
            res = _scan_candidate(
                symbol=sym, now_et=now_et,
                spy_daily_df=spy_daily,
                spy_5min_with_indicators=spy_5min_with_ind,
                cand_5min_today=cand_today,
                cand_5min_history=cand_hist,
                cand_daily=cand_daily,
                has_position=has_any_position,
                in_earnings_blackout=in_earnings,
            )
            scan_results.append(res)
            if res["qualified_setup"] is not None:
                setup_result = res["qualified_setup"]
                msg = format_setup_alert(
                    setup_result,
                    daily_regime=classify_regime(spy_daily),
                    intraday_character=classify_intraday_character(
                        spy_5min_with_ind
                    ),
                    auto_execute=day_auto_execute_enabled(),
                )
                send_alert(msg)
                # D5a — auto-execute if kill switch is set. Gates run
                # against broker state; failures surface as a Telegram
                # alert and don't abort the rest of the scan.
                if day_auto_execute_enabled():
                    try:
                        equity = float(client.get_account().equity)
                        gate = check_pre_execution_gates(
                            client, setup_result, equity=equity,
                            lifecycle_stats=lifecycle,
                        )
                        if not gate.allowed:
                            send_alert(
                                f"⏭ AUTO-EXEC SKIP — {sym} Setup {setup_result['setup']}: "
                                f"{gate.reason}"
                            )
                        else:
                            exec_result = place_entry_bundle(
                                setup_result,
                                equity=equity,
                                client=client,
                            )
                            if exec_result.get("placed", False):
                                # Slot taken — later symbols in THIS scan
                                # must see the position (the broker-side
                                # gate backstops this, but the evaluators'
                                # C10 check reads this flag).
                                has_any_position = True
                            if not exec_result.get("placed", False):
                                send_alert(
                                    f"⚠️ AUTO-EXEC FAILED — {sym} entry rejected: "
                                    f"{exec_result.get('skip_reason') or exec_result.get('errors')}"
                                )
                            elif not exec_result.get("protective_orders_complete", False):
                                send_alert(
                                    f"🚨 AUTO-EXEC PARTIAL — {sym} entry filled at "
                                    f"${exec_result.get('fill_price')} BUT some protective "
                                    f"orders failed: {exec_result.get('errors')}. "
                                    f"Order IDs: {exec_result.get('order_ids')}. "
                                    f"MANUALLY VERIFY POSITION."
                                )
                            else:
                                tp2_str = (
                                    f"${exec_result['tp2_price']:.2f}"
                                    if exec_result.get("tp2_price") is not None
                                    else "n/a"
                                )
                                notional = exec_result['fill_price'] * exec_result['shares']
                                risk_per_share = exec_result['fill_price'] - exec_result['stop_price']
                                risk_total = risk_per_share * exec_result['shares']
                                send_alert(
                                    f"✅ AUTO-EXEC FILLED — {sym} Setup "
                                    f"{setup_result['setup']} @ ${exec_result['fill_price']:.2f} "
                                    f"× {exec_result['shares']} sh "
                                    f"(${notional:,.0f} notional, ${risk_total:,.0f} risk). "
                                    f"Stop ${exec_result['stop_price']:.2f}, "
                                    f"TP1 ${exec_result['tp1_price']:.2f}, "
                                    f"TP2 {tp2_str}"
                                )
                    except Exception as exc:
                        send_alert(f"⚠️ AUTO-EXEC error on {sym}: {exc}")
            else:
                reason = res.get("skip_reason") or "unknown"
                skipped_counter[reason] = skipped_counter.get(reason, 0) + 1
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"[day-watcher] error scanning {sym}: {exc}\n{tb}",
                  file=sys.stderr)
            send_alert(f"⚠️ day-watcher error on {sym}: {exc}")

    qualifying = [r for r in scan_results if r["qualified_setup"] is not None]

    # End-of-session summary at the 15:50 ET tick (last intraday tick
    # before the 15:55 hard close). Other ticks just log to stdout —
    # per-setup alerts already fire individually in real-time, so a
    # per-tick "no qualifying setups" Telegram was pure noise.
    clock = now_et.astimezone(ET).time()
    is_end_of_session = _END_OF_SESSION_START <= clock < _END_OF_SESSION_END
    if is_end_of_session:
        send_alert(format_end_of_session_summary(
            now_et=now_et.astimezone(ET),
            candidates_scanned=len(scan_results),
            skipped_summary=skipped_counter,
            qualified_count=len(qualifying),
            lifecycle_stats=lifecycle,
        ))
    else:
        # Stdout-only debug summary — visible in GH Actions logs, not Telegram.
        print(
            f"[day-watcher] scan complete: scanned={len(scan_results)} "
            f"qualified={len(qualifying)} skipped={dict(skipped_counter)}"
        )

    return {
        "phase": "intraday_scan",
        "scanned": len(scan_results),
        "qualified": len(qualifying),
        "skipped": skipped_counter,
        "lifecycle": lifecycle,
        "sent_end_of_session_summary": is_end_of_session,
    }


def main() -> int:
    _assert_paper_mode()
    now_et = datetime.now(timezone.utc).astimezone(ET)
    phase = determine_phase(now_et)
    print(f"[day-watcher] phase={phase.name} now_et={now_et.isoformat()}")

    if not phase.is_actionable:
        return 0

    try:
        if phase is PHASE_PRE_SESSION:
            run_pre_session(now_et)
        elif phase is PHASE_INTRADAY_SCAN:
            run_intraday_scan(now_et)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[day-watcher] FAILED: {exc}\n{tb}", file=sys.stderr)
        send_alert(f"⚠️ day-watcher crash in phase {phase.name}: {exc}")
        # Flip the dead-man's switch to "down" now rather than waiting for
        # the silence window — a crash is a real failure the watchdog should
        # surface immediately.
        ping_heartbeat(fail=True)
        return 1
    # Scan completed cleanly — tell the external watchdog we're alive. Pings
    # only fire on actionable phases, matching the healthcheck's market-hours
    # cron so off-hours silence never trips a false alarm.
    ping_heartbeat()
    return 0


if __name__ == "__main__":
    sys.exit(main())
