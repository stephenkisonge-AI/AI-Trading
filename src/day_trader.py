"""Phases D5a + D5b — auto-execution + in-trade management for the
day-trade strategy.

D5a (entries): called by the day-watcher when a setup qualifies AND
WATCHER_DAY_AUTO_EXECUTE=true. Places:
    1. Market entry BUY
    2. Wait for fill
    3. Two OCO bundles, each covering half the shares:
        OCO1: tp1_qty shares, stop_loss @ stop  + take_profit @ TP1
        OCO2: tp2_qty shares, stop_loss @ stop  + take_profit @ TP2
       When TP1 fills, OCO1's stop auto-cancels; OCO2 keeps protecting
       the runner. Two OCOs (rather than one stop + two limits) avoid
       Alpaca's `held_for_orders` reservation conflict that caused TP
       placements to be rejected with insufficient-qty.

D5b (management): runs at the TOP of every intraday scan before the
entry pass, so any closes free the position slot. For the (at most)
one open position, applies in priority order:
    1. 3:55 PM exit  — close via close_position (atomic cancel+close).
    2. SPY-VWAP break — close via close_position if SPY 5-min closed
       below its session VWAP after our entry filled.
    3. TP1 fill detected — replace OCO2's stop leg to avg_entry_price
       (breakeven) via replace_order_by_id, preserving the OCO link
       so TP2's limit stays alive.
    4. Time stop — if (now − fill_time) ≥ 30 min AND current price <
       entry + 0.25R, close via close_position.
    (Circuit-breaker halt detection is intentionally deferred — Alpaca
    doesn't expose a clean "halted" status on the position side.)

Safety contract:
    - Refuses to act unless ALPACA_PAPER_TRADE=True. Live trading is
      always manual (the doc's two-switch promise).
    - Runs all pre-execution gates from Day_Trading_Strategy.md §"Risk
      caps" before placing the entry. Failures return a SkipDecision
      with the reason; the watcher logs it.
    - Never raises out of `place_entry_bundle` or `manage_position` —
      partial-bundle failures are captured in the returned dict so the
      caller can fire an urgent Telegram alert.

Deferred to D5c:
    - Weekly loss cap and consecutive-loss cooldown (need realized-P&L
      reconstruction from order history).
    - Per-scan lifecycle stats block.
"""
from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as _time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from src.data import get_client


# --- Tunables (keep in sync with Day_Trading_Strategy.md) ----------------
# Notional cap is a safety belt that only binds when the stop is so tight
# that the risk-percent allowance would deploy an unreasonably large
# position. At $30,000 cap with 1% stops, max risk per trade is $300
# (well under the 0.5% × equity allowance for a $100K+ account). The cap
# was previously $500, calibrated to a much smaller account size — that
# value silently throttled every trade to ~1 share at typical prices,
# making the $500/week target structurally unreachable.
_MAX_NOTIONAL_USD = 30_000.0
_MIN_NOTIONAL_USD = 50.0
_MIN_STOP_DIST_PCT = 0.003  # 0.3% — tighter = normal noise stops us out
_MAX_STOP_DIST_PCT = 0.03   # 3%   — wider = 2R unreachable intraday
_RISK_PER_TRADE_PCT = 0.005  # 0.5% of equity (half of swing)
_MAX_TRADES_PER_SESSION = 3
_DAILY_LOSS_CAP_PCT = 0.015  # -1.5% from prior session equity
# Weekly loss cap — Day_Trading_Strategy.md §"Risk caps": −4% from
# week-start equity stops trading until the following Monday. Measured
# from Alpaca portfolio history (1W window), so it includes the crypto
# strand's P&L too — account-level risk brake, deliberately strict.
_WEEKLY_LOSS_CAP_PCT = 0.04
# Consecutive-loss cooldowns — doc §"Risk caps": 2 losing trades in one
# session stops the session; 3 losing sessions in a row pauses ~5
# trading days (approximated as 7 calendar days, stateless).
_SESSION_LOSS_STOP_COUNT = 2
_LOSING_SESSION_STREAK = 3
_LOSING_STREAK_PAUSE_CAL_DAYS = 7
# Spread gate — doc lists 0.05% consolidated NBBO; IEX top-of-book
# (the free feed) prints wider than NBBO, so 0.10% is the IEX-adjusted
# equivalent. Quote failures fail OPEN (IEX quote gaps are data quirks).
_SPREAD_CAP_PCT = 0.001
_FILL_POLL_TIMEOUT_SEC = 30
_FILL_POLL_INTERVAL_SEC = 1

# --- D5b management tunables ---------------------------------------------
# 3:55 PM ET hard close — all positions flat by 4:00 PM.
_HARD_CLOSE_TIME_ET = _time(15, 55)
# Time stop: if no progress 30 min after fill AND price < entry + 0.25R.
_TIME_STOP_MINUTES = 30
_TIME_STOP_R_FRACTION = 0.25
# Order-history lookback when reconstructing position state from Alpaca
# (find fill_time, find original stop). 1 day is enough for intraday.
_ORDER_HISTORY_LOOKBACK_DAYS = 1
# After cancelling resting orders pre-close, wait up to this long for the
# `held_for_orders` reservation to release before calling close_position.
_CANCEL_RELEASE_TIMEOUT_SEC = 15
_CANCEL_RELEASE_POLL_SEC = 0.5
# Statuses that mean an order still holds (or may hold) share reservations.
_OPEN_ORDER_STATUSES = (
    "new", "accepted", "pending_new", "accepted_for_bidding",
    "held", "partially_filled",
)

# --- D5c lifecycle tunables ----------------------------------------------
# Per Day_Trading_Strategy.md §"Phase D5c". 90 days mirrors crypto.
_LIFECYCLE_LOOKBACK_DAYS = 90
# Below this mean R after the sample threshold = stop the experiment.
_EXPECTANCY_MIN_R = 0.2
# Day-trade sample threshold — doc says "at least 50 closed day trades"
# (vs swing's 30) because per-trade edge is smaller intraday.
_MIN_SAMPLE_FOR_EXPECTANCY = 50
# client_order_id prefix used to tag entry BUYs with setup type — parsed
# back out during lifecycle reconstruction.
_CLIENT_ORDER_ID_PREFIX = "DAY"
# Env var that lets the user override the expectancy circuit breaker.
# When the lifecycle expectancy_warning fires, new entries are refused
# unless this is set to "true". Existing positions are still managed
# normally. Doc: Day_Trading_Strategy.md §"Reminder to myself".
_OVERRIDE_EXPECTANCY_ENV_VAR = "WATCHER_DAY_OVERRIDE_EXPECTANCY"
# -------------------------------------------------------------------------

