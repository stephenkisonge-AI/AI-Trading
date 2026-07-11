"""Phase 3 — crypto swing exit lifecycle (journal-driven).

Replaces the old simultaneous exit bundle (stop 100% + TP1 50% + TP2
25% resting at once, up to 175% of the position in open sells) with:

    * ONE broker-held protective stop-limit for exactly the current
      position quantity, and
    * application-managed take profits: TP1/TP2 are persisted as
      intended levels only; when a fresh executable bid reaches a
      level, the stop is cancelled (confirmed terminal), the tranche is
      sold at market, and a replacement stop is placed for exactly the
      remaining quantity.

Required invariant, checked after every transition:

    sum(remaining qty of open SELL orders for the symbol)
        <= current broker position quantity

The gap watchdog runs on every management pass: if price has gapped to
or below the stop trigger while the stop-limit sits unfilled, the stop
is cancelled (confirmed) and the exact remainder is closed at market.
The watchdog is application-dependent — it is NOT equivalent to a
broker-held stop-market order and must never be described as such.

All broker access goes through the Phase 2 execution contract
(persist intent -> submit -> verify), and Alpaca paper state is the
source of truth reconciled against the journal (rule 20). Unknown
states never guess: they mark the trade RECOVERY_REQUIRED, freeze new
entries, and emit a CRITICAL alert.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
)

from src.execution import (
    ExecConfig,
    make_client_order_id,
    make_trade_id,
    next_leg,
    order_avg_price,
    order_filled_qty,
    order_status_str,
    persist_submit_verify,
    resolve_entry_fill,
    resolve_unresolved_intent,
    unwind_trade,
    validate_entry_fill,
    wait_for_terminal,
    FILLABLE_STATUSES,
)
from src.journal import TradeView
from src.trader import (
    _MAX_NOTIONAL_USD,
    _MIN_NOTIONAL_USD,
    _QTY_DECIMALS,
    _RISK_PER_TRADE_PCT,
    _TIME_STOP_DAYS,
    _TRAIL_ATR_MULT,
    _round_qty_down,
    compute_position_size,
)

STRAND = "swing"
TP1_R = 1.5
TP2_R = 3.0
TP1_FRACTION = 0.5
TP2_FRACTION = 0.25
_PRICE_DECIMALS = 2
QTY_TOLERANCE = 1e-9
# A quote older than this cannot trigger a TP transition or the gap
# watchdog — a stale observation is not an executable one.
MAX_QUOTE_AGE_SEC = 120.0


def min_qty_increment(symbol: str) -> float:
    return 10 ** -_QTY_DECIMALS.get(symbol, 4)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _position_qty(client, symbol_no_slash: str) -> Optional[float]:
    """Current broker position qty; 0.0 if flat; None if unknowable."""
    try:
        position = client.get_open_position(symbol_no_slash)
        return float(getattr(position, "qty", 0))
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404 or "not found" in str(exc).lower():
            return 0.0
        return None


def _avg_entry_price(view: TradeView) -> Optional[float]:
    return view.plan.get("actual_fill_price")


def check_sell_invariant(client, symbol_no_slash: str) -> tuple[Optional[bool], str]:
    """(ok, detail). ok=None when broker state could not be read."""
    qty = _position_qty(client, symbol_no_slash)
    if qty is None:
        return None, "position unreadable"
    try:
        open_orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol_no_slash]))
    except Exception as exc:
        return None, f"open orders unreadable: {exc}"
    total_remaining = 0.0
    for order in open_orders:
        side = getattr(order, "side", "")
        side = side.value if hasattr(side, "value") else str(side)
        if not str(side).lower().endswith("sell"):
            continue
        requested = float(getattr(order, "qty", 0) or 0)
        total_remaining += max(requested - order_filled_qty(order), 0.0)
    ok = total_remaining <= qty + QTY_TOLERANCE
    return ok, (f"open sell remaining {total_remaining:.10f} vs position "
                f"{qty:.10f}")


def _guard_invariant(client, journal, trade_id: str, symbol_no_slash: str,
                     alert_fn, moment: str) -> None:
    ok, detail = check_sell_invariant(client, symbol_no_slash)
    if ok is False:
        journal.record_error(trade_id, f"SELL INVARIANT VIOLATED {moment}: {detail}")
        journal.set_entry_freeze(True, f"sell invariant violated ({moment})")
        journal.record_recovery_required(trade_id,
                                         f"sell invariant violated {moment}")
        alert_fn(f"🚨 CRITICAL — sell-quantity invariant violated for "
                 f"{symbol_no_slash} {moment}: {detail}. Entries frozen.")


# =========================================================================
# Sizing — bounded by loss at the stop-LIMIT price, not just the trigger
# =========================================================================

def size_position_at_limit(equity: float, entry_hint: float,
                           structural_stop: float, symbol: str,
                           config: ExecConfig) -> dict:
    """Position size such that the loss modeled at the stop-limit price
    stays within approved risk. Reuses the strategy's structural checks
    (stop-distance cap, notional caps) then shrinks qty for the limit
    band. Returns {qty, approved_risk, stop_limit_price, skip_reason}.
    """
    base = compute_position_size(equity, entry_hint, structural_stop)
    out = {"qty": None, "approved_risk": None, "stop_limit_price": None,
           "skip_reason": base["skip_reason"]}
    if base["skip_reason"]:
        return out
    approved_risk = equity * _RISK_PER_TRADE_PCT
    stop_limit_price = round(structural_stop * (1 - config.stop_limit_offset_pct),
                             _PRICE_DECIMALS)
    risk_per_unit_at_limit = entry_hint - stop_limit_price
    if risk_per_unit_at_limit <= 0:
        out["skip_reason"] = "stop-limit price at/above entry"
        return out
    qty_at_limit = approved_risk / risk_per_unit_at_limit
    qty = _round_qty_down(symbol, min(base["qty"], qty_at_limit))
    if qty <= 0:
        out["skip_reason"] = "size rounds to zero"
        return out
    notional = qty * entry_hint
    if notional > _MAX_NOTIONAL_USD:
        qty = _round_qty_down(symbol, _MAX_NOTIONAL_USD / entry_hint)
        notional = qty * entry_hint
    if notional < _MIN_NOTIONAL_USD:
        out["skip_reason"] = (f"notional ${notional:.2f} below "
                              f"${_MIN_NOTIONAL_USD:.0f} minimum after "
                              f"limit-price sizing")
        return out
    out.update(qty=qty, approved_risk=approved_risk,
               stop_limit_price=stop_limit_price)
    return out


# =========================================================================
# Protective stop placement (place + verify accepted + verify quantity)
# =========================================================================

def _place_verified_stop(client, journal, *, trade_id: str, symbol: str,
                         setup: str, signal_ts, stop_price: float,
                         config: ExecConfig, alert_fn,
                         sleep_fn=time.sleep, clock_fn=time.monotonic) -> dict:
    """Place one stop-limit for EXACTLY the current position quantity and
    verify Alpaca accepted it with the right remaining quantity.

    Returns {ok, stop_order, qty, unverified_reason}. On failure the
    trade is marked recovery-required, entries freeze, and a CRITICAL
    alert fires — a position must never silently sit unprotected.
    """
    symbol_no_slash = symbol.replace("/", "")
    out = {"ok": False, "stop_order": None, "qty": None,
           "unverified_reason": None}

    qty = _position_qty(client, symbol_no_slash)
    if qty is None or qty <= 0:
        out["unverified_reason"] = f"position unreadable or flat (qty={qty})"
    else:
        stop_px = round(stop_price, _PRICE_DECIMALS)
        limit_px = round(stop_price * (1 - config.stop_limit_offset_pct),
                         _PRICE_DECIMALS)
        leg = next_leg(journal, trade_id, "stop")
        coid = make_client_order_id(STRAND, symbol, setup, signal_ts,
                                    "stop", leg)
        outcome = persist_submit_verify(
            client, journal, trade_id=trade_id,
            action_id=f"{trade_id}-stop-{leg}", client_order_id=coid,
            action="stop",
            intent={"requested_qty": qty, "stop_price": stop_px,
                    "limit_price": limit_px},
            submit_fn=lambda: client.submit_order(StopLimitOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC, stop_price=stop_px,
                limit_price=limit_px, client_order_id=coid)),
            symbol_no_slash=symbol_no_slash)
        if not outcome.ok:
            out["unverified_reason"] = f"stop submission {outcome.status}"
        else:
            order = outcome.order
            status = order_status_str(order)
            remaining = float(getattr(order, "qty", 0) or 0) - order_filled_qty(order)
            if status not in FILLABLE_STATUSES and status != "filled":
                out["unverified_reason"] = f"stop status '{status}' not live"
            elif abs(remaining - qty) > QTY_TOLERANCE and status != "filled":
                out["unverified_reason"] = (
                    f"stop remaining {remaining} != position {qty}")
            else:
                out.update(ok=True, stop_order=order, qty=qty)

    if not out["ok"]:
        journal.record_recovery_required(
            trade_id, f"protection unverified: {out['unverified_reason']}")
        journal.set_entry_freeze(
            True, f"unprotected position {symbol}: {out['unverified_reason']}")
        alert_fn(f"🚨 CRITICAL — could not verify protective stop for "
                 f"{symbol}: {out['unverified_reason']}. Entries frozen; "
                 f"trade {trade_id} needs reconciliation.")
    return out


# =========================================================================
# Entry — market entry, fill reconciliation, one verified stop, TP intents
# =========================================================================

def open_protected_trade(client, journal, *, symbol: str, setup: str,
                         signal_ts, planned_entry: float,
                         structural_stop: float, equity: float,
                         config: ExecConfig, alert_fn: Callable[[str], object],
                         sleep_fn=time.sleep,
                         clock_fn=time.monotonic) -> dict:
    """Full Phase 3 entry sequence. Returns a result dict with `status`:
    "protected" | "skipped" | "aborted" | "unwound" | "recovery_required".
    """
    trade_id = make_trade_id(STRAND, symbol, setup, signal_ts)
    symbol_no_slash = symbol.replace("/", "")
    result = {"trade_id": trade_id, "symbol": symbol, "status": "aborted",
              "detail": None, "filled_qty": None, "avg_fill_price": None,
              "stop_order_id": None, "tp1": None, "tp2": None}

    existing = journal.trades().get(trade_id)
    if existing is not None and not existing.is_terminal():
        result.update(status="skipped", detail="signal already journaled")
        return result

    sizing = size_position_at_limit(equity, planned_entry, structural_stop,
                                    symbol, config)
    if sizing["skip_reason"]:
        result.update(status="skipped", detail=f"sizing: {sizing['skip_reason']}")
        return result
    qty = sizing["qty"]
    approved_risk = sizing["approved_risk"]

    persisted = journal.record_trade_planned(
        trade_id, symbol=symbol, setup=setup,
        signal_bar_ts=str(signal_ts), planned_entry=planned_entry,
        structural_stop=structural_stop,
        stop_limit_price=sizing["stop_limit_price"],
        approved_risk_usd=approved_risk, planned_qty=qty,
        sizing_inputs={"equity": equity,
                       "risk_pct": _RISK_PER_TRADE_PCT,
                       "stop_limit_offset_pct": config.stop_limit_offset_pct})
    if not persisted:
        result.update(detail="journal unavailable — entry not submitted")
        return result

    # --- 1-2. market entry + fill reconciliation -------------------------
    coid = make_client_order_id(STRAND, symbol, setup, signal_ts, "entry", 0)
    outcome = persist_submit_verify(
        client, journal, trade_id=trade_id, action_id=f"{trade_id}-entry-0",
        client_order_id=coid, action="entry",
        intent={"requested_qty": qty, "planned_entry": planned_entry},
        submit_fn=lambda: client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC, client_order_id=coid)),
        symbol_no_slash=symbol_no_slash)
    if outcome.status in ("ambiguous", "intent_not_persisted", "submit_failed"):
        journal.record_state_transition(trade_id, "ABORTED",
                                        f"entry {outcome.status}")
        result.update(detail=f"entry {outcome.status}")
        return result

    fill = resolve_entry_fill(
        client, journal, trade_id=trade_id, action_id=f"{trade_id}-entry-0",
        order=outcome.order, client_order_id=coid, config=config,
        sleep_fn=sleep_fn, clock_fn=clock_fn)
    if fill.status == "unknown":
        journal.set_entry_freeze(True, f"entry fill unresolved for {trade_id}")
        alert_fn(f"🚨 CRITICAL — entry fill state for {symbol} could not be "
                 f"established. Entries frozen; reconcile before resuming.")
        result.update(status="recovery_required", detail="entry fill unknown")
        return result
    if fill.status == "none":
        journal.record_state_transition(trade_id, "ABORTED",
                                        "entry terminal with zero fill")
        result.update(detail="entry did not fill")
        return result

    filled_qty, avg_price = fill.filled_qty, fill.avg_price
    result.update(filled_qty=filled_qty, avg_fill_price=avg_price)

    # --- 3. recalculate risk / TP levels from the ACTUAL fill -------------
    validation = validate_entry_fill(
        planned_entry=planned_entry, structural_stop=structural_stop,
        approved_risk_dollars=approved_risk, filled_qty=filled_qty,
        avg_fill_price=avg_price, tp1_r=TP1_R, tp2_r=TP2_R,
        min_qty_increment=min_qty_increment(symbol), config=config,
        price_decimals=_PRICE_DECIMALS)
    if not validation.ok:
        unwind = unwind_trade(
            client, journal, trade_id=trade_id, symbol=symbol, strand=STRAND,
            setup=setup, signal_ts=signal_ts,
            reason="; ".join(validation.reasons), alert_fn=alert_fn,
            config=config, sleep_fn=sleep_fn, clock_fn=clock_fn)
        result.update(status="unwound" if unwind["closed"] else "recovery_required",
                      detail="; ".join(validation.reasons))
        return result

    # --- 8-9. persist TP levels as INTENDED levels only -------------------
    tp1_qty = _round_qty_down(symbol, filled_qty * TP1_FRACTION)
    tp2_qty = _round_qty_down(symbol, filled_qty * TP2_FRACTION)
    persisted = journal.record_trade_planned(
        trade_id, symbol=symbol, setup=setup,
        signal_bar_ts=str(signal_ts), planned_entry=planned_entry,
        structural_stop=structural_stop,
        stop_limit_price=validation.stop_limit_price,
        approved_risk_usd=approved_risk,
        actual_fill_qty=filled_qty, actual_fill_price=avg_price,
        actual_risk_per_unit=validation.actual_risk_per_unit,
        actual_risk_dollars=validation.actual_risk_dollars,
        tp1_price=validation.tp1, tp2_price=validation.tp2,
        tp1_qty=tp1_qty, tp2_qty=tp2_qty,
        entry_filled_at=_now_iso(),
        # No resting TP orders — these are application-managed levels.
        tp_management="application")
    if not persisted:
        journal.set_entry_freeze(True, f"plan persistence failed for {trade_id}")
        alert_fn(f"🚨 CRITICAL — {symbol} filled but the final plan could "
                 f"not be persisted. Entries frozen; reconcile.")
        result.update(status="recovery_required", detail="plan persist failed")
        return result

    # --- 4-7, 10-12. one broker-held stop, verified ----------------------
    placed = _place_verified_stop(
        client, journal, trade_id=trade_id, symbol=symbol, setup=setup,
        signal_ts=signal_ts, stop_price=structural_stop, config=config,
        alert_fn=alert_fn, sleep_fn=sleep_fn, clock_fn=clock_fn)
    if not placed["ok"]:
        result.update(status="recovery_required",
                      detail=placed["unverified_reason"])
        return result

    journal.record_state_transition(trade_id, "PROTECTED",
                                    "entry filled, stop verified")
    _guard_invariant(client, journal, trade_id, symbol_no_slash, alert_fn,
                     "after entry protection")
    result.update(status="protected",
                  stop_order_id=str(getattr(placed["stop_order"], "id", "")),
                  tp1=validation.tp1, tp2=validation.tp2)
    return result


# =========================================================================
# Quotes — a TP/watchdog trigger needs a FRESH executable observation
# =========================================================================

def fresh_bid(get_quote_fn, symbol: str,
              now_fn=lambda: datetime.now(timezone.utc),
              max_age_sec: float = MAX_QUOTE_AGE_SEC) -> Optional[dict]:
    """Current bid (the executable side for a long-position SELL) with
    its timestamp, or None when unavailable/stale. A None here must
    never trigger a TP transition — the protective stop stays."""
    try:
        quote = get_quote_fn(symbol)
        bid = float(quote.bid_price)
        quote_ts = getattr(quote, "timestamp", None)
    except Exception:
        return None
    if bid <= 0 or quote_ts is None:
        return None
    if getattr(quote_ts, "tzinfo", None) is None:
        quote_ts = quote_ts.replace(tzinfo=timezone.utc)
    age = (now_fn() - quote_ts).total_seconds()
    if age > max_age_sec or age < -max_age_sec:
        return None
    return {"bid": bid, "quote_ts": quote_ts.isoformat(), "age_sec": age}


# =========================================================================
# Management pass
# =========================================================================

def _latest_stop_action(view: TradeView) -> Optional[tuple[str, dict]]:
    """(action_id, action) of the most recent stop intent, or None."""
    stops = [(action_id, action) for action_id, action in view.actions.items()
             if (action.get("intent") or {}).get("action") == "stop"]
    if not stops:
        return None
    # Action IDs end in the leg number; the highest leg is the current stop.
    return max(stops, key=lambda pair: pair[0])


def _read_stop_order(client, view: TradeView) -> tuple[str, object]:
    """Fresh broker read of the trade's current stop order.

    Returns (kind, order):
      "none"    — the journal has no stop for this trade (order is None)
      "ok"      — order is the live broker read
      "unknown" — a stop exists in the journal but the broker could not
                  be read. Callers must NOT place a potentially
                  conflicting replacement — go recovery-required.
    """
    latest = _latest_stop_action(view)
    if latest is None:
        return "none", None
    action = latest[1]
    broker_order_id = ((action.get("submitted") or {}).get("broker_order_id")
                       or (action["states"][-1].get("broker_order_id")
                           if action["states"] else None))
    if not broker_order_id:
        # Intent persisted but never resolved — ambiguous by definition.
        return "unknown", None
    try:
        return "ok", client.get_order_by_id(broker_order_id)
    except Exception:
        return "unknown", None


def _realized_r(view: TradeView) -> Optional[float]:
    fill_price = view.plan.get("actual_fill_price")
    risk_per_unit = view.plan.get("actual_risk_per_unit")
    filled_qty = view.plan.get("actual_fill_qty")
    if not fill_price or not risk_per_unit or not filled_qty:
        return None
    pl = sum(float(e.get("qty") or 0) * (float(e.get("price") or 0) - fill_price)
             for e in view.exits if e.get("price"))
    return pl / (filled_qty * risk_per_unit)


def _close_trade_from_stop_fill(client, journal, view: TradeView,
                                stop_order, alert_fn) -> dict:
    """The broker-held stop (fully) filled — record the exit and close."""
    qty = order_filled_qty(stop_order)
    price = order_avg_price(stop_order)
    journal.record_exit(view.trade_id, qty=qty, price=price, reason="STOP")
    realized = _realized_r(journal.trades()[view.trade_id])
    journal.record_trade_closed(view.trade_id, realized_r=realized,
                                reason="protective stop filled")
    alert_fn(f"🛑 STOP FILLED — {view.symbol}: sold {qty} @ {price} "
             f"(realized {realized if realized is None else f'{realized:+.2f}'}R)")
    return {"action": "stop_filled", "symbol": view.symbol, "qty": qty,
            "price": price, "realized_r": realized}


def _cancel_stop_confirmed(client, journal, view: TradeView, stop_order,
                           config: ExecConfig, sleep_fn, clock_fn):
    """Cancel the protective stop and wait for a TERMINAL state.

    Returns (status, final_order):
      "canceled"  — confirmed terminal cancellation (may carry partial fill)
      "filled"    — the stop filled before the cancel took effect (race)
      "unknown"   — never terminalized; caller must go recovery-required
    """
    order_id = str(getattr(stop_order, "id", ""))
    try:
        client.cancel_order_by_id(order_id)
    except Exception as exc:
        journal.record_error(view.trade_id, f"stop cancel request failed: {exc}")
    reached_terminal, final = wait_for_terminal(
        client, order_id, config.cancel_confirm_timeout_sec,
        sleep_fn=sleep_fn, clock_fn=clock_fn)
    if not reached_terminal or final is None:
        return "unknown", final
    latest_stop = _latest_stop_action(view)
    if latest_stop is not None:
        journal.record_order_state(view.trade_id, latest_stop[0],
                                   broker_order_id=order_id,
                                   status=order_status_str(final),
                                   filled_qty=order_filled_qty(final),
                                   avg_fill_price=order_avg_price(final))
    if order_filled_qty(final) > 0:
        # Any fill that happened on the way out is a realized exit.
        journal.record_exit(view.trade_id, qty=order_filled_qty(final),
                            price=order_avg_price(final),
                            reason="STOP_PARTIAL_ON_CANCEL")
    if order_status_str(final) == "filled":
        return "filled", final
    return "canceled", final


def _market_sell_confirmed(client, journal, view: TradeView, *, qty: float,
                           action: str, reason: str, config: ExecConfig,
                           sleep_fn, clock_fn) -> dict:
    """Market-sell `qty` under the execution contract and resolve the
    fill. Returns {status, filled_qty, avg_price}."""
    symbol = view.symbol
    signal_ts = view.plan.get("signal_bar_ts") or view.trade_id.rsplit("-", 1)[-1]
    leg = next_leg(journal, view.trade_id, action)
    coid = make_client_order_id(STRAND, symbol, view.setup or "A",
                                _parse_ts(signal_ts), action, leg)
    outcome = persist_submit_verify(
        client, journal, trade_id=view.trade_id,
        action_id=f"{view.trade_id}-{action}-{leg}", client_order_id=coid,
        action=action, intent={"requested_qty": qty, "reason": reason},
        submit_fn=lambda: client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, client_order_id=coid)),
        symbol_no_slash=symbol.replace("/", ""))
    if not outcome.ok:
        return {"status": f"submit_{outcome.status}", "filled_qty": 0.0,
                "avg_price": None}
    fill = resolve_entry_fill(
        client, journal, trade_id=view.trade_id,
        action_id=f"{view.trade_id}-{action}-{leg}", order=outcome.order,
        client_order_id=coid, config=config, sleep_fn=sleep_fn,
        clock_fn=clock_fn)
    return {"status": fill.status, "filled_qty": fill.filled_qty,
            "avg_price": fill.avg_price}


def _parse_ts(value):
    """Journal timestamps come back as strings; IDs need datetimes."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc)
    return value


