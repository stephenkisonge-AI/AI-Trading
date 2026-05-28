"""Phases D5a + D5b — auto-execution + in-trade management for the
day-trade strategy.

D5a (entries): called by the day-watcher when a setup qualifies AND
WATCHER_DAY_AUTO_EXECUTE=true. Places a 4-order bundle:
    1. Market entry (BUY)
    2. Wait for fill
    3. Stop-market SELL (full qty, DAY) at the strategy-defined stop
    4. TP1 limit SELL (50% qty, DAY) at +1R
    5. TP2 limit SELL (50% qty, DAY) at +2R

D5b (management): runs at the TOP of every intraday scan before the
entry pass, so any closes free the position slot. For the (at most)
one open position, applies in priority order:
    1. 3:55 PM exit  — close at market, cancel resting orders.
    2. SPY-VWAP break — close at market if SPY 5-min closed below its
       session VWAP after our entry filled.
    3. TP1 fill detected — cancel original stop, place new stop-market
       at avg_entry_price (breakeven).
    4. Time stop — if (now − fill_time) ≥ 30 min AND current price <
       entry + 0.25R, close at market.
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

from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from src.data import get_client


# --- Tunables (keep in sync with Day_Trading_Strategy.md) ----------------
_MAX_NOTIONAL_USD = 500.0
_MIN_NOTIONAL_USD = 50.0
_MIN_STOP_DIST_PCT = 0.003  # 0.3% — tighter = normal noise stops us out
_MAX_STOP_DIST_PCT = 0.03   # 3%   — wider = 2R unreachable intraday
_RISK_PER_TRADE_PCT = 0.005  # 0.5% of equity (half of swing)
_MAX_TRADES_PER_SESSION = 3
_DAILY_LOSS_CAP_PCT = 0.015  # -1.5% from prior session equity
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
# -------------------------------------------------------------------------

UNIVERSE = ["NVDA", "TSLA", "AAPL", "AMZN", "GOOGL", "MSFT", "GLD"]
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


def compute_position_size(equity: float, entry: float, stop: float) -> dict:
    """R-based sizing capped at $500 notional. Returns a dict with `shares`
    (int) and either a `skip_reason` (str) when sizing is rejected or
    `notional` + `risk_dollars` when accepted.
    """
    if entry <= 0 or stop <= 0 or stop >= entry:
        return {"shares": 0, "skip_reason": "invalid_entry_or_stop"}
    stop_dist_pct = (entry - stop) / entry
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
    """Count today's filled BUY orders for universe symbols."""
    today_et = datetime.now(timezone.utc).astimezone(ET).date()
    start_utc = datetime.combine(today_et, _time(0, 0), tzinfo=ET).astimezone(timezone.utc)
    request = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=start_utc,
        symbols=UNIVERSE,
        side="buy",
        limit=50,
    )
    orders = client.get_orders(filter=request)
    return sum(1 for o in orders if getattr(o, "filled_at", None) is not None)


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


def check_pre_execution_gates(client, setup_result: dict, equity: float) -> SkipDecision:
    """Returns SkipDecision(allowed=True, reason="") when the trade may
    proceed, else SkipDecision(allowed=False, reason="..."). Order of
    checks is deliberate — cheapest first, so we minimize Alpaca calls
    on the no-trade path.
    """
    # Strategy hygiene first — would the setup itself sizing be valid.
    sizing = compute_position_size(equity, setup_result["entry"], setup_result["stop"])
    if "skip_reason" in sizing:
        return SkipDecision(False, sizing["skip_reason"])

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

    return SkipDecision(True, "")