from src.universe import UNIVERSE  # single source of truth — edit src/universe.py
ET = ZoneInfo("America/New_York")


@dataclass
class SkipDecision:
    allowed: bool
    reason: str


def day_auto_execute_enabled() -> bool:
    """True only if both env flags are set correctly. Live trading is
    never auto-executed regardless of WATCHER_DAY_AUTO_EXECUTE.
    """
    if os.environ.get("WATCHER_DAY_AUTO_EXECUTE", "").lower() != "true":
        return False
    if os.environ.get("ALPACA_PAPER_TRADE") != "True":
        return False
    return True


def day_shorts_enabled() -> bool:
    """Short-side kill switch — fail-closed like the auto-execute flag.
    Short setups ALERT regardless; execution requires the literal string
    "true" in WATCHER_DAY_ENABLE_SHORTS (GitHub Secret).
    """
    return os.environ.get("WATCHER_DAY_ENABLE_SHORTS", "").lower() == "true"


def compute_position_size(
    equity: float, entry: float, stop: float, direction: str = "long",
) -> dict:
    """R-based sizing: deploy notional such that loss-at-stop = 0.5% of
    equity, capped by `_MAX_NOTIONAL_USD` for tight-stop safety.

    For longs the stop must be below entry; for shorts, above. All caps
    and floors apply identically to both directions.

    Returns a dict with `shares` (int) and either a `skip_reason` (str)
    when sizing is rejected or `notional` + `risk_dollars` when accepted.
    """
    if entry <= 0 or stop <= 0:
        return {"shares": 0, "skip_reason": "invalid_entry_or_stop"}
    if direction == "long" and stop >= entry:
        return {"shares": 0, "skip_reason": "invalid_entry_or_stop"}
    if direction == "short" and stop <= entry:
        return {"shares": 0, "skip_reason": "invalid_entry_or_stop"}
    stop_dist_pct = abs(entry - stop) / entry
    if stop_dist_pct < _MIN_STOP_DIST_PCT:
        return {"shares": 0, "skip_reason": "stop_too_tight_under_0.3pct"}
    if stop_dist_pct > _MAX_STOP_DIST_PCT:
        return {"shares": 0, "skip_reason": "stop_too_wide_over_3pct"}

    risk_dollars = _RISK_PER_TRADE_PCT * equity
    notional_needed = risk_dollars / stop_dist_pct
    notional_capped = min(notional_needed, _MAX_NOTIONAL_USD)
    if notional_capped < _MIN_NOTIONAL_USD:
        return {"shares": 0, "skip_reason": "notional_below_50_floor"}

    shares = int(notional_capped / entry)  # round down to whole shares
    if shares < 1:
        return {"shares": 0, "skip_reason": "fractional_under_one_share"}

    return {
        "shares": shares,
        "notional": shares * entry,
        "risk_dollars": risk_dollars,
        "stop_dist_pct": stop_dist_pct,
    }


def _count_today_entries(client) -> int:
    """Count today's filled ENTRY orders for universe symbols.

    Entries are identified by the DAY- client_order_id tag rather than by
    side — with shorts enabled, an entry can be a SELL and a cover a BUY,
    so side alone no longer distinguishes entries from exits. Manual
    untagged trades no longer consume the session cap (they never had
    protective bundles anyway).
    """
    today_et = datetime.now(timezone.utc).astimezone(ET).date()
    start_utc = datetime.combine(today_et, _time(0, 0), tzinfo=ET).astimezone(timezone.utc)
    request = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=start_utc,
        symbols=UNIVERSE,
        limit=100,
    )
    orders = client.get_orders(filter=request)
    return sum(
        1 for o in orders
        if getattr(o, "filled_at", None) is not None
        and str(getattr(o, "client_order_id", "") or "").startswith(
            _CLIENT_ORDER_ID_PREFIX + "-"
        )
    )


def _gate_daily_loss(client) -> Optional[SkipDecision]:
    """Daily loss limit check: (equity − last_equity) / last_equity ≤ −1.5%.

    `last_equity` is Alpaca's prior-session-close equity, so this counts
    today's net change including unrealized — slightly stricter than the
    doc's realized-only definition. Stricter is safer.
    """
    try:
        account = client.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
    except Exception as exc:
        return SkipDecision(False, f"could_not_fetch_account: {exc}")
    if last_equity <= 0:
        return None
    pct = (equity - last_equity) / last_equity
    if pct <= -_DAILY_LOSS_CAP_PCT:
        return SkipDecision(
            False, f"daily_loss_limit_hit_{pct * 100:.2f}pct"
        )
    return None


def _gate_weekly_loss(client) -> Optional[SkipDecision]:
    """Weekly loss cap — doc §"Risk caps": −4% from week-start equity
    stops new entries until the window rolls. Fail-open on infra errors
    (portfolio history unavailable ≠ strategy violation), matching the
    crypto strand's posture.
    """
    try:
        hist = client.get_portfolio_history(filter=GetPortfolioHistoryRequest(
            period="1W", timeframe="1D",
        ))
        equities = [e for e in (hist.equity or []) if e is not None and e > 0]
    except Exception as exc:
        print(f"[day_trader] weekly cap: portfolio history failed: {exc}",
              file=sys.stderr)
        return None
    if len(equities) < 2:
        return None
    weekly_pl = (equities[-1] - equities[0]) / equities[0]
    if weekly_pl <= -_WEEKLY_LOSS_CAP_PCT:
        return SkipDecision(
            False, f"weekly_loss_cap_hit_{weekly_pl * 100:.2f}pct"
        )
    return None


def _gate_consecutive_losses(
    lifecycle_stats: dict | None,
    now_et: datetime | None = None,
) -> Optional[SkipDecision]:
    """Doc §"Risk caps" cooldowns, reconstructed statelessly from the
    lifecycle `sessions` block:
    - 2 losing trades in TODAY's session → no more entries today.
    - Last 3 sessions-with-trades all net-negative → pause ~5 trading
      days (7 calendar days) from the most recent losing session.

    Fail-open when stats are missing or errored (same posture as the
    expectancy gate).
    """
    if not lifecycle_stats or lifecycle_stats.get("error"):
        return None
    sessions = lifecycle_stats.get("sessions") or {}
    if not sessions:
        return None
    now_et = now_et or datetime.now(timezone.utc).astimezone(ET)
    today = now_et.astimezone(ET).date()

    today_stats = sessions.get(today.isoformat())
    if today_stats and today_stats.get("losing", 0) >= _SESSION_LOSS_STOP_COUNT:
        return SkipDecision(
            False,
            f"session_loss_cooldown_{today_stats['losing']}_losses_today",
        )

    dates = sorted(sessions.keys())
    recent = dates[-_LOSING_SESSION_STREAK:]
    if (len(recent) == _LOSING_SESSION_STREAK
            and all(sessions[d].get("net_pl", 0.0) < 0 for d in recent)):
        last_losing = date.fromisoformat(recent[-1])
        if (today - last_losing).days <= _LOSING_STREAK_PAUSE_CAL_DAYS:
            return SkipDecision(
                False,
                f"losing_streak_pause_until_"
                f"{last_losing + timedelta(days=_LOSING_STREAK_PAUSE_CAL_DAYS)}",
            )
    return None