def _go_recovery(journal, view: TradeView, alert_fn, reason: str) -> dict:
    journal.record_recovery_required(view.trade_id, reason)
    journal.set_entry_freeze(True, f"{view.trade_id}: {reason}")
    alert_fn(f"🚨 CRITICAL — {view.symbol} trade {view.trade_id}: {reason}. "
             f"No further orders submitted; entries frozen.")
    return {"action": "recovery_required", "symbol": view.symbol,
            "reason": reason}


def _full_close(client, journal, view: TradeView, *, reason: str,
                critical: bool, config: ExecConfig, alert_fn, sleep_fn,
                clock_fn) -> dict:
    """Cancel the stop (confirmed), market-sell the exact remainder,
    confirm flat, close the trade. Shared by the gap watchdog, regime
    exit, time stop, and runner exit."""
    symbol_no_slash = view.symbol.replace("/", "")
    stop_kind, stop_order = _read_stop_order(client, view)
    if stop_kind == "unknown":
        return _go_recovery(journal, view, alert_fn,
                            f"stop state unknown during close ({reason})")
    if stop_order is not None and order_status_str(stop_order) in FILLABLE_STATUSES:
        status, final = _cancel_stop_confirmed(client, journal, view,
                                               stop_order, config,
                                               sleep_fn, clock_fn)
        if status == "unknown":
            return _go_recovery(journal, view, alert_fn,
                                f"stop cancel unresolved during close ({reason})")
        if status == "filled":
            return _close_trade_from_stop_fill(client, journal, view,
                                               final, alert_fn)

    remaining = _position_qty(client, symbol_no_slash)
    if remaining is None:
        return _go_recovery(journal, view, alert_fn,
                            f"position unreadable during close ({reason})")
    if remaining <= QTY_TOLERANCE:
        realized = _realized_r(journal.trades()[view.trade_id])
        journal.record_trade_closed(view.trade_id, realized_r=realized,
                                    reason=reason)
        return {"action": "closed", "symbol": view.symbol, "reason": reason,
                "realized_r": realized}

    sell = _market_sell_confirmed(client, journal, view, qty=remaining,
                                  action="close", reason=reason,
                                  config=config, sleep_fn=sleep_fn,
                                  clock_fn=clock_fn)
    if sell["status"] != "filled":
        return _go_recovery(journal, view, alert_fn,
                            f"close sell not confirmed ({sell['status']}) "
                            f"during {reason}")
    journal.record_exit(view.trade_id, qty=sell["filled_qty"],
                        price=sell["avg_price"], reason=reason)
    after = _position_qty(client, symbol_no_slash)
    if after is None or after > QTY_TOLERANCE:
        return _go_recovery(journal, view, alert_fn,
                            f"position not flat after close ({reason})")
    realized = _realized_r(journal.trades()[view.trade_id])
    journal.record_trade_closed(view.trade_id, realized_r=realized,
                                reason=reason)
    _guard_invariant(client, journal, view.trade_id, symbol_no_slash,
                     alert_fn, f"after {reason}")
    prefix = "🚨 CRITICAL — " if critical else ""
    alert_fn(f"{prefix}{view.symbol} closed at market ({reason}): sold "
             f"{sell['filled_qty']} @ {sell['avg_price']} "
             f"(realized {realized if realized is None else f'{realized:+.2f}'}R)")
    return {"action": "emergency_close" if critical else "mgmt_close",
            "symbol": view.symbol, "reason": reason,
            "qty": sell["filled_qty"], "price": sell["avg_price"],
            "realized_r": realized}