def _wait_for_fill(client, order_id, timeout_sec: int = _FILL_POLL_TIMEOUT_SEC):
    """Poll until the order fills or we time out. Returns the order object
    on fill, raises TimeoutError otherwise.
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        order = client.get_order_by_id(order_id)
        status = getattr(order, "status", None)
        if str(status).lower().endswith("filled"):
            return order
        if str(status).lower() in ("rejected", "canceled", "expired"):
            raise RuntimeError(f"entry order ended in status {status}")
        time.sleep(_FILL_POLL_INTERVAL_SEC)
    raise TimeoutError(f"order {order_id} not filled within {timeout_sec}s")


def place_entry_bundle(setup_result: dict, equity: float, client=None) -> dict:
    """Place market entry + stop-market + TP1 limit + TP2 limit.

    Returns a dict with:
        - placed: bool — entry buy at minimum was submitted
        - protective_orders_complete: bool — stop + TP1 + TP2 all submitted
        - fill_price: float — actual fill price of the entry
        - shares: int
        - order_ids: dict with entry/stop/tp1/tp2 IDs (None for any that failed)
        - errors: list of (component, exception_str) for partial failures
        - skip_reason: str | None — set when sizing rejected
    """
    if client is None:
        client = get_client()

    sizing = compute_position_size(
        equity, setup_result["entry"], setup_result["stop"]
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
        "entry": None, "stop": None, "tp1": None, "tp2": None,
    }

    # --- 1. Market BUY entry ---
    try:
        entry_req = MarketOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
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

    # --- 2. Stop-market SELL (full qty) ---
    try:
        stop_req = StopOrderRequest(
            symbol=symbol, qty=shares, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=round(setup_result["stop"], 2),
        )
        stop_order = client.submit_order(stop_req)
        order_ids["stop"] = str(stop_order.id)
    except Exception as exc:
        errors.append(("stop", str(exc)))

    # --- 3. TP1 limit SELL (50%) ---
    try:
        tp1_req = LimitOrderRequest(
            symbol=symbol, qty=tp1_qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=round(setup_result["tp1"], 2),
        )
        tp1_order = client.submit_order(tp1_req)
        order_ids["tp1"] = str(tp1_order.id)
    except Exception as exc:
        errors.append(("tp1", str(exc)))

    # --- 4. TP2 limit SELL (50%) ---
    if tp2_qty > 0:
        try:
            tp2_req = LimitOrderRequest(
                symbol=symbol, qty=tp2_qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round(setup_result["tp2"], 2),
            )
            tp2_order = client.submit_order(tp2_req)
            order_ids["tp2"] = str(tp2_order.id)
        except Exception as exc:
            errors.append(("tp2", str(exc)))

    needed = {"entry", "stop", "tp1"}
    if tp2_qty > 0:
        needed.add("tp2")
    protective_ok = all(order_ids.get(k) is not None for k in needed)

    return {
        "placed": True,
        "protective_orders_complete": protective_ok,
        "symbol": symbol,
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


def _find_entry_fill(client, symbol: str, now_utc: datetime):
    """Find the most recent filled BUY order for `symbol`. Returns the
    order object or None.

    Used to derive fill_time (for the time stop) and to anchor "since
    entry" predicates.
    """
    since = now_utc - timedelta(days=_ORDER_HISTORY_LOOKBACK_DAYS)
    orders = _list_orders_since(client, symbol, since)
    fills = [
        o for o in orders
        if str(getattr(o, "side", "")).lower().endswith("buy")
        and getattr(o, "filled_at", None) is not None
    ]
    if not fills:
        return None
    # Most recent fill — assume single intraday position.
    fills.sort(key=lambda o: o.filled_at, reverse=True)
    return fills[0]


def _find_resting_stop(client, symbol: str, now_utc: datetime):
    """Return the (single) resting stop SELL order for `symbol`, or None.

    The day-trade entry bundle places exactly one stop-market SELL.
    """
    since = now_utc - timedelta(days=_ORDER_HISTORY_LOOKBACK_DAYS)
    orders = _list_orders_since(client, symbol, since)
    for o in orders:
        is_open = str(getattr(o, "status", "")).lower().split(".")[-1] in (
            "new", "accepted", "pending_new", "accepted_for_bidding",
            "held", "partially_filled",
        )
        is_sell = str(getattr(o, "side", "")).lower().endswith("sell")
        is_stop = str(getattr(o, "order_type", "")).lower().endswith("stop")
        if is_open and is_sell and is_stop:
            return o
    return None


def _detect_tp1_filled(client, symbol: str, since: datetime) -> bool:
    """True if a non-stop limit SELL has filled for `symbol` since `since`.

    A filled limit-sell is unambiguous evidence TP1 fired — the entry
    bundle only places limit SELLs at TP1 / TP2 prices, and TP1 fires
    first by construction (lower price).
    """
    orders = _list_orders_since(client, symbol, since)
    for o in orders:
        is_filled = getattr(o, "filled_at", None) is not None
        is_sell = str(getattr(o, "side", "")).lower().endswith("sell")
        is_limit = str(getattr(o, "order_type", "")).lower().endswith("limit")
        is_not_stop = "stop" not in str(getattr(o, "order_type", "")).lower()
        if is_filled and is_sell and is_limit and is_not_stop:
            return True
    return False


def _cancel_resting_orders(client, symbol: str, now_utc: datetime) -> list[str]:
    """Cancel all open orders for `symbol`. Returns list of cancelled IDs.

    Best-effort: individual cancel failures don't abort the loop — the
    caller can still proceed to close the position.
    """
    since = now_utc - timedelta(days=_ORDER_HISTORY_LOOKBACK_DAYS)
    orders = _list_orders_since(client, symbol, since)
    cancelled: list[str] = []
    for o in orders:
        is_open = str(getattr(o, "status", "")).lower().split(".")[-1] in (
            "new", "accepted", "pending_new", "accepted_for_bidding",
            "held", "partially_filled",
        )
        if not is_open:
            continue
        try:
            client.cancel_order_by_id(o.id)
            cancelled.append(str(o.id))
        except Exception:
            # Best-effort. Log nothing — D5b summary captures aggregate.
            pass
    return cancelled


def _close_position_market(client, symbol: str, position, reason: str,
                           now_utc: datetime) -> dict:
    """Cancel all open orders for `symbol`, then market-sell the qty.

    Mirrors the crypto pattern. Never raises — failures captured in the
    returned dict.
    """
    cancelled = _cancel_resting_orders(client, symbol, now_utc)
    qty = float(getattr(position, "qty", 0))
    out: dict = {
        "reason": reason, "cancelled_orders": cancelled,
        "close_order_id": None, "qty": qty, "error": None,
    }
    try:
        order = client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        out["close_order_id"] = str(order.id)
    except Exception as exc:
        out["error"] = f"close submit failed: {exc}"
    return out


def _move_stop_to_breakeven(client, symbol: str, position, old_stop,
                            remaining_qty: float) -> dict:
    """Cancel old stop, place new stop-market at avg_entry_price for the
    given remaining qty.

    `remaining_qty` is passed in rather than read from `position.qty`
    because position.qty reflects pre-TP1-fill quantity in some Alpaca
    snapshots — caller computes it from the now-known fill events.
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
        client.cancel_order_by_id(old_stop.id)
    except Exception as exc:
        out["error"] = f"cancel old stop failed: {exc}"
        return out
    try:
        new_stop = client.submit_order(StopOrderRequest(
            symbol=symbol, qty=remaining_qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=stop_px,
        ))
        out["new_stop_order_id"] = str(new_stop.id)
        out["success"] = True
    except Exception as exc:
        out["error"] = (
            f"new stop submit failed: {exc} "
            f"(OLD STOP CANCELLED — POSITION TEMPORARILY UNPROTECTED)"
        )
    return out


