"""Phase D5a — auto-execution layer for the day-trade strategy.

Called by the day-watcher when a setup qualifies AND
WATCHER_DAY_AUTO_EXECUTE=true. Places a 4-order bundle:
    1. Market entry (BUY)
    2. Wait for fill
    3. Stop-market SELL (full qty, DAY) at the strategy-defined stop
    4. TP1 limit SELL (50% qty, DAY) at +1R
    5. TP2 limit SELL (50% qty, DAY) at +2R

Safety contract:
    - Refuses to act unless ALPACA_PAPER_TRADE=True. Live trading is
      always manual (the doc's two-switch promise).
    - Runs all pre-execution gates from Day_Trading_Strategy.md §"Risk
      caps" before placing the entry. Failures return a SkipDecision
      with the reason; the watcher logs it.
    - Never raises out of `place_entry_bundle` — partial-bundle
      failures are captured in the returned dict with
      `protective_orders_complete=False` so the caller can fire an
      urgent Telegram alert.

Phase D5a scope (deferred to later sub-phases):
    - Weekly loss cap and consecutive-loss cooldown need realized-P&L
      reconstruction — they land in D5c alongside lifecycle stats.
    - In-trade management (TP1 → breakeven, time stop, hard exits) is
      D5b.
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