def _gate_spread(symbol: str) -> Optional[SkipDecision]:
    """Spread cap — skip entries when the IEX top-of-book spread exceeds
    _SPREAD_CAP_PCT. Fail-open on any fetch problem or degenerate quote:
    IEX (free feed) legitimately prints empty/zero quotes at quiet
    moments, and that's a data quirk, not a reason to veto the trade.
    """
    try:
        from src.day_data import get_stock_latest_quote
        quote = get_stock_latest_quote(symbol)
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)
    except Exception as exc:
        print(f"[day_trader] spread gate: quote fetch failed for {symbol}: {exc}",
              file=sys.stderr)
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (ask + bid) / 2.0
    spread_pct = (ask - bid) / mid
    if spread_pct > _SPREAD_CAP_PCT:
        return SkipDecision(
            False,
            f"spread_{spread_pct * 100:.3f}pct_exceeds_{_SPREAD_CAP_PCT * 100:.2f}pct_cap",
        )
    return None


def _gate_expectancy_warning(lifecycle_stats: dict | None) -> Optional[SkipDecision]:
    """Refuse new entries when the lifecycle expectancy_warning is active,
    unless the user has explicitly overridden via env var.

    Returns None to mean "proceed" (no warning, or override active, or
    no stats available). Returns SkipDecision when the circuit breaker
    should trip.

    Fail-open posture:
    - When lifecycle_stats is None or has an error field, this gate is
      skipped. The lifecycle fetch failing isn't itself a reason to
      block trading — that's a data-availability problem, not a
      strategy violation.
    """
    if lifecycle_stats is None:
        return None
    if lifecycle_stats.get("error"):
        return None
    warning = lifecycle_stats.get("expectancy_warning")
    if not warning:
        return None
    if os.environ.get(_OVERRIDE_EXPECTANCY_ENV_VAR, "").lower() == "true":
        return None
    return SkipDecision(
        False,
        f"expectancy_circuit_breaker: {warning} "
        f"(set {_OVERRIDE_EXPECTANCY_ENV_VAR}=true to override)",
    )


def check_pre_execution_gates(
    client,
    setup_result: dict,
    equity: float,
    lifecycle_stats: dict | None = None,
) -> SkipDecision:
    """Returns SkipDecision(allowed=True, reason="") when the trade may
    proceed, else SkipDecision(allowed=False, reason="..."). Order of
    checks is deliberate — cheapest first, so we minimize Alpaca calls
    on the no-trade path.

    `lifecycle_stats` is the dict returned by summarize_day_lifecycle.
    When provided AND its expectancy_warning is active AND the user
    has not set WATCHER_DAY_OVERRIDE_EXPECTANCY=true, new entries are
    refused. Pass None to bypass the expectancy/cooldown gates entirely
    (used by tests).
    """
    direction = setup_result.get("direction", "long")

    # Strategy hygiene first — is the sizing valid.
    sizing = compute_position_size(
        equity, setup_result["entry"], setup_result["stop"], direction=direction,
    )
    if "skip_reason" in sizing:
        return SkipDecision(False, sizing["skip_reason"])

    # Short-side kill switch — alerts fire regardless, execution is
    # fail-closed until WATCHER_DAY_ENABLE_SHORTS=true.
    if direction == "short" and not day_shorts_enabled():
        return SkipDecision(
            False,
            "shorts_disabled (set WATCHER_DAY_ENABLE_SHORTS=true to enable)",
        )

    # Expectancy circuit breaker — checked before any Alpaca calls so the
    # rejection path stays cheap. lifecycle_stats was already computed
    # once at the top of the scan, so this gate is essentially free.
    expectancy_skip = _gate_expectancy_warning(lifecycle_stats)
    if expectancy_skip is not None:
        return expectancy_skip

    # Consecutive-loss cooldowns (2-loss session stop / losing-streak
    # pause) — also free, reads the same lifecycle stats.
    cooldown_skip = _gate_consecutive_losses(lifecycle_stats)
    if cooldown_skip is not None:
        return cooldown_skip

    # One-position rule — asked of the broker at execution time, not scan
    # time, so a fill earlier in the same scan can't slip a second entry
    # through. Scoped to the day universe: crypto swing holdings in the
    # shared paper account neither block nor are blocked by day trades.
    try:
        positions = client.get_all_positions()
    except Exception as exc:
        return SkipDecision(False, f"could_not_fetch_positions: {exc}")
    open_day_positions = [
        p for p in positions if getattr(p, "symbol", None) in UNIVERSE
    ]
    if open_day_positions:
        held = getattr(open_day_positions[0], "symbol", "?")
        return SkipDecision(False, f"position_already_open_{held}")

    # Session-cap check.
    try:
        n_today = _count_today_entries(client)
    except Exception as exc:
        return SkipDecision(False, f"could_not_fetch_orders: {exc}")
    if n_today >= _MAX_TRADES_PER_SESSION:
        return SkipDecision(False, f"session_trade_cap_reached_{n_today}")

    # Daily loss cap.
    daily_skip = _gate_daily_loss(client)
    if daily_skip is not None:
        return daily_skip

    # Weekly loss cap.
    weekly_skip = _gate_weekly_loss(client)
    if weekly_skip is not None:
        return weekly_skip

    # Spread cap — last (one extra data call, only on otherwise-passing
    # trades).
    spread_skip = _gate_spread(setup_result["symbol"])
    if spread_skip is not None:
        return spread_skip

    return SkipDecision(True, "")


def _extract_stop_leg_id(oco_order) -> str | None:
    """Return the ID of the stop-loss leg of a returned OCO order, or None
    if the SDK didn't populate `.legs`. The OCO parent's `.legs` is a list
    of child orders; one is the take-profit limit, one is the stop-loss
    stop. We need the stop leg's ID to later replace it with a breakeven
    stop without breaking the OCO link.
    """
    legs = getattr(oco_order, "legs", None) or []
    for leg in legs:
        if "stop" in str(getattr(leg, "order_type", "")).lower():
            return str(leg.id)
    return None


