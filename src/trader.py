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

import pandas as pd

from src.data import get_bars, get_client, get_latest_quote
from src.indicators import add_indicators
from src.strategy import classify_regime
from src.universe import CRYPTO_SYMBOLS_NO_SLASH


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
_TRAIL_ATR_MULT = 2.0              # runner-phase trail: HWM - 2x 4H ATR(14)
_LIFECYCLE_LOOKBACK_DAYS = 90      # window for lifecycle stats in scan summary
_EXPECTANCY_MIN_R = 0.2            # below this mean R after 30 trades = stop experiment

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
        # The day-trade strand shares this paper account — its equity BUYs
        # must not consume the crypto strand's 1-entry/day budget.
        sym = str(getattr(o, "symbol", "") or "").replace("/", "")
        if sym not in CRYPTO_SYMBOLS_NO_SLASH:
            continue
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


def _get_tp_fill_events(client, symbol_no_slash: str, since: datetime) -> list[dict]:
    """Filled non-stop limit SELLs since `since`, sorted chronologically.
    Each event: {"filled_at": datetime, "qty": float, "price": float}.

    A non-empty list means TP1 has fired; len>=2 means TP2 has also fired
    (runner phase).
    """
    orders = client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        symbols=[symbol_no_slash],
        after=since,
    ))
    events: list[dict] = []
    for o in orders:
        if not _side_is(o, "sell"):
            continue
        otype = _order_type_str(o)
        if "limit" not in otype or "stop" in otype:
            continue
        fq = getattr(o, "filled_qty", None)
        if fq is None or float(fq) <= 0:
            continue
        ts = getattr(o, "filled_at", None) or getattr(o, "updated_at", None)
        events.append({
            "filled_at": ts,
            "qty": float(fq),
            "price": float(getattr(o, "filled_avg_price", 0) or 0),
        })
    events.sort(key=lambda e: e["filled_at"] or datetime.min.replace(tzinfo=timezone.utc))
    return events


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


def _replace_stop(client, symbol: str, position, old_stop, new_stop_price: float) -> dict:
    """Cancel `old_stop` and place a new stop-limit at `new_stop_price`
    for the position's current qty. Shared by breakeven moves and trail
    raises.
    """
    qty = float(position.qty)
    stop_px = round(new_stop_price, _PRICE_DECIMALS)
    limit_px = round(new_stop_price * (1 - _STOP_LIMIT_SLIPPAGE_PCT), _PRICE_DECIMALS)
    out: dict = {
        "old_stop_order_id": str(old_stop.id),
        "old_stop_price": float(getattr(old_stop, "stop_price", 0) or 0),
        "new_stop_order_id": None,
        "stop_price": stop_px,
        "qty": qty,
        "success": False,
        "error": None,
    }
    try:
        client.cancel_order_by_id(old_stop.id)
    except Exception as exc:
        out["error"] = f"cancel old stop failed: {exc}"
        return out

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


def _move_stop_to_breakeven(client, symbol: str, position, old_stop) -> dict:
    """Convenience wrapper: replace stop with one at position.avg_entry_price."""
    avg_entry = float(position.avg_entry_price)
    return _replace_stop(client, symbol, position, old_stop, avg_entry)