def _spy_broke_vwap_after(spy_5min_with_indicators, entry_fill_time_utc: datetime) -> bool:
    """True if any closed SPY 5-min bar AFTER our entry fill closed below
    its session VWAP.

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
    entry_fill = _find_entry_fill(client, symbol, now_utc)
    if entry_fill is None:
        # Position exists but no recent BUY fill — could be a manually
        # opened position or an Alpaca lag. Bail rather than misbehave.
        return None
    fill_time_utc = entry_fill.filled_at.astimezone(timezone.utc) \
        if entry_fill.filled_at.tzinfo else entry_fill.filled_at.replace(tzinfo=timezone.utc)
    avg_entry = float(getattr(position, "avg_entry_price", 0) or 0)

    # --- 2. SPY VWAP break after our entry ---
    if _spy_broke_vwap_after(spy_5min_with_indicators, fill_time_utc):
        res = _close_position_market(
            client, symbol, position,
            reason="SPY 5-min close below session VWAP after entry",
            now_utc=now_utc,
        )
        return {"action": "hard_exit_spy_vwap_break", "symbol": symbol, **res}

    # --- 3. TP1 fill → move stop to breakeven ---
    tp1_filled = _detect_tp1_filled(client, symbol, fill_time_utc)
    resting_stop = _find_resting_stop(client, symbol, now_utc)
    if tp1_filled and resting_stop is not None:
        stop_px = float(getattr(resting_stop, "stop_price", 0) or 0)
        # Only move if the current stop is BELOW avg_entry — i.e. we
        # haven't already moved it to breakeven on a prior scan.
        if stop_px < avg_entry:
            remaining_qty = float(getattr(position, "qty", 0))
            rep = _move_stop_to_breakeven(
                client, symbol, position, resting_stop, remaining_qty,
            )
            return {"action": "breakeven_move", "symbol": symbol, **rep}

    # --- 4. Time stop: 30 min after fill AND price < entry + 0.25R ---
    minutes_since_fill = (now_utc - fill_time_utc).total_seconds() / 60.0
    if minutes_since_fill >= _TIME_STOP_MINUTES and not tp1_filled:
        # Recover R from the resting stop if we still have it.
        r_value = None
        if resting_stop is not None and avg_entry > 0:
            stop_px = float(getattr(resting_stop, "stop_price", 0) or 0)
            r_value = avg_entry - stop_px if stop_px > 0 else None
        if r_value is not None and r_value > 0:
            current_price = float(getattr(position, "current_price", 0) or 0)
            if current_price > 0 and current_price < avg_entry + _TIME_STOP_R_FRACTION * r_value:
                res = _close_position_market(
                    client, symbol, position,
                    reason=(
                        f"time stop: {int(minutes_since_fill)} min since fill, "
                        f"price {current_price:.2f} < entry+0.25R "
                        f"{avg_entry + _TIME_STOP_R_FRACTION * r_value:.2f}"
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
