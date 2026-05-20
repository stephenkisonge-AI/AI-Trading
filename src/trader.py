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
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    GetPortfolioHistoryRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
)

from src.data import get_bars, get_client, get_latest_quote
from src.strategy import classify_regime


# --- Tunables (keep in sync with Crypto Strategy.md) ---------------------
_MAX_NOTIONAL_USD = 500.0
_MIN_NOTIONAL_USD = 50.0
_MAX_STOP_DIST_PCT = 0.08          # 8%
_RISK_PER_TRADE_PCT = 0.01         # 1% of equity
_MAX_POSITIONS = 2
_DAILY_LOSS_CAP_PCT = 0.02         # -2% from prior session equity
_WEEKLY_LOSS_CAP_PCT = 0.05        # -5% from week-start equity
_ROLLING_DRAWDOWN_CAP_PCT = 0.10   # -10% from 30d equity peak
_SPREAD_CAP_PCT = 0.005            # 0.5% bid/ask spread
_TIME_STOP_DAYS = 10               # close at market if no TP1 within 10 days
_STOP_LIMIT_SLIPPAGE_PCT = 0.005   # stop_limit = stop * (1 - 0.5%) for SELL
_FILL_POLL_TIMEOUT_SEC = 60
_FILL_POLL_INTERVAL_SEC = 1
_ENTRY_LOOKBACK_DAYS = 30          # how far back to search for the position-opening BUY

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


def _slashed(symbol_no_slash: str) -> str:
    """Convert Alpaca's concatenated symbol back to slash form (BTCUSD -> BTC/USD).
    All our universe pairs are vs USD so the rule is simple.
    """
    if symbol_no_slash.endswith("USD") and len(symbol_no_slash) > 3:
        return f"{symbol_no_slash[:-3]}/USD"
    return symbol_no_slash


def _side_is(order, target: str) -> bool:
    """Robust side check that works whether alpaca-py returns an enum or string."""
    side = getattr(order, "side", None)
    target = target.lower()
    if hasattr(side, "value"):
        return str(side.value).lower() == target
    return str(side).lower().endswith(target)


def _order_type_str(order) -> str:
    ot = getattr(order, "order_type", None) or getattr(order, "type", None)
    if hasattr(ot, "value"):
        return str(ot.value).lower()
    return str(ot).lower()


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


def _gate_daily_loss_cap(client) -> Optional[SkipDecision]:
    account = client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity)
    daily_pl_pct = (equity - last_equity) / last_equity if last_equity > 0 else 0.0
    if daily_pl_pct <= -_DAILY_LOSS_CAP_PCT:
        return SkipDecision(
            False, f"daily loss cap hit ({daily_pl_pct*100:.2f}% from prior equity)"
        )
    return None


def _gate_position_caps(client, symbol: str) -> Optional[SkipDecision]:
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
    return None


def _gate_daily_entry_cap(client) -> Optional[SkipDecision]:
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    recent = client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=today_start,
    ))
    for o in recent:
        if not _side_is(o, "buy"):
            continue
        fq = getattr(o, "filled_qty", None)
        if fq is not None and float(fq) > 0:
            return SkipDecision(False, "daily entry cap hit (1 entry already filled today)")
    return None


def _gate_spread(symbol: str) -> Optional[SkipDecision]:
    try:
        quote = get_latest_quote(symbol)
        bid = float(quote.bid_price)
        ask = float(quote.ask_price)
    except Exception as exc:
        print(f"[trader] spread gate: quote fetch failed for {symbol}: {exc}",
              file=sys.stderr)
        return None  # infra failure -> don't block; logged for debugging
    if ask <= 0 or bid <= 0:
        return SkipDecision(False, f"invalid quote (bid={bid}, ask={ask})")
    spread_pct = (ask - bid) / ask
    if spread_pct > _SPREAD_CAP_PCT:
        return SkipDecision(
            False,
            f"spread {spread_pct*100:.3f}% exceeds {_SPREAD_CAP_PCT*100:.1f}% cap "
            f"(bid={bid} ask={ask})",
        )
    return None


def _gate_weekly_loss_cap(client) -> Optional[SkipDecision]:
    try:
        hist = client.get_portfolio_history(filter=GetPortfolioHistoryRequest(
            period="1W", timeframe="1D"
        ))
        equities = [e for e in (hist.equity or []) if e is not None and e > 0]
    except Exception as exc:
        print(f"[trader] weekly cap: portfolio history failed: {exc}", file=sys.stderr)
        return None
    if len(equities) < 2:
        return None
    week_start = equities[0]
    current = equities[-1]
    weekly_pl = (current - week_start) / week_start
    if weekly_pl <= -_WEEKLY_LOSS_CAP_PCT:
        return SkipDecision(
            False, f"weekly loss cap hit ({weekly_pl*100:.2f}% from week start)"
        )
    return None