def _runner_phase_action(client, symbol: str, position, tp_fill_events: list[dict]) -> Optional[dict]:
    """Phase 2 (runner) management — applies once TP2 has filled.

    Two parallel triggers per Crypto Strategy.md §"Stops, targets...":
      A. Exit at market if the latest CLOSED 4H bar's close < 4H EMA20.
      B. Otherwise raise the trail stop to HWM - 2 × ATR(14), where HWM
         is the max 4H high since TP2 fill. Never lower the stop.
    """
    try:
        h4_raw = get_bars(symbol, "4Hour", limit=250)
    except Exception as exc:
        return {"action": "error", "symbol": symbol, "error": f"4H bars fetch: {exc}"}
    if len(h4_raw) < 30:
        return None
    h4 = add_indicators(h4_raw.iloc[:-1].copy())  # drop in-progress
    last_bar = h4.iloc[-1]
    close = float(last_bar["close"])
    ema20 = float(last_bar["ema20"]) if pd.notna(last_bar["ema20"]) else None
    atr14 = float(last_bar["atr14"]) if pd.notna(last_bar["atr14"]) else None

    # Trigger A — 4H close < EMA20 exits the runner
    if ema20 is not None and close < ema20:
        result = _close_position_market(
            client, symbol, position,
            reason=f"runner exit: 4H close {close:.4f} < EMA20 {ema20:.4f}",
        )
        return {"action": "runner_exit", "symbol": symbol,
                "trigger": "4H close < EMA20",
                "h4_close": close, "h4_ema20": ema20, **result}

    # Trigger B — raise trail
    if atr14 is None:
        return None
    tp_times = sorted(e["filled_at"] for e in tp_fill_events if e.get("filled_at"))
    if len(tp_times) < 2:
        return None
    tp2_time = tp_times[-1]
    try:
        bars_since = h4[h4.index >= tp2_time]
    except TypeError:
        return None  # timezone mismatch — bail rather than misbehave
    if bars_since.empty:
        return None
    hwm = float(bars_since["high"].max())
    new_trail = hwm - _TRAIL_ATR_MULT * atr14

    stop_orders = _find_open_stop_orders(client, _alpaca_position_symbol(symbol))
    if not stop_orders:
        return {"action": "error", "symbol": symbol,
                "error": f"runner has no open stop order; calculated trail {new_trail:.4f} — INTERVENE"}
    current_stop = stop_orders[0]
    current_stop_px = float(getattr(current_stop, "stop_price", 0) or 0)
    if new_trail <= current_stop_px:
        return None  # never lower

    rep = _replace_stop(client, symbol, position, current_stop, new_trail)
    return {"action": "trail_raise", "symbol": symbol,
            "hwm": hwm, "atr14": atr14, **rep}