def _recovered_level_fill(view: TradeView, level: str) -> tuple[float, Optional[float]]:
    """(filled_qty, avg_price) already executed for this TP level —
    nonzero after a crash between the tranche fill and state persistence."""
    total, price = 0.0, None
    for action in view.actions.values():
        if (action.get("intent") or {}).get("action") != level:
            continue
        last = action["states"][-1] if action["states"] else None
        if last and float(last.get("filled_qty") or 0) > 0:
            total += float(last["filled_qty"])
            price = last.get("avg_fill_price") or price
    return total, price


def _execute_take_profit(client, journal, view: TradeView, *, level: str,
                         quote: dict, config: ExecConfig, alert_fn,
                         sleep_fn, clock_fn) -> dict:
    """The 15-step TP transition. `level` is "tp1" or "tp2"."""
    symbol = view.symbol
    symbol_no_slash = symbol.replace("/", "")
    plan = view.plan
    intended_qty = plan.get(f"{level}_qty") or 0.0
    tp_price = plan.get(f"{level}_price")

    # Crash recovery: if a previous run already executed this tranche
    # (found via its deterministic client order ID), repair the journal
    # state instead of selling the tranche a second time.
    recovered_qty, recovered_price = _recovered_level_fill(view, level)
    exit_recorded = any(e.get("reason") == level.upper() for e in view.exits)
    if recovered_qty > 0 and not exit_recorded:
        journal.record_exit(view.trade_id, qty=recovered_qty,
                            price=recovered_price,
                            reason=level.upper())
        journal.record_state_transition(
            view.trade_id, f"{level.upper()}_FILLED",
            f"state repaired after crash: tranche {recovered_qty} had "
            f"already filled")
        return {"action": f"{level}_state_repaired", "symbol": symbol,
                "qty": recovered_qty, "price": recovered_price}

    # 1. persist the TP execution intent (as a state transition marker —
    #    the sell order's own intent is persisted by the contract below).
    if not journal.record_state_transition(
            view.trade_id, f"{level.upper()}_TRIGGERED",
            f"bid {quote['bid']} >= {tp_price} (quote {quote['quote_ts']})"):
        return {"action": "error", "symbol": symbol,
                "error": f"{level} trigger could not be persisted — no action taken"}

    # 2. confirm the current broker position.
    position_qty = _position_qty(client, symbol_no_slash)
    if position_qty is None:
        return _go_recovery(journal, view, alert_fn,
                            f"position unreadable at {level} trigger")
    if position_qty <= QTY_TOLERANCE:
        return _go_recovery(journal, view, alert_fn,
                            f"no position at {level} trigger yet trade open")

    # 3. confirm the current protective stop.
    stop_kind, stop_order = _read_stop_order(client, view)
    if stop_kind != "ok":
        return _go_recovery(journal, view, alert_fn,
                            f"protective stop unreadable at {level} trigger")
    if order_status_str(stop_order) == "filled":
        return _close_trade_from_stop_fill(client, journal, view, stop_order,
                                           alert_fn)

    # 4-5. cancel the stop; poll to a confirmed terminal state.
    unprotected_started = clock_fn()
    status, final = _cancel_stop_confirmed(client, journal, view, stop_order,
                                           config, sleep_fn, clock_fn)
    if status == "unknown":
        return _go_recovery(journal, view, alert_fn,
                            f"stop cancel unresolved at {level} trigger")
    if status == "filled":
        return _close_trade_from_stop_fill(client, journal, view, final,
                                           alert_fn)

    # 6-7. re-read the position; tranche from remaining + journal state.
    remaining = _position_qty(client, symbol_no_slash)
    if remaining is None:
        return _go_recovery(journal, view, alert_fn,
                            f"position unreadable after stop cancel ({level})")
    if remaining <= QTY_TOLERANCE:
        realized = _realized_r(journal.trades()[view.trade_id])
        journal.record_trade_closed(view.trade_id, realized_r=realized,
                                    reason=f"flat after stop cancel at {level}")
        return {"action": "closed", "symbol": symbol,
                "reason": "flat after stop cancel", "realized_r": realized}
    tranche = min(_round_qty_down(symbol, intended_qty), remaining)
    leftover = remaining - tranche
    if leftover < min_qty_increment(symbol) or tranche <= 0:
        # Dust rule: never leave an unprotectable remainder.
        tranche = remaining

    # 8-9. market-sell the tranche; poll and reconcile the fill.
    sell = _market_sell_confirmed(client, journal, view, qty=tranche,
                                  action=level, reason=f"{level} trigger",
                                  config=config, sleep_fn=sleep_fn,
                                  clock_fn=clock_fn)
    if sell["status"] not in ("filled", "partial"):
        return _go_recovery(journal, view, alert_fn,
                            f"{level} sell not confirmed ({sell['status']}) "
                            f"while stop is cancelled")
    if sell["filled_qty"] > 0:
        journal.record_exit(view.trade_id, qty=sell["filled_qty"],
                            price=sell["avg_price"], reason=level.upper())

    # 10-13. re-read; replace protection for EXACTLY the remainder.
    after = _position_qty(client, symbol_no_slash)
    if after is None:
        return _go_recovery(journal, view, alert_fn,
                            f"position unreadable after {level} sell")
    if after <= QTY_TOLERANCE:
        realized = _realized_r(journal.trades()[view.trade_id])
        journal.record_trade_closed(view.trade_id, realized_r=realized,
                                    reason=f"position fully exited at {level}")
        journal.record_state_transition(view.trade_id, "CLOSED",
                                        f"fully exited at {level}")
        alert_fn(f"🎯 {level.upper()} — {symbol}: sold {sell['filled_qty']} @ "
                 f"{sell['avg_price']}; position fully closed.")
        return {"action": f"{level}_execute", "symbol": symbol,
                "qty": sell["filled_qty"], "price": sell["avg_price"],
                "position_closed": True}

    # Breakeven after TP1 (strategy rule); keep breakeven floor after TP2.
    avg_entry = _avg_entry_price(view) or plan.get("planned_entry")
    structural = plan.get("structural_stop")
    new_stop_price = max(structural or 0.0, avg_entry or 0.0)
    placed = _place_verified_stop(
        client, journal, trade_id=view.trade_id, symbol=symbol,
        setup=view.setup or "A",
        signal_ts=_parse_ts(plan.get("signal_bar_ts") or view.created_at),
        stop_price=new_stop_price, config=config, alert_fn=alert_fn,
        sleep_fn=sleep_fn, clock_fn=clock_fn)
    unprotected_seconds = clock_fn() - unprotected_started
    if not placed["ok"]:
        return {"action": "recovery_required", "symbol": symbol,
                "reason": placed["unverified_reason"],
                "unprotected_seconds": unprotected_seconds}

    # 14-15. persist transition + the unprotected window duration.
    journal.record_state_transition(
        view.trade_id, f"{level.upper()}_FILLED",
        f"tranche {sell['filled_qty']} @ {sell['avg_price']}; replacement "
        f"stop {new_stop_price} for {placed['qty']}; unprotected "
        f"{unprotected_seconds:.1f}s")
    _guard_invariant(client, journal, view.trade_id, symbol_no_slash,
                     alert_fn, f"after {level}")
    alert_fn(f"🎯 {level.upper()} — {symbol}: sold {sell['filled_qty']} @ "
             f"{sell['avg_price']}. Replacement stop {new_stop_price} covers "
             f"{placed['qty']} (unprotected {unprotected_seconds:.1f}s).")
    return {"action": f"{level}_execute", "symbol": symbol,
            "qty": sell["filled_qty"], "price": sell["avg_price"],
            "new_stop": new_stop_price,
            "unprotected_seconds": unprotected_seconds}