def _gate_rolling_drawdown(client) -> Optional[SkipDecision]:
    try:
        hist = client.get_portfolio_history(filter=GetPortfolioHistoryRequest(
            period="1M", timeframe="1D"
        ))
        equities = [e for e in (hist.equity or []) if e is not None and e > 0]
    except Exception as exc:
        print(f"[trader] drawdown gate: portfolio history failed: {exc}", file=sys.stderr)
        return None
    if len(equities) < 2:
        return None
    peak = max(equities)
    current = equities[-1]
    drawdown = (current - peak) / peak
    if drawdown <= -_ROLLING_DRAWDOWN_CAP_PCT:
        return SkipDecision(
            False,
            f"30d rolling drawdown {drawdown*100:.2f}% exceeds "
            f"-{_ROLLING_DRAWDOWN_CAP_PCT*100:.0f}% cap "
            f"(peak={peak:.2f} current={current:.2f})",
        )
    return None


def check_safety_gates(symbol: str) -> SkipDecision:
    """Pre-execution gates from Crypto Strategy.md §"Risk caps".

    Composes sub-gates; first failure determines the skip reason.
    Infra failures (quote fetch, portfolio history) log to stderr and
    don't block — they surface as real failures during execution or
    are caught next scan.

    Phase 5a gates: daily loss cap, position caps, daily entry cap.
    Phase 5b additions: spread cap, weekly loss cap, rolling drawdown.
    """
    if not auto_execute_enabled():
        return SkipDecision(
            False, "auto-execute disabled (WATCHER_AUTO_EXECUTE != 'true' or live mode)"
        )

    client = get_client()
    sub_gates = [
        lambda: _gate_daily_loss_cap(client),
        lambda: _gate_weekly_loss_cap(client),
        lambda: _gate_rolling_drawdown(client),
        lambda: _gate_position_caps(client, symbol),
        lambda: _gate_daily_entry_cap(client),
        lambda: _gate_spread(symbol),
    ]
    for gate_fn in sub_gates:
        decision = gate_fn()
        if decision is not None and not decision.allowed:
            return decision
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


# =========================================================================
# Phase 5b — in-trade management
# =========================================================================
#
# Runs at the top of every scan (before the entry pass) so closed positions
# are out of the way when entry safety gates evaluate "max positions".
#
# For each open position, in priority order:
#   1. Regime exit — if the symbol's daily regime classified BEARISH,
#      cancel all open orders for the symbol and close the position at
#      market. (We interpret the strategy doc's "if daily regime flips
#      bearish" as per-symbol — closing BTC doesn't auto-close ETH.)
#   2. Time stop — if the position is >10 days old AND TP1 has not filled,
#      cancel open orders and close at market.
#   3. Breakeven move — if TP1 has filled but the open stop is still at
#      its original loss-side level (stop_price < avg_entry_price), cancel
#      the old stop and place a new stop-limit at avg_entry_price for the
#      remaining qty.