def manage_open_positions() -> list[dict]:
    """Run the in-trade management rules over all open positions.

    Returns a list of action dicts. Each dict has at minimum:
        {"action": <str>, "symbol": <slashed symbol>, ...}
    Possible actions: "regime_close", "time_stop", "breakeven_move",
    "trail_raise", "runner_exit", "error".

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
        # Equity positions belong to the day-trade strand (same account) —
        # regime exits / time stops / breakeven moves must not touch them.
        if symbol_no_slash.replace("/", "") not in CRYPTO_SYMBOLS_NO_SLASH:
            continue
        symbol = _slashed(symbol_no_slash)
        try:
            # Priority 1 — regime exit (always applies)
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
                    reason="daily regime flipped BEARISH",
                )
                actions.append({"action": "regime_close", "symbol": symbol,
                                "regime": regime, **result})
                continue

            # Detect phase from TP fill history
            entry_at = _find_entry_fill_time(client, symbol_no_slash)
            tp_events = (
                _get_tp_fill_events(client, symbol_no_slash, entry_at)
                if entry_at is not None else []
            )
            tp_count = len(tp_events)

            if tp_count == 0:
                # Phase 0 — time stop
                if entry_at is not None:
                    age_days = (datetime.now(timezone.utc) - entry_at).total_seconds() / 86400
                    if age_days > _TIME_STOP_DAYS:
                        result = _close_position_market(
                            client, symbol, position,
                            reason=f"time stop (age {age_days:.1f}d, TP1 not hit)",
                        )
                        actions.append({"action": "time_stop", "symbol": symbol,
                                        "age_days": age_days, **result})
                continue

            if tp_count == 1:
                # Phase 1 — breakeven move (idempotent: skips if stop already moved)
                stop_orders = _find_open_stop_orders(client, symbol_no_slash)
                avg_entry = float(position.avg_entry_price)
                for stop in stop_orders:
                    sp = float(getattr(stop, "stop_price", 0) or 0)
                    if sp > 0 and sp < avg_entry:
                        be = _move_stop_to_breakeven(client, symbol, position, stop)
                        actions.append({"action": "breakeven_move", "symbol": symbol,
                                        "avg_entry_price": avg_entry, **be})
                        break
                continue

            # tp_count >= 2 → Phase 2 (runner)
            runner_action = _runner_phase_action(client, symbol, position, tp_events)
            if runner_action is not None:
                actions.append(runner_action)
        except Exception as exc:
            actions.append({"action": "error", "symbol": symbol, "error": str(exc)})

    return actions


# =========================================================================
# Phase 5c — lifecycle reconstruction & expectancy stats
# =========================================================================
#
# Stateless — every scan walks Alpaca's closed-orders history fresh and
# reconstructs trade objects. No journal file is persisted; the strategy
# doc's per-trade "lesson learned" notes are intentionally not automated
# (they require human judgment). Aggregate stats are appended to the scan
# summary so the math is visible on every run.


def _trade_walk_for_symbol(orders_sorted: list) -> list[dict]:
    """Walk a single symbol's CLOSED orders (chronological) and produce
    trade objects. A trade opens on filled BUY and closes when sold qty
    matches the entry qty (or when a new BUY arrives — defensive).
    """
    trades: list[dict] = []
    current: Optional[dict] = None
    for o in orders_sorted:
        otype = _order_type_str(o)
        fq = float(getattr(o, "filled_qty", 0) or 0)
        fp = float(getattr(o, "filled_avg_price", 0) or 0)
        stop_px = float(getattr(o, "stop_price", 0) or 0)

        if _side_is(o, "buy") and fq > 0:
            if current is not None:
                trades.append(current)
            current = {
                "symbol": getattr(o, "symbol", ""),
                "entry_at": getattr(o, "filled_at", None),
                "entry_qty": fq,
                "entry_price": fp,
                "qty_remaining": fq,
                "exits": [],
                "stops_seen": [],
            }
            continue

        if current is None:
            continue

        if "stop" in otype and _side_is(o, "sell"):
            if stop_px > 0:
                current["stops_seen"].append(stop_px)
            if fq > 0:
                current["exits"].append({"qty": fq, "price": fp, "reason": "STOP"})
                current["qty_remaining"] -= fq
        elif _side_is(o, "sell") and "limit" in otype:
            if fq > 0:
                current["exits"].append({"qty": fq, "price": fp, "reason": "TP"})
                current["qty_remaining"] -= fq
        elif _side_is(o, "sell") and fq > 0:
            current["exits"].append({"qty": fq, "price": fp, "reason": "MGMT_CLOSE"})
            current["qty_remaining"] -= fq

        if current["qty_remaining"] < current["entry_qty"] * 0.01:
            trades.append(current)
            current = None

    if current is not None:
        trades.append(current)
    return trades


def summarize_lifecycle(days_back: int = _LIFECYCLE_LOOKBACK_DAYS) -> dict:
    """Reconstruct closed trades from Alpaca order history and compute
    aggregate stats: total closed, win rate, realized P&L in USD, mean R
    (where the original stop is recoverable from order history).

    Stateless — runs each scan. Cheap (one orders fetch).
    """
    if not auto_execute_enabled():
        return {"enabled": False}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    client = get_client()
    try:
        closed_orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=cutoff,
            limit=500,
        ))
    except Exception as exc:
        print(f"[trader] lifecycle: order fetch failed: {exc}", file=sys.stderr)
        return {"enabled": True, "error": str(exc), "days_back": days_back}

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
        "enabled": True,
        "days_back": days_back,
        "total_closed": len(closed),
        "open_trades": open_trades,
        "wins": 0,
        "losses": 0,
        "total_pl_usd": 0.0,
        "win_rate": None,
        "mean_r": None,
        "best_r": None,
        "worst_r": None,
        "expectancy_warning": None,
    }
    r_multiples: list[float] = []
    for t in closed:
        entry_value = t["entry_qty"] * t["entry_price"]
        exit_value = sum(e["qty"] * e["price"] for e in t["exits"])
        pl = exit_value - entry_value
        stats["total_pl_usd"] += pl
        if pl > 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        if t["stops_seen"]:
            # Stops never widen by strategy rule, so min() is the original.
            orig_stop = min(t["stops_seen"])
            risk_per_unit = t["entry_price"] - orig_stop
            if risk_per_unit > 0:
                r_multiples.append(pl / (t["entry_qty"] * risk_per_unit))

    if stats["total_closed"] > 0:
        stats["win_rate"] = stats["wins"] / stats["total_closed"]
    if r_multiples:
        stats["mean_r"] = sum(r_multiples) / len(r_multiples)
        stats["best_r"] = max(r_multiples)
        stats["worst_r"] = min(r_multiples)
        if stats["total_closed"] >= 30 and stats["mean_r"] < _EXPECTANCY_MIN_R:
            stats["expectancy_warning"] = (
                f"mean R {stats['mean_r']:+.2f} below +{_EXPECTANCY_MIN_R}R after "
                f"{stats['total_closed']} trades — strategy doc says STOP the experiment"
            )
    return stats