def _maybe_trail_runner(client, journal, view: TradeView, runner_ctx: dict,
                        config: ExecConfig, alert_fn, sleep_fn,
                        clock_fn) -> Optional[dict]:
    """After TP2: exit on 4H close < EMA20, else raise (never lower) the
    chandelier trail HWM - 2xATR(14)."""
    close = runner_ctx.get("close")
    ema20 = runner_ctx.get("ema20")
    atr14 = runner_ctx.get("atr14")
    hwm = runner_ctx.get("hwm")
    if close is not None and ema20 is not None and close < ema20:
        return _full_close(client, journal, view,
                           reason=f"runner exit: 4H close {close} < EMA20 {ema20}",
                           critical=False, config=config, alert_fn=alert_fn,
                           sleep_fn=sleep_fn, clock_fn=clock_fn)
    if atr14 is None or hwm is None:
        return None
    new_trail = hwm - _TRAIL_ATR_MULT * atr14
    stop_kind, stop_order = _read_stop_order(client, view)
    if stop_kind == "unknown":
        return _go_recovery(journal, view, alert_fn,
                            "stop state unknown during trail raise")
    if stop_order is None:
        # Runner genuinely without a stop — re-protect at the higher of
        # trail and breakeven.
        avg_entry = _avg_entry_price(view) or 0.0
        placed = _place_verified_stop(
            client, journal, trade_id=view.trade_id, symbol=view.symbol,
            setup=view.setup or "A",
            signal_ts=_parse_ts(view.plan.get("signal_bar_ts") or view.created_at),
            stop_price=max(new_trail, avg_entry), config=config,
            alert_fn=alert_fn, sleep_fn=sleep_fn, clock_fn=clock_fn)
        return ({"action": "reprotect", "symbol": view.symbol,
                 "stop": max(new_trail, avg_entry)} if placed["ok"] else
                {"action": "recovery_required", "symbol": view.symbol,
                 "reason": placed["unverified_reason"]})
    current_stop_px = float(getattr(stop_order, "stop_price", 0) or 0)
    if new_trail <= current_stop_px:
        return None
    unprotected_started = clock_fn()
    status, final = _cancel_stop_confirmed(client, journal, view, stop_order,
                                           config, sleep_fn, clock_fn)
    if status == "unknown":
        return _go_recovery(journal, view, alert_fn,
                            "stop cancel unresolved during trail raise")
    if status == "filled":
        return _close_trade_from_stop_fill(client, journal, view, final,
                                           alert_fn)
    placed = _place_verified_stop(
        client, journal, trade_id=view.trade_id, symbol=view.symbol,
        setup=view.setup or "A",
        signal_ts=_parse_ts(view.plan.get("signal_bar_ts") or view.created_at),
        stop_price=new_trail, config=config, alert_fn=alert_fn,
        sleep_fn=sleep_fn, clock_fn=clock_fn)
    unprotected_seconds = clock_fn() - unprotected_started
    if not placed["ok"]:
        return {"action": "recovery_required", "symbol": view.symbol,
                "reason": placed["unverified_reason"],
                "unprotected_seconds": unprotected_seconds}
    journal.record_state_transition(
        view.trade_id, "TP2_FILLED",
        f"trail raised to {new_trail} (unprotected {unprotected_seconds:.1f}s)")
    return {"action": "trail_raise", "symbol": view.symbol,
            "new_stop": new_trail,
            "unprotected_seconds": unprotected_seconds}