def _find_entry_fill_time(client, symbol_no_slash: str) -> Optional[datetime]:
    """Most recent filled BUY for this symbol within the lookback window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_ENTRY_LOOKBACK_DAYS)
    orders = client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        symbols=[symbol_no_slash],
        after=cutoff,
    ))
    filled_buys = []
    for o in orders:
        if not _side_is(o, "buy"):
            continue
        fq = getattr(o, "filled_qty", None)
        if fq is None or float(fq) <= 0:
            continue
        ts = getattr(o, "filled_at", None) or getattr(o, "updated_at", None)
        if ts is None:
            continue
        filled_buys.append((ts, o))
    if not filled_buys:
        return None
    filled_buys.sort(key=lambda pair: pair[0], reverse=True)
    return filled_buys[0][0]


def _detect_tp1_filled(client, symbol_no_slash: str, since: datetime) -> bool:
    """Any filled limit SELL (excluding stop-limit) since `since`."""
    orders = client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        symbols=[symbol_no_slash],
        after=since,
    ))
    for o in orders:
        if not _side_is(o, "sell"):
            continue
        otype = _order_type_str(o)
        if "limit" not in otype or "stop" in otype:
            continue
        fq = getattr(o, "filled_qty", None)
        if fq is not None and float(fq) > 0:
            return True
    return False


def _find_open_stop_orders(client, symbol_no_slash: str):
    orders = client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN,
        symbols=[symbol_no_slash],
    ))
    return [o for o in orders if "stop" in _order_type_str(o)]


def _cancel_open_orders(client, symbol_no_slash: str) -> list[str]:
    """Cancel every open order for a symbol. Returns list of cancelled order IDs.
    Failures are swallowed (printed) — the caller will discover unsold
    quantities when the close order is placed.
    """
    orders = client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.OPEN,
        symbols=[symbol_no_slash],
    ))
    cancelled: list[str] = []
    for o in orders:
        try:
            client.cancel_order_by_id(o.id)
            cancelled.append(str(o.id))
        except Exception as exc:
            print(f"[trader] cancel {o.id} failed: {exc}", file=sys.stderr)
    return cancelled


def _close_position_market(client, symbol: str, position, reason: str) -> dict:
    """Cancel all open orders for the symbol, then market-sell the position."""
    symbol_no_slash = _alpaca_position_symbol(symbol)
    cancelled = _cancel_open_orders(client, symbol_no_slash)
    qty = float(position.qty)
    out: dict = {
        "reason": reason,
        "cancelled_orders": cancelled,
        "close_order_id": None,
        "qty": qty,
        "error": None,
    }
    try:
        close_order = client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
        ))
        out["close_order_id"] = str(close_order.id)
    except Exception as exc:
        out["error"] = f"close submit failed: {exc}"
    return out


def _move_stop_to_breakeven(client, symbol: str, position, old_stop) -> dict:
    """Cancel the original stop and place a new stop-limit at breakeven
    (position.avg_entry_price) for the remaining qty.
    """
    avg_entry = float(position.avg_entry_price)
    qty = float(position.qty)
    out: dict = {
        "old_stop_order_id": str(old_stop.id),
        "old_stop_price": float(getattr(old_stop, "stop_price", 0) or 0),
        "new_stop_order_id": None,
        "stop_price": round(avg_entry, _PRICE_DECIMALS),
        "qty": qty,
        "success": False,
        "error": None,
    }
    try:
        client.cancel_order_by_id(old_stop.id)
    except Exception as exc:
        out["error"] = f"cancel old stop failed: {exc}"
        return out

    stop_px = round(avg_entry, _PRICE_DECIMALS)
    limit_px = round(avg_entry * (1 - _STOP_LIMIT_SLIPPAGE_PCT), _PRICE_DECIMALS)
    try:
        new_stop = client.submit_order(StopLimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_px,
            limit_price=limit_px,
        ))
        out["new_stop_order_id"] = str(new_stop.id)
        out["success"] = True
    except Exception as exc:
        out["error"] = (
            f"new stop submit failed: {exc} "
            f"(OLD STOP CANCELLED — POSITION TEMPORARILY UNPROTECTED)"
        )
    return out


def manage_open_positions() -> list[dict]:
    """Run the in-trade management rules over all open positions.

    Returns a list of action dicts. Each dict has at minimum:
        {"action": <str>, "symbol": <slashed symbol>, ...}
    Possible actions: "regime_close", "time_stop", "breakeven_move",
    "error".

    No-ops (nothing to do for this position) produce no entry.
    """
    actions: list[dict] = []
    if not auto_execute_enabled():
        return actions

    client = get_client()
    try:
        positions = client.get_all_positions()
    except Exception as exc:
        actions.append({"action": "error", "symbol": "*", "error": f"position fetch: {exc}"})
        return actions

    for position in positions:
        symbol_no_slash = getattr(position, "symbol", "")
        symbol = _slashed(symbol_no_slash)
        try:
            # Priority 1 — regime exit
            try:
                daily = get_bars(symbol, "1Day", limit=250)
                daily_closed = daily.iloc[:-1] if len(daily) > 0 else daily
                regime = classify_regime(daily_closed)
            except Exception as exc:
                regime = None
                print(f"[trader] regime check failed for {symbol}: {exc}",
                      file=sys.stderr)
            if regime == "BEARISH":
                result = _close_position_market(
                    client, symbol, position,
                    reason="daily regime flipped BEARISH"
                )
                actions.append({"action": "regime_close", "symbol": symbol,
                                "regime": regime, **result})
                continue

            # Priority 2 — time stop
            entry_at = _find_entry_fill_time(client, symbol_no_slash)
            tp1_filled = (
                _detect_tp1_filled(client, symbol_no_slash, entry_at)
                if entry_at is not None else False
            )
            if entry_at is not None:
                age_days = (datetime.now(timezone.utc) - entry_at).total_seconds() / 86400
                if age_days > _TIME_STOP_DAYS and not tp1_filled:
                    result = _close_position_market(
                        client, symbol, position,
                        reason=f"time stop (age {age_days:.1f}d, TP1 not hit)"
                    )
                    actions.append({"action": "time_stop", "symbol": symbol,
                                    "age_days": age_days, **result})
                    continue

            # Priority 3 — TP1-fired → breakeven move
            if tp1_filled:
                stop_orders = _find_open_stop_orders(client, symbol_no_slash)
                avg_entry = float(position.avg_entry_price)
                for stop in stop_orders:
                    sp = float(getattr(stop, "stop_price", 0) or 0)
                    if sp > 0 and sp < avg_entry:
                        be = _move_stop_to_breakeven(client, symbol, position, stop)
                        actions.append({"action": "breakeven_move", "symbol": symbol,
                                        "avg_entry_price": avg_entry, **be})
                        break
        except Exception as exc:
            actions.append({"action": "error", "symbol": symbol, "error": str(exc)})

    return actions