def _wait_for_fill(client, order_id, timeout_sec: int = _FILL_POLL_TIMEOUT_SEC):
    """Poll until the order fills or we time out. Returns the order object
    on fill, raises TimeoutError otherwise.
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        order = client.get_order_by_id(order_id)
        # Normalize both enum reprs ("OrderStatus.FILLED") and plain strings
        # ("filled") to the bare status word. Exact match matters:
        # "partially_filled" must NOT count as filled — sizing the OCOs off
        # a partial fill would leave sell qty exceeding the position.
        status = str(getattr(order, "status", "")).lower().split(".")[-1]
        if status == "filled":
            return order
        if status in ("rejected", "canceled", "cancelled", "expired"):
            raise RuntimeError(f"entry order ended in status {status}")
        time.sleep(_FILL_POLL_INTERVAL_SEC)
    raise TimeoutError(f"order {order_id} not filled within {timeout_sec}s")


def place_entry_bundle(setup_result: dict, equity: float, client=None) -> dict:
    """Place market entry + two OCO bundles (50/50 scale-out).

    Two OCOs are used (rather than one stop + two TP limits) because
    Alpaca reserves the full position qty against a stop sell, leaving
    zero `available` for separate TP limit orders. Each OCO atomically
    pairs a stop_loss + take_profit for its half of the shares, so the
    `held_for_orders` total matches position size exactly.

    Returns a dict with:
        - placed: bool — entry buy at minimum was submitted
        - protective_orders_complete: bool — entry + OCOs all submitted
        - fill_price: float — actual fill price of the entry
        - shares: int
        - order_ids: dict with entry / oco_tp1 / oco_tp1_stop /
          oco_tp2 / oco_tp2_stop IDs (None for any that failed)
        - errors: list of (component, exception_str) for partial failures
        - skip_reason: str | None — set when sizing rejected
    """
    if client is None:
        client = get_client()

    direction = setup_result.get("direction", "long")
    is_short = direction == "short"
    entry_side = OrderSide.SELL if is_short else OrderSide.BUY
    exit_side = OrderSide.BUY if is_short else OrderSide.SELL

    sizing = compute_position_size(
        equity, setup_result["entry"], setup_result["stop"],
        direction=direction,
    )
    if "skip_reason" in sizing:
        return {
            "placed": False, "protective_orders_complete": False,
            "skip_reason": sizing["skip_reason"],
        }

    symbol = setup_result["symbol"]
    shares = sizing["shares"]
    # TP1 takes 50% of shares (round down); TP2 takes the remainder.
    tp1_qty = shares // 2
    tp2_qty = shares - tp1_qty
    if tp1_qty < 1 or tp2_qty < 1:
        # Can't split 1 share cleanly between two TP orders. Send the
        # single share to TP1 and skip TP2 — D5b management will close
        # the runner via the time-stop or 3:55 PM hard exit.
        tp1_qty = shares
        tp2_qty = 0

    errors: list[tuple[str, str]] = []
    order_ids: dict[str, str | None] = {
        "entry": None,
        "oco_tp1": None, "oco_tp1_stop": None,
        "oco_tp2": None, "oco_tp2_stop": None,
    }

    # --- 1. Market entry (BUY for longs, SELL-short for shorts) ---
    # Tag with client_order_id encoding setup type + direction — D5c
    # lifecycle stats parses this back out for per-setup expectancy AND
    # to reconstruct short trades (entry side is sell there).
    # Format: DAY-{A|B|AS|BS}-{SYMBOL}-{epoch_seconds}. Safely under
    # Alpaca's 48-char client_order_id limit.
    setup_token = f"{setup_result['setup']}{'S' if is_short else ''}"
    epoch_s = int(datetime.now(timezone.utc).timestamp())
    client_order_id = (
        f"{_CLIENT_ORDER_ID_PREFIX}-{setup_token}-"
        f"{symbol}-{epoch_s}"
    )
    try:
        entry_req = MarketOrderRequest(
            symbol=symbol, qty=shares, side=entry_side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        entry_order = client.submit_order(entry_req)
        order_ids["entry"] = str(entry_order.id)
    except Exception as exc:
        return {
            "placed": False, "protective_orders_complete": False,
            "errors": [("entry", str(exc))], "order_ids": order_ids,
        }

    # Wait for fill — without a confirmed fill, protective orders are
    # placing into thin air.
    try:
        filled = _wait_for_fill(client, entry_order.id)
        fill_price = float(filled.filled_avg_price)
        filled_at = getattr(filled, "filled_at", None)
    except Exception as exc:
        return {
            "placed": True, "protective_orders_complete": False,
            "errors": [("entry_fill_wait", str(exc))],
            "order_ids": order_ids,
        }

    stop_px = round(setup_result["stop"], 2)

    tp1_px = round(setup_result["tp1"], 2)
    tp2_px = round(setup_result["tp2"], 2)

    # --- 2. OCO bundle covering TP1's half ---
    # Alpaca's OCO requires BOTH take_profit and stop_loss children to be
    # explicit. The parent's limit_price alone is rejected with
    # "oco orders require take_profit.limit_price" (error 40010001).
    # For shorts the OCO children are BUYs: TP limit below entry,
    # protective stop above.
    try:
        oco1 = client.submit_order(LimitOrderRequest(
            symbol=symbol, qty=tp1_qty, side=exit_side,
            time_in_force=TimeInForce.DAY,
            limit_price=tp1_px,
            order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=tp1_px),
            stop_loss=StopLossRequest(stop_price=stop_px),
        ))
        order_ids["oco_tp1"] = str(oco1.id)
        order_ids["oco_tp1_stop"] = _extract_stop_leg_id(oco1)
    except Exception as exc:
        errors.append(("oco_tp1", str(exc)))

    # --- 3. OCO bundle covering TP2's half (skipped for 1-share case) ---
    if tp2_qty > 0:
        try:
            oco2 = client.submit_order(LimitOrderRequest(
                symbol=symbol, qty=tp2_qty, side=exit_side,
                time_in_force=TimeInForce.DAY,
                limit_price=tp2_px,
                order_class=OrderClass.OCO,
                take_profit=TakeProfitRequest(limit_price=tp2_px),
                stop_loss=StopLossRequest(stop_price=stop_px),
            ))
            order_ids["oco_tp2"] = str(oco2.id)
            order_ids["oco_tp2_stop"] = _extract_stop_leg_id(oco2)
        except Exception as exc:
            errors.append(("oco_tp2", str(exc)))

    needed = {"entry", "oco_tp1"}
    if tp2_qty > 0:
        needed.add("oco_tp2")
    protective_ok = all(order_ids.get(k) is not None for k in needed)

    return {
        "placed": True,
        "protective_orders_complete": protective_ok,
        "symbol": symbol,
        "direction": direction,
        "shares": shares,
        "fill_price": fill_price,
        "filled_at": filled_at,
        "stop_price": round(setup_result["stop"], 2),
        "tp1_price": round(setup_result["tp1"], 2),
        "tp1_qty": tp1_qty,
        "tp2_price": round(setup_result["tp2"], 2) if tp2_qty > 0 else None,
        "tp2_qty": tp2_qty,
        "setup": setup_result["setup"],
        "order_ids": order_ids,
        "errors": errors,
    }


# =========================================================================
# Phase D5b — in-trade management
# =========================================================================


def _list_orders_since(client, symbol: str, since: datetime, statuses=None):
    """Fetch orders for `symbol` filled/created since `since` UTC.

    Default returns all statuses (open + closed). Filtering by side or
    status happens at the call site so the helper stays general.
    """
    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        after=since,
        symbols=[symbol],
        limit=100,
    )
    return list(client.get_orders(filter=request))


def _position_direction(position) -> str:
    """"long" or "short", from the position's qty sign (Alpaca reports
    short positions with negative qty) with the side attribute as a
    fallback.
    """
    try:
        qty = float(getattr(position, "qty", 0) or 0)
        if qty < 0:
            return "short"
        if qty > 0:
            return "long"
    except (TypeError, ValueError):
        pass
    side = str(getattr(position, "side", "")).lower()
    return "short" if side.endswith("short") else "long"


def _find_entry_fill(client, symbol: str, now_utc: datetime,
                     direction: str = "long"):
    """Find the most recent filled ENTRY order for `symbol` — a BUY for
    longs, a SELL for shorts. Returns the order object or None.

    Used to derive fill_time (for the time stop) and to anchor "since
    entry" predicates.
    """
    entry_side = "sell" if direction == "short" else "buy"
    since = now_utc - timedelta(days=_ORDER_HISTORY_LOOKBACK_DAYS)
    orders = _list_orders_since(client, symbol, since)
    fills = [
        o for o in orders
        if str(getattr(o, "side", "")).lower().endswith(entry_side)
        and getattr(o, "filled_at", None) is not None
    ]
    if not fills:
        return None
    # Most recent fill — assume single intraday position.
    fills.sort(key=lambda o: o.filled_at, reverse=True)
    return fills[0]


def _find_resting_stop(client, symbol: str, now_utc: datetime,
                       direction: str = "long"):
    """Return the (single) resting protective stop for `symbol`, or None.
    Exit side: SELL stop for longs, BUY stop for shorts.
    """
    exit_side = "buy" if direction == "short" else "sell"
    since = now_utc - timedelta(days=_ORDER_HISTORY_LOOKBACK_DAYS)
    orders = _list_orders_since(client, symbol, since)
    for o in orders:
        is_open = (str(getattr(o, "status", "")).lower().split(".")[-1]
                   in _OPEN_ORDER_STATUSES)
        is_exit = str(getattr(o, "side", "")).lower().endswith(exit_side)
        is_stop = str(getattr(o, "order_type", "")).lower().endswith("stop")
        if is_open and is_exit and is_stop:
            return o
    return None


def _detect_tp1_filled(client, symbol: str, since: datetime,
                       direction: str = "long") -> bool:
    """True if a non-stop limit EXIT has filled for `symbol` since
    `since` — a limit SELL for longs, a limit BUY (cover) for shorts.

    A filled exit limit is unambiguous evidence TP1 fired — the entry
    bundle only places exit limits at TP1 / TP2 prices, and TP1 fires
    first by construction (closer to entry).
    """
    exit_side = "buy" if direction == "short" else "sell"
    orders = _list_orders_since(client, symbol, since)
    for o in orders:
        is_filled = getattr(o, "filled_at", None) is not None
        is_exit = str(getattr(o, "side", "")).lower().endswith(exit_side)
        is_limit = str(getattr(o, "order_type", "")).lower().endswith("limit")
        is_not_stop = "stop" not in str(getattr(o, "order_type", "")).lower()
        if is_filled and is_exit and is_limit and is_not_stop:
            return True
    return False


def _open_orders_for(client, symbol: str) -> list:
    """Open orders for `symbol`, defensively re-filtered by status string
    so PENDING_CANCEL / already-cancelled orders don't count as open.
    """
    orders = client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN, symbols=[symbol],
    ))
    return [
        o for o in orders
        if str(getattr(o, "status", "")).lower().split(".")[-1]
        in _OPEN_ORDER_STATUSES
    ]


def _close_position_market(client, symbol: str, position, reason: str,
                           now_utc: datetime) -> dict:
    """Cancel resting orders, wait for the share reservation to release,
    then market-close the position.

    Alpaca's `close_position` does NOT cancel related orders server-side
    — with OCO exits resting it rejects with "insufficient qty available"
    (code 40310000; live-verified on paper 2026-07-02, both directions).
    So: explicit cancel sweep first, then poll until no open orders
    remain (cancellation is async — `held_for_orders` releases with a
    small lag), then close.

    Never raises — failures are captured in the returned dict.
    """
    qty = float(getattr(position, "qty", 0))
    out: dict = {
        "reason": reason,
        "cancelled_orders": [],
        "close_order_id": None, "qty": qty, "error": None,
    }
    try:
        for o in _open_orders_for(client, symbol):
            try:
                client.cancel_order_by_id(o.id)
                out["cancelled_orders"].append(str(o.id))
            except Exception as exc:
                print(f"[day_trader] cancel {getattr(o, 'id', '?')} failed: {exc}",
                      file=sys.stderr)
        deadline = time.monotonic() + _CANCEL_RELEASE_TIMEOUT_SEC
        while _open_orders_for(client, symbol) and time.monotonic() < deadline:
            time.sleep(_CANCEL_RELEASE_POLL_SEC)
    except Exception as exc:
        # Sweep failures shouldn't stop the close attempt — worst case it
        # fails with the same insufficient-qty error and alerts.
        print(f"[day_trader] pre-close cancel sweep failed for {symbol}: {exc}",
              file=sys.stderr)
    try:
        order = client.close_position(symbol)
        out["close_order_id"] = str(getattr(order, "id", "") or "")
    except Exception as exc:
        out["error"] = f"close_position failed: {exc}"
    return out


def _move_stop_to_breakeven(client, symbol: str, position, old_stop,
                            remaining_qty: float) -> dict:
    """Replace the resting OCO stop leg's stop_price with avg_entry_price.

    Uses `replace_order_by_id` rather than cancel+resubmit. After TP1
    fills, OCO2 still has both a stop leg and a TP2 limit leg linked
    OCO-style — cancelling the stop would also cancel the TP2 limit
    and leave the runner without a target. Replacing the stop leg in
    place preserves the OCO link and keeps TP2 alive.

    `remaining_qty` is recorded for diagnostics; the qty on the leg is
    already correct (OCO2 was sized to tp2_qty at entry).
    """
    avg_entry = float(getattr(position, "avg_entry_price", 0) or 0)
    if avg_entry <= 0:
        return {"success": False, "error": "missing avg_entry_price"}
    stop_px = round(avg_entry, 2)
    out: dict = {
        "old_stop_order_id": str(getattr(old_stop, "id", "")),
        "old_stop_price": float(getattr(old_stop, "stop_price", 0) or 0),
        "new_stop_order_id": None, "stop_price": stop_px,
        "qty": remaining_qty, "success": False, "error": None,
    }
    try:
        replaced = client.replace_order_by_id(
            old_stop.id,
            ReplaceOrderRequest(stop_price=stop_px),
        )
        # Alpaca's replace returns a new Order with a fresh ID; the OCO
        # link follows the leg automatically.
        out["new_stop_order_id"] = str(getattr(replaced, "id", "") or old_stop.id)
        out["success"] = True
    except Exception as exc:
        out["error"] = f"replace stop failed: {exc}"
    return out


def _spy_broke_vwap_after(spy_5min_with_indicators, entry_fill_time_utc: datetime,
                          direction: str = "long") -> bool:
    """True if any closed SPY 5-min bar AFTER our entry fill closed on the
    adverse side of its session VWAP — below for longs (buyers lost the
    tape), ABOVE for shorts (sellers lost it).

    `spy_5min_with_indicators` is already pulled + computed by the
    watcher — re-using it here avoids a second Alpaca round-trip.
    """
    if spy_5min_with_indicators is None or len(spy_5min_with_indicators) == 0:
        return False
    df = spy_5min_with_indicators
    # Index is tz-aware UTC. Drop in-progress (last) bar — only closed
    # bars count for hard exits.
    if len(df) < 2:
        return False
    closed = df.iloc[:-1]
    after_entry = closed[closed.index > entry_fill_time_utc]
    if len(after_entry) == 0:
        return False
    if direction == "short":
        return bool((after_entry["close"] > after_entry["vwap"]).any())
    return bool((after_entry["close"] < after_entry["vwap"]).any())


def manage_position(
    *,
    client,
    position,
    now_et: datetime,
    spy_5min_with_indicators,
) -> dict | None:
    """Apply the day-trade management rules in priority order.

    Returns an action dict if something was done, else None.
    Priority: 3:55 close → SPY-VWAP break → TP1→breakeven → time stop.

    Caller is responsible for iterating over open positions; this
    function handles one position at a time.
    """
    symbol = getattr(position, "symbol", None)
    if not symbol:
        return None

    direction = _position_direction(position)
    is_short = direction == "short"
    now_utc = now_et.astimezone(timezone.utc)
    et_clock = now_et.astimezone(ET).time()

    # --- 1. 3:55 PM hard close ---
    if et_clock >= _HARD_CLOSE_TIME_ET:
        res = _close_position_market(
            client, symbol, position,
            reason=f"3:55 PM hard close (now {et_clock.strftime('%H:%M')} ET)",
            now_utc=now_utc,
        )
        return {"action": "hard_close_355pm", "symbol": symbol, **res}

    # Reconstruct entry fill — needed for SPY-VWAP-after-entry check
    # AND for time stop.
    entry_fill = _find_entry_fill(client, symbol, now_utc, direction=direction)
    if entry_fill is None:
        # Position exists but no recent entry fill — could be a manually
        # opened position or an Alpaca lag. Bail rather than misbehave.
        return None
    fill_time_utc = entry_fill.filled_at.astimezone(timezone.utc) \
        if entry_fill.filled_at.tzinfo else entry_fill.filled_at.replace(tzinfo=timezone.utc)
    avg_entry = float(getattr(position, "avg_entry_price", 0) or 0)

    # --- 2. SPY VWAP break against our direction after entry ---
    if _spy_broke_vwap_after(spy_5min_with_indicators, fill_time_utc,
                             direction=direction):
        side_word = "above" if is_short else "below"
        res = _close_position_market(
            client, symbol, position,
            reason=f"SPY 5-min close {side_word} session VWAP after entry",
            now_utc=now_utc,
        )
        return {"action": "hard_exit_spy_vwap_break", "symbol": symbol, **res}

    # --- 3. TP1 fill → move stop to breakeven ---
    tp1_filled = _detect_tp1_filled(client, symbol, fill_time_utc,
                                    direction=direction)
    resting_stop = _find_resting_stop(client, symbol, now_utc,
                                      direction=direction)
    if tp1_filled and resting_stop is not None:
        stop_px = float(getattr(resting_stop, "stop_price", 0) or 0)
        # Only move if the current stop is still on the loss side of
        # avg_entry (below it for longs, ABOVE it for shorts) — i.e. we
        # haven't already moved it to breakeven on a prior scan.
        needs_move = (stop_px > avg_entry) if is_short else (stop_px < avg_entry)
        if stop_px > 0 and needs_move:
            remaining_qty = float(getattr(position, "qty", 0))
            rep = _move_stop_to_breakeven(
                client, symbol, position, resting_stop, remaining_qty,
            )
            return {"action": "breakeven_move", "symbol": symbol, **rep}

    # --- 4. Time stop: 30 min after fill without ≥ 0.25R of progress ---
    minutes_since_fill = (now_utc - fill_time_utc).total_seconds() / 60.0
    if minutes_since_fill >= _TIME_STOP_MINUTES and not tp1_filled:
        # Recover R from the resting stop if we still have it.
        r_value = None
        if resting_stop is not None and avg_entry > 0:
            stop_px = float(getattr(resting_stop, "stop_price", 0) or 0)
            if stop_px > 0:
                r_value = (stop_px - avg_entry) if is_short else (avg_entry - stop_px)
        if r_value is not None and r_value > 0:
            current_price = float(getattr(position, "current_price", 0) or 0)
            threshold = (
                avg_entry - _TIME_STOP_R_FRACTION * r_value if is_short
                else avg_entry + _TIME_STOP_R_FRACTION * r_value
            )
            stalled = (
                current_price > threshold if is_short
                else current_price < threshold
            )
            if current_price > 0 and stalled:
                cmp_word = ">" if is_short else "<"
                res = _close_position_market(
                    client, symbol, position,
                    reason=(
                        f"time stop: {int(minutes_since_fill)} min since fill, "
                        f"price {current_price:.2f} {cmp_word} entry±0.25R "
                        f"{threshold:.2f}"
                    ),
                    now_utc=now_utc,
                )
                return {"action": "time_stop", "symbol": symbol, **res}

    return None


def manage_open_positions(
    *,
    now_et: datetime,
    spy_5min_with_indicators,
    client=None,
) -> list[dict]:
    """Run management over all open positions. Strategy doc caps at 1
    position; the loop handles N anyway for robustness.

    Returns a list of action dicts; empty list when nothing was done.
    """
    if client is None:
        client = get_client()
    try:
        positions = client.get_all_positions()
    except Exception as exc:
        return [{"action": "error", "error": f"could not fetch positions: {exc}"}]

    actions: list[dict] = []
    for pos in positions:
        # The crypto swing strand shares this paper account. Its positions
        # (BTCUSD etc.) must never be touched by day-trade management —
        # the 3:55 PM hard close would liquidate a multi-day swing hold.
        if getattr(pos, "symbol", None) not in UNIVERSE:
            continue
        try:
            res = manage_position(
                client=client, position=pos, now_et=now_et,
                spy_5min_with_indicators=spy_5min_with_indicators,
            )
            if res is not None:
                actions.append(res)
        except Exception as exc:
            actions.append({
                "action": "error",
                "symbol": getattr(pos, "symbol", "?"),
                "error": f"management crashed: {exc}",
            })
    return actions


# =========================================================================
# Phase D5c — lifecycle stats
# =========================================================================


_SETUP_TOKENS = ("A", "B", "AS", "BS")  # AS/BS = short-side variants


def _parse_setup_from_client_order_id(client_order_id: str | None) -> str:
    """Recover setup token from the entry order's client_order_id.

    Format: DAY-{A|B|AS|BS}-{SYMBOL}-{epoch_seconds}. Returns the token
    or 'unknown' when the prefix is missing or malformed (covers orders
    placed manually or by older code).
    """
    if not client_order_id:
        return "unknown"
    parts = client_order_id.split("-")
    if len(parts) < 2 or parts[0] != _CLIENT_ORDER_ID_PREFIX:
        return "unknown"
    setup = parts[1]
    return setup if setup in _SETUP_TOKENS else "unknown"


def _trade_pl(t: dict) -> float:
    """Realized P&L of a (closed) trade dict, direction-aware."""
    entry_value = t["entry_qty"] * t["entry_price"]
    exit_value = sum(e["qty"] * e["price"] for e in t["exits"])
    if t.get("direction", "long") == "short":
        return entry_value - exit_value
    return exit_value - entry_value


def _trade_risk_per_unit(t: dict) -> float | None:
    """Original per-share risk from the stops seen on the trade. For
    longs the original stop is the LOWEST stop seen (stops only move up);
    for shorts it's the HIGHEST (stops only move down). None when no
    stop was recoverable or the geometry is degenerate.
    """
    if not t["stops_seen"]:
        return None
    if t.get("direction", "long") == "short":
        risk = max(t["stops_seen"]) - t["entry_price"]
    else:
        risk = t["entry_price"] - min(t["stops_seen"])
    return risk if risk > 0 else None


def _trade_walk_for_symbol(orders_sorted: list) -> list[dict]:
    """Walk a single symbol's CLOSED orders (chronological) and produce
    trade objects.

    A LONG trade opens on a filled BUY (tagged or not — legacy behavior)
    and closes when sold qty equals the entry qty. A SHORT trade opens on
    a filled SELL carrying a DAY-{AS|BS} client_order_id tag (short
    entries are always auto-executed and therefore always tagged) and
    closes on cover BUYs. Untagged sells with no open trade are ignored,
    as before.

    Each trade dict carries `setup` + `direction` recovered from the
    entry's client_order_id; `stops_seen` (for R reconstruction); `exits`
    list with reason classification (STOP / TP / MGMT_CLOSE); and
    `entry_at` / `last_exit_at` for duration math.
    """
    trades: list[dict] = []
    current: dict | None = None

    def _is_buy(o):
        return str(getattr(o, "side", "")).lower().endswith("buy")

    def _is_sell(o):
        return str(getattr(o, "side", "")).lower().endswith("sell")

    def _otype(o):
        return str(getattr(o, "order_type", "")).lower()

    def _open_trade(o, fq, fp, filled_at, direction):
        return {
            "symbol": getattr(o, "symbol", ""),
            "setup": _parse_setup_from_client_order_id(
                getattr(o, "client_order_id", None)
            ),
            "direction": direction,
            "entry_at": filled_at,
            "entry_qty": fq,
            "entry_price": fp,
            "qty_remaining": fq,
            "exits": [],
            "stops_seen": [],
            "last_exit_at": None,
        }

    def _record_exit(o, fq, fp, stop_px, filled_at):
        otype = _otype(o)
        if "stop" in otype:
            if stop_px > 0:
                current["stops_seen"].append(stop_px)
            reason = "STOP"
        elif "limit" in otype:
            reason = "TP"
        else:
            reason = "MGMT_CLOSE"
        if fq > 0:
            current["exits"].append({"qty": fq, "price": fp, "reason": reason})
            current["qty_remaining"] -= fq
            current["last_exit_at"] = filled_at

    for o in orders_sorted:
        fq = float(getattr(o, "filled_qty", 0) or 0)
        fp = float(getattr(o, "filled_avg_price", 0) or 0)
        stop_px = float(getattr(o, "stop_price", 0) or 0)
        filled_at = getattr(o, "filled_at", None)
        setup_token = _parse_setup_from_client_order_id(
            getattr(o, "client_order_id", None)
        )
        is_short_entry_tag = setup_token in ("AS", "BS")

        if _is_buy(o):
            if current is not None and current["direction"] == "short":
                # Cover BUY: exit fill, or resting BUY-stop carrying the
                # short's protective stop price (fq == 0 records it only).
                _record_exit(o, fq, fp, stop_px, filled_at)
            elif fq > 0:
                # New long entry (defensively closes any stale trade).
                if current is not None:
                    trades.append(current)
                current = _open_trade(o, fq, fp, filled_at, "long")
                continue
            else:
                continue
        elif _is_sell(o):
            if fq > 0 and is_short_entry_tag:
                # Tagged short entry (defensively closes any stale trade).
                if current is not None:
                    trades.append(current)
                current = _open_trade(o, fq, fp, filled_at, "short")
                continue
            if current is None or current["direction"] != "long":
                # Untagged sells with no open long: nothing to attribute.
                continue
            _record_exit(o, fq, fp, stop_px, filled_at)
        else:
            continue

        if current is not None and current["qty_remaining"] < current["entry_qty"] * 0.01:
            trades.append(current)
            current = None

    if current is not None:
        trades.append(current)
    return trades


def _aggregate_bucket(trades: list[dict]) -> dict:
    """Compute closed/wins/mean_r for a bucket of trades."""
    closed = [t for t in trades if t["qty_remaining"] < t["entry_qty"] * 0.01]
    wins = 0
    r_multiples: list[float] = []
    for t in closed:
        pl = _trade_pl(t)
        if pl > 0:
            wins += 1
        risk_per_unit = _trade_risk_per_unit(t)
        if risk_per_unit is not None:
            r_multiples.append(pl / (t["entry_qty"] * risk_per_unit))
    return {
        "closed": len(closed),
        "wins": wins,
        "mean_r": (sum(r_multiples) / len(r_multiples)) if r_multiples else None,
    }


def summarize_day_lifecycle(
    days_back: int = _LIFECYCLE_LOOKBACK_DAYS,
    client=None,
) -> dict:
    """Reconstruct closed day-trade trades from Alpaca order history and
    compute aggregate stats.

    Stateless — runs each scan. Cheap (one orders fetch). Returns the
    same shape regardless of whether auto-execute is currently enabled,
    so toggling the kill switch doesn't lose visibility into prior
    auto-executed trades.

    Schema:
        {
          "days_back": int,
          "total_closed": int,  "open_trades": int,
          "wins": int,          "losses": int,
          "win_rate": float | None,
          "total_pl_usd": float,
          "mean_r": float | None,  "best_r": float | None, "worst_r": float | None,
          "avg_minutes_in_trade": float | None,
          "sessions": {iso_date: {net_pl, closed, losing}, ...},
          "by_setup": {"A": {...}, "B": {...}, "AS": {...}, "BS": {...},
                       "unknown": {...}},
          "by_symbol": {sym: {...}, ...},
          "expectancy_warning": str | None,
          "error": str | None,   # set when the orders fetch fails
        }
    """
    if client is None:
        client = get_client()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    try:
        closed_orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=cutoff,
            symbols=UNIVERSE,
            limit=500,
        ))
    except Exception as exc:
        return {
            "days_back": days_back,
            "error": f"order fetch failed: {exc}",
        }

    by_symbol: dict[str, list] = {}
    for o in closed_orders:
        sym = getattr(o, "symbol", None)
        if sym:
            by_symbol.setdefault(sym, []).append(o)

    def _ts(o):
        return (
            getattr(o, "submitted_at", None)
            or getattr(o, "created_at", None)
            or datetime.min.replace(tzinfo=timezone.utc)
        )

    all_trades: list[dict] = []
    for sym, orders in by_symbol.items():
        orders.sort(key=_ts)
        all_trades.extend(_trade_walk_for_symbol(orders))

    closed = [t for t in all_trades if t["qty_remaining"] < t["entry_qty"] * 0.01]
    open_trades = len(all_trades) - len(closed)

    stats: dict = {
        "days_back": days_back,
        "total_closed": len(closed),
        "open_trades": open_trades,
        "wins": 0, "losses": 0,
        "win_rate": None,
        "total_pl_usd": 0.0,
        "mean_r": None, "best_r": None, "worst_r": None,
        "avg_minutes_in_trade": None,
        "sessions": {},
        "by_setup": {},
        "by_symbol": {},
        "expectancy_warning": None,
        "error": None,
    }

    r_multiples: list[float] = []
    durations_min: list[float] = []
    sessions: dict[str, dict] = {}
    for t in closed:
        pl = _trade_pl(t)
        stats["total_pl_usd"] += pl
        if pl > 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        risk_per_unit = _trade_risk_per_unit(t)
        if risk_per_unit is not None:
            r_multiples.append(pl / (t["entry_qty"] * risk_per_unit))
        if t["entry_at"] and t["last_exit_at"]:
            try:
                dur = (t["last_exit_at"] - t["entry_at"]).total_seconds() / 60.0
                if dur >= 0:
                    durations_min.append(dur)
            except (TypeError, AttributeError):
                pass
        # Per-session ledger — feeds the consecutive-loss cooldown gate.
        if t["entry_at"] is not None:
            try:
                session_key = t["entry_at"].astimezone(ET).date().isoformat()
            except (TypeError, AttributeError):
                session_key = None
            if session_key:
                s = sessions.setdefault(
                    session_key, {"net_pl": 0.0, "closed": 0, "losing": 0},
                )
                s["net_pl"] += pl
                s["closed"] += 1
                if pl < 0:
                    s["losing"] += 1

    if stats["total_closed"] > 0:
        stats["win_rate"] = stats["wins"] / stats["total_closed"]
    if r_multiples:
        stats["mean_r"] = sum(r_multiples) / len(r_multiples)
        stats["best_r"] = max(r_multiples)
        stats["worst_r"] = min(r_multiples)
        if (stats["total_closed"] >= _MIN_SAMPLE_FOR_EXPECTANCY
                and stats["mean_r"] < _EXPECTANCY_MIN_R):
            stats["expectancy_warning"] = (
                f"mean R {stats['mean_r']:+.2f} below +{_EXPECTANCY_MIN_R}R "
                f"after {stats['total_closed']} day trades — "
                f"strategy doc says STOP the experiment"
            )
    if durations_min:
        stats["avg_minutes_in_trade"] = sum(durations_min) / len(durations_min)
    stats["sessions"] = sessions

    # Per-setup and per-symbol breakdowns. AS/BS are the short variants.
    by_setup_trades: dict[str, list[dict]] = {
        "A": [], "B": [], "AS": [], "BS": [], "unknown": [],
    }
    by_symbol_trades: dict[str, list[dict]] = {}
    for t in all_trades:
        by_setup_trades.setdefault(t["setup"], []).append(t)
        by_symbol_trades.setdefault(t["symbol"], []).append(t)
    for setup, trades in by_setup_trades.items():
        stats["by_setup"][setup] = _aggregate_bucket(trades)
    for sym, trades in by_symbol_trades.items():
        stats["by_symbol"][sym] = _aggregate_bucket(trades)

    return stats