def manage_swing_trades(client, journal, *, config: ExecConfig,
                        alert_fn: Callable[[str], object],
                        get_quote_fn, regime_fn, runner_ctx_fn,
                        now_fn=lambda: datetime.now(timezone.utc),
                        sleep_fn=time.sleep,
                        clock_fn=time.monotonic) -> list[dict]:
    """One management pass over all nonterminal journal trades.

    Injected dependencies:
      get_quote_fn(symbol)        -> quote (bid_price, timestamp)
      regime_fn(symbol)           -> "BULLISH"/"BEARISH"/None (None = unknown)
      runner_ctx_fn(symbol, view) -> {"close","ema20","atr14","hwm"} or None

    Management NEVER creates new entry risk — it only protects, reduces,
    or closes. It keeps running while entries are frozen.
    """
    actions: list[dict] = []
    for trade_id, view in sorted(journal.open_trades().items()):
        symbol = view.symbol
        if not symbol:
            continue
        symbol_no_slash = symbol.replace("/", "")
        try:
            # 0. resolve crash windows before touching anything.
            for action_id in view.unresolved_intents():
                outcome = resolve_unresolved_intent(client, journal, trade_id,
                                                    action_id)
                actions.append({"action": "intent_recovery", "symbol": symbol,
                                "action_id": action_id, "outcome": outcome})
                if outcome == "unknown":
                    actions.append(_go_recovery(
                        journal, view, alert_fn,
                        f"unresolved intent {action_id} (broker unreachable)"))
                    raise StopIteration
            view = journal.trades()[trade_id]  # re-fold after recovery

            if view.recovery_required:
                actions.append({"action": "recovery_hold", "symbol": symbol,
                                "reason": view.recovery_reason})
                continue
            if view.state not in ("PROTECTED", "TP1_TRIGGERED", "TP1_FILLED",
                                  "TP2_TRIGGERED", "TP2_FILLED"):
                continue

            position_qty = _position_qty(client, symbol_no_slash)
            if position_qty is None:
                actions.append(_go_recovery(journal, view, alert_fn,
                                            "position unreadable in management"))
                continue

            stop_kind, stop_order = _read_stop_order(client, view)
            if stop_kind == "unknown":
                # Rule: unknown stop state — do not submit anything that
                # could conflict; recovery + freeze.
                actions.append(_go_recovery(journal, view, alert_fn,
                                            "stop state unknown in management"))
                continue
            stop_status = order_status_str(stop_order) if stop_order is not None else None

            if position_qty <= QTY_TOLERANCE:
                # Flat at the broker: the stop most likely filled.
                if stop_order is not None and stop_status == "filled":
                    actions.append(_close_trade_from_stop_fill(
                        client, journal, view, stop_order, alert_fn))
                elif stop_order is not None and stop_status in FILLABLE_STATUSES:
                    status, final = _cancel_stop_confirmed(
                        client, journal, view, stop_order, config, sleep_fn,
                        clock_fn)
                    if status == "unknown":
                        actions.append(_go_recovery(
                            journal, view, alert_fn,
                            "flat but resting stop cancel unresolved"))
                        continue
                    realized = _realized_r(journal.trades()[trade_id])
                    journal.record_trade_closed(trade_id, realized_r=realized,
                                                reason="flat at broker")
                    actions.append({"action": "closed", "symbol": symbol,
                                    "reason": "flat at broker",
                                    "realized_r": realized})
                else:
                    actions.append(_go_recovery(
                        journal, view, alert_fn,
                        "flat at broker with no stop fill recorded"))
                continue

            # Position exists but no live protection -> re-protect NOW.
            if stop_order is None or stop_status not in FILLABLE_STATUSES:
                if stop_order is not None and stop_status == "filled":
                    # Stop filled yet position remains (partial data race):
                    # record the exit, then re-protect the remainder.
                    journal.record_exit(trade_id,
                                        qty=order_filled_qty(stop_order),
                                        price=order_avg_price(stop_order),
                                        reason="STOP")
                avg_entry = _avg_entry_price(view) or 0.0
                structural = view.plan.get("structural_stop") or 0.0
                floor = structural if view.state == "PROTECTED" else max(
                    structural, avg_entry)
                placed = _place_verified_stop(
                    client, journal, trade_id=trade_id, symbol=symbol,
                    setup=view.setup or "A",
                    signal_ts=_parse_ts(view.plan.get("signal_bar_ts")
                                        or view.created_at),
                    stop_price=floor, config=config, alert_fn=alert_fn,
                    sleep_fn=sleep_fn, clock_fn=clock_fn)
                actions.append({"action": "reprotect", "symbol": symbol,
                                "ok": placed["ok"], "stop": floor})
                if not placed["ok"]:
                    continue
                stop_order = placed["stop_order"]
                stop_status = order_status_str(stop_order)

            # 1. gap watchdog — application-dependent; NOT a stop-market.
            quote = fresh_bid(get_quote_fn, symbol, now_fn=now_fn)
            stop_trigger = float(getattr(stop_order, "stop_price", 0) or 0)
            if (quote is not None and stop_trigger > 0
                    and quote["bid"] <= stop_trigger
                    and stop_status in FILLABLE_STATUSES):
                journal.record_state_transition(
                    trade_id, "EMERGENCY_EXIT",
                    f"gap watchdog: bid {quote['bid']} <= stop trigger "
                    f"{stop_trigger} with stop unfilled")
                actions.append(_full_close(
                    client, journal, view,
                    reason=f"gap watchdog (bid {quote['bid']} <= trigger "
                           f"{stop_trigger})",
                    critical=True, config=config, alert_fn=alert_fn,
                    sleep_fn=sleep_fn, clock_fn=clock_fn))
                continue

            # 2. regime exit (unknown regime = no exit, stop stays).
            regime = regime_fn(symbol)
            if regime == "BEARISH":
                actions.append(_full_close(
                    client, journal, view, reason="daily regime BEARISH",
                    critical=False, config=config, alert_fn=alert_fn,
                    sleep_fn=sleep_fn, clock_fn=clock_fn))
                continue

            # 3. time stop — 10 days without TP1.
            entered_at = view.plan.get("entry_filled_at")
            if view.state == "PROTECTED" and entered_at:
                age_days = (now_fn() - _parse_ts(entered_at)).total_seconds() / 86400
                if age_days > _TIME_STOP_DAYS:
                    actions.append(_full_close(
                        client, journal, view,
                        reason=f"time stop ({age_days:.1f}d without TP1)",
                        critical=False, config=config, alert_fn=alert_fn,
                        sleep_fn=sleep_fn, clock_fn=clock_fn))
                    continue

            # 3.5. breakeven enforcement — after TP1 the stop never sits
            # below breakeven. The normal TP1 path already replaces the
            # stop at breakeven; this is the idempotent repair for
            # crash-recovered states re-protected at the structural stop.
            if view.state == "TP1_FILLED":
                avg_entry = _avg_entry_price(view) or 0.0
                structural = view.plan.get("structural_stop") or 0.0
                floor = max(structural, avg_entry)
                current_trigger = float(getattr(stop_order, "stop_price", 0) or 0)
                if floor > 0 and current_trigger < floor - QTY_TOLERANCE:
                    status, final = _cancel_stop_confirmed(
                        client, journal, view, stop_order, config, sleep_fn,
                        clock_fn)
                    if status == "unknown":
                        actions.append(_go_recovery(
                            journal, view, alert_fn,
                            "stop cancel unresolved during breakeven move"))
                        continue
                    if status == "filled":
                        actions.append(_close_trade_from_stop_fill(
                            client, journal, view, final, alert_fn))
                        continue
                    placed = _place_verified_stop(
                        client, journal, trade_id=trade_id, symbol=symbol,
                        setup=view.setup or "A",
                        signal_ts=_parse_ts(view.plan.get("signal_bar_ts")
                                            or view.created_at),
                        stop_price=floor, config=config, alert_fn=alert_fn,
                        sleep_fn=sleep_fn, clock_fn=clock_fn)
                    actions.append({"action": "breakeven_enforce",
                                    "symbol": symbol, "ok": placed["ok"],
                                    "stop": floor})
                    continue

            # 4. TP triggers — fresh executable bid only.
            if quote is not None:
                tp1_price = view.plan.get("tp1_price")
                tp2_price = view.plan.get("tp2_price")
                if (view.state in ("PROTECTED", "TP1_TRIGGERED")
                        and tp1_price and quote["bid"] >= tp1_price):
                    actions.append(_execute_take_profit(
                        client, journal, view, level="tp1", quote=quote,
                        config=config, alert_fn=alert_fn, sleep_fn=sleep_fn,
                        clock_fn=clock_fn))
                    continue
                if (view.state in ("TP1_FILLED", "TP2_TRIGGERED")
                        and tp2_price and quote["bid"] >= tp2_price):
                    actions.append(_execute_take_profit(
                        client, journal, view, level="tp2", quote=quote,
                        config=config, alert_fn=alert_fn, sleep_fn=sleep_fn,
                        clock_fn=clock_fn))
                    continue

            # 5. runner maintenance after TP2.
            if view.state == "TP2_FILLED":
                runner_ctx = runner_ctx_fn(symbol, view)
                if runner_ctx:
                    runner_action = _maybe_trail_runner(
                        client, journal, view, runner_ctx, config, alert_fn,
                        sleep_fn, clock_fn)
                    if runner_action is not None:
                        actions.append(runner_action)
        except StopIteration:
            continue
        except Exception as exc:
            journal.record_error(trade_id, f"management crash: {exc}")
            actions.append(_go_recovery(journal, journal.trades()[trade_id],
                                        alert_fn,
                                        f"management pass crashed: {exc}"))
    return actions
