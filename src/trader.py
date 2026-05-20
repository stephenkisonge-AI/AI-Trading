"""Phase 5a — auto-execution layer.

Called by the watcher when a setup qualifies AND WATCHER_AUTO_EXECUTE=true.
Places a 4-order bundle:
    1. Market entry (BUY)
    2. Wait for fill
    3. Stop-limit SELL (full qty, GTC) at the strategy-defined stop
    4. TP1 limit SELL (50% qty, GTC) at +1.5R
    5. TP2 limit SELL (25% qty, GTC) at +3R
    (Remaining 25% has no resting order — trailed in Phase 5c.)

Safety contract:
    - Refuses to act unless ALPACA_PAPER_TRADE=True. Live trading is
      always manual (the strategy doc's confirmation gate stays on for
      live mode and only carves out for paper auto-execution).
    - Runs all pre-execution gates from Crypto Strategy.md §"Risk caps"
      before placing the entry. Failures return a SkipDecision with the
      reason; the watcher logs it silently into the scan summary.
    - Never raises out of `place_entry_bundle` — all errors are captured
      in the returned dict so the watcher can alert appropriately. A
      partial bundle (entry placed, some protective orders failed) is
      flagged via `protective_orders_complete=False` so the caller can
      escalate to an urgent Telegram alert.

Phase 5a scope (deferred to later phases):
    - Weekly loss cap and rolling equity-drawdown gates need state we
      don't yet persist — they're checked in Phase 5b once we add a
      lifecycle journal.
    - Spread/quote-staleness no-trade conditions need a latest-quote
      fetch we don't currently make — added in 5b alongside management.
    - Trailing stop on the final 25% is Phase 5c.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
)

from src.data import get_client


# --- Tunables (keep in sync with Crypto Strategy.md) ---------------------
_MAX_NOTIONAL_USD = 500.0
_MIN_NOTIONAL_USD = 50.0
_MAX_STOP_DIST_PCT = 0.08          # 8%
_RISK_PER_TRADE_PCT = 0.01         # 1% of equity
_MAX_POSITIONS = 2
_DAILY_LOSS_CAP_PCT = 0.02         # -2% from prior session equity
_STOP_LIMIT_SLIPPAGE_PCT = 0.005   # stop_limit = stop * (1 - 0.5%) for SELL
_FILL_POLL_TIMEOUT_SEC = 60
_FILL_POLL_INTERVAL_SEC = 1

# Per-symbol qty rounding. Conservative — Alpaca crypto allows more
# precision than this, but rounding down to these decimals avoids tiny
# rejections and keeps notionals at-or-under the budgeted amount.
_QTY_DECIMALS = {
    "BTC/USD": 6,
    "ETH/USD": 5,
    "SOL/USD": 4,
    "LINK/USD": 3,
    "AVAX/USD": 3,
}
_DEFAULT_QTY_DECIMALS = 4
_PRICE_DECIMALS = 2


@dataclass
class SkipDecision:
    allowed: bool
    reason: str


def auto_execute_enabled() -> bool:
    """True only if both env flags are set correctly. Live trading is
    never auto-executed regardless of WATCHER_AUTO_EXECUTE.
    """
    if os.environ.get("WATCHER_AUTO_EXECUTE", "").lower() != "true":
        return False
    if os.environ.get("ALPACA_PAPER_TRADE") != "True":
        return False
    return True


def _round_qty_down(symbol: str, qty: float) -> float:
    decimals = _QTY_DECIMALS.get(symbol, _DEFAULT_QTY_DECIMALS)
    factor = 10 ** decimals
    return math.floor(qty * factor) / factor


def _alpaca_position_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


def compute_position_size(equity: float, entry: float, stop: float) -> dict:
    """Implements Crypto Strategy.md §"Position sizing". Returns a dict
    with keys: qty (unrounded), notional, stop_dist_pct, skip_reason
    (None or a human-readable string explaining the skip).
    """
    stop_dist_pct = (entry - stop) / entry if entry > 0 else 0.0
    if stop_dist_pct <= 0:
        return {
            "qty": None, "notional": None, "stop_dist_pct": stop_dist_pct,
            "skip_reason": f"stop_dist non-positive ({stop_dist_pct:.4f})",
        }
    if stop_dist_pct > _MAX_STOP_DIST_PCT:
        return {
            "qty": None, "notional": None, "stop_dist_pct": stop_dist_pct,
            "skip_reason": (
                f"stop_dist {stop_dist_pct*100:.2f}% exceeds "
                f"{_MAX_STOP_DIST_PCT*100:.0f}% cap"
            ),
        }

    risk_dollars = equity * _RISK_PER_TRADE_PCT
    notional = min(risk_dollars / stop_dist_pct, _MAX_NOTIONAL_USD)
    if notional < _MIN_NOTIONAL_USD:
        return {
            "qty": None, "notional": notional, "stop_dist_pct": stop_dist_pct,
            "skip_reason": (
                f"notional ${notional:.2f} below ${_MIN_NOTIONAL_USD:.0f} minimum"
            ),
        }

    return {
        "qty": notional / entry,
        "notional": notional,
        "stop_dist_pct": stop_dist_pct,
        "skip_reason": None,
    }


def check_safety_gates(symbol: str) -> SkipDecision:
    """Pre-execution gates from Crypto Strategy.md §"Risk caps".

    Gates checked in Phase 5a:
        - WATCHER_AUTO_EXECUTE flag set (and paper mode)
        - Daily loss cap (equity vs last_equity)
        - Max simultaneous positions
        - Already holding this symbol (defense-in-depth; Setup cond 8 too)
        - BTC/ETH correlation rule (one slot, not two)
        - 1 entry per day cap

    Gates deferred to Phase 5b:
        - Weekly loss cap (needs week-start tracking)
        - Equity drawdown rolling -10% (needs peak tracking)
        - Spread > 0.5% (needs latest-quote fetch)
        - 60-minute >5% gap (needs minute bars)
    """
    if not auto_execute_enabled():
        return SkipDecision(
            False, "auto-execute disabled (WATCHER_AUTO_EXECUTE != 'true' or live mode)"
        )

    client = get_client()
    account = client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity)

    daily_pl_pct = (equity - last_equity) / last_equity if last_equity > 0 else 0.0
    if daily_pl_pct <= -_DAILY_LOSS_CAP_PCT:
        return SkipDecision(
            False, f"daily loss cap hit ({daily_pl_pct*100:.2f}% from prior equity)"
        )

    positions = client.get_all_positions()
    position_symbols = {getattr(p, "symbol", None) for p in positions}

    if len(positions) >= _MAX_POSITIONS:
        return SkipDecision(
            False, f"max positions reached ({len(positions)}/{_MAX_POSITIONS})"
        )

    if _alpaca_position_symbol(symbol) in position_symbols:
        return SkipDecision(False, f"already hold {symbol}")

    if symbol in ("BTC/USD", "ETH/USD"):
        other = "ETH/USD" if symbol == "BTC/USD" else "BTC/USD"
        if _alpaca_position_symbol(other) in position_symbols:
            return SkipDecision(
                False, f"BTC/ETH correlation rule (already hold {other})"
            )

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    recent = client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=today_start,
    ))
    def _is_filled_buy(o) -> bool:
        side = getattr(o, "side", None)
        side_str = str(side).lower()
        if side != OrderSide.BUY and not side_str.endswith("buy"):
            return False
        fq = getattr(o, "filled_qty", None)
        return fq is not None and float(fq) > 0

    placed_buy_today = any(_is_filled_buy(o) for o in recent)
    if placed_buy_today:
        return SkipDecision(False, "daily entry cap hit (1 entry already filled today)")

    return SkipDecision(True, "")


def _wait_for_fill(client, order_id: str) -> dict:
    """Poll until the order reaches FILLED, or raise on timeout/terminal."""
    for _ in range(_FILL_POLL_TIMEOUT_SEC // _FILL_POLL_INTERVAL_SEC):
        order = client.get_order_by_id(order_id)
        status = order.status
        if status == OrderStatus.FILLED:
            return {
                "filled_qty": float(order.filled_qty),
                "filled_avg_price": float(order.filled_avg_price),
            }
        if status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            raise RuntimeError(
                f"entry order {order_id} reached terminal {status} before filling"
            )
        time.sleep(_FILL_POLL_INTERVAL_SEC)
    raise RuntimeError(
        f"entry order {order_id} did not fill within {_FILL_POLL_TIMEOUT_SEC}s"
    )


def place_entry_bundle(
    symbol: str,
    entry_price_hint: float,
    stop_price: float,
    tp1_price: float,
    tp2_price: float,
) -> dict:
    """Place the entry + 3 protective orders. Never raises; returns a
    result dict for the watcher to interpret and alert on.

    `entry_price_hint` is the strategy's reference entry (typically the
    last closed 4H close) used for sizing only. The market order does
    not bind to that price; actual fill may slip slightly.
    """
    result = {
        "symbol": symbol,
        "qty_intended": None,
        "entry_filled_qty": None,
        "entry_filled_avg_price": None,
        "stop_order_id": None,
        "tp1_order_id": None,
        "tp2_order_id": None,
        "stop_price": stop_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "protective_orders_complete": False,
        "errors": [],
    }

    client = get_client()
    try:
        account = client.get_account()
    except Exception as exc:
        result["errors"].append(f"account fetch failed: {exc}")
        return result
    equity = float(account.equity)

    sizing = compute_position_size(equity, entry_price_hint, stop_price)
    if sizing["skip_reason"]:
        result["errors"].append(f"sizing: {sizing['skip_reason']}")
        return result

    qty = _round_qty_down(symbol, sizing["qty"])
    result["qty_intended"] = qty
    if qty <= 0:
        result["errors"].append(
            f"sizing: rounded qty is zero (raw {sizing['qty']:.8f})"
        )
        return result

    # 1. Market entry
    try:
        entry_order = client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
        ))
    except Exception as exc:
        result["errors"].append(f"entry submit failed: {exc}")
        return result

    # 2. Wait for fill
    try:
        fill = _wait_for_fill(client, entry_order.id)
    except Exception as exc:
        result["errors"].append(f"entry fill: {exc}")
        return result

    filled_qty = fill["filled_qty"]
    result["entry_filled_qty"] = filled_qty
    result["entry_filled_avg_price"] = fill["filled_avg_price"]

    # 3. Stop-limit SELL for full filled qty
    stop_px = round(stop_price, _PRICE_DECIMALS)
    stop_limit_px = round(stop_price * (1 - _STOP_LIMIT_SLIPPAGE_PCT), _PRICE_DECIMALS)
    try:
        stop_order = client.submit_order(StopLimitOrderRequest(
            symbol=symbol,
            qty=filled_qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_px,
            limit_price=stop_limit_px,
        ))
        result["stop_order_id"] = stop_order.id
    except Exception as exc:
        result["errors"].append(f"stop submit failed: {exc}")

    # 4. TP1 limit SELL, 50% of filled qty
    tp1_qty = _round_qty_down(symbol, filled_qty * 0.5)
    if tp1_qty > 0:
        try:
            tp1_order = client.submit_order(LimitOrderRequest(
                symbol=symbol,
                qty=tp1_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=round(tp1_price, _PRICE_DECIMALS),
            ))
            result["tp1_order_id"] = tp1_order.id
        except Exception as exc:
            result["errors"].append(f"tp1 submit failed: {exc}")
    else:
        result["errors"].append(f"tp1 skipped: qty rounds to zero (filled_qty={filled_qty})")

    # 5. TP2 limit SELL, 25% of filled qty
    tp2_qty = _round_qty_down(symbol, filled_qty * 0.25)
    if tp2_qty > 0:
        try:
            tp2_order = client.submit_order(LimitOrderRequest(
                symbol=symbol,
                qty=tp2_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=round(tp2_price, _PRICE_DECIMALS),
            ))
            result["tp2_order_id"] = tp2_order.id
        except Exception as exc:
            result["errors"].append(f"tp2 submit failed: {exc}")
    else:
        result["errors"].append(f"tp2 skipped: qty rounds to zero (filled_qty={filled_qty})")

    result["protective_orders_complete"] = (
        result["stop_order_id"] is not None
        and result["tp1_order_id"] is not None
        and result["tp2_order_id"] is not None
    )
    return result
