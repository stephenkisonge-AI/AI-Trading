"""Phase 2 — deterministic order identity + idempotent, verifying execution.

Everything here obeys the safety rules from docs/BASELINE.md:
  - rule 15: an order timeout never means "nothing filled"
  - rule 16: a submitted order is not accepted until broker state confirms
  - rule 17: a cancel is not canceled until Alpaca reports a terminal state
  - rule 12: unresolvable broker/journal state freezes new entries

The persist-then-submit contract (one broker action):
    1. deterministic action ID / client order ID (make_client_order_id)
    2. persist the intended action        (journal.record_action_intent)
    3. confirm persistence succeeded      (False -> DO NOT SUBMIT)
    4. submit the broker request
    5. persist the broker response
    6. query the broker to verify the resulting state
    7. persist the verified state

All functions take the client and journal as arguments — no globals —
so every failure mode is testable with fakes.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import timezone
from typing import Callable, Optional


# =========================================================================
# Deterministic client order IDs
# =========================================================================

# Alpaca rejects client_order_id longer than 48 characters (see
# src/day_trader.py, verified in production for the day strand).
ALPACA_CLIENT_ORDER_ID_MAX = 48

# Logical broker actions this system performs. The leg number
# distinguishes deliberate replacements (stop-1 replaces stop-0);
# a RETRY of the same logical action reuses the same leg.
KNOWN_ACTIONS = {"entry", "stop", "tp1", "tp2", "close", "cancel"}

_ID_SAFE = re.compile(r"[^A-Za-z0-9]")


def format_signal_ts(ts) -> str:
    """UTC compact timestamp for order IDs: 20260710T120000Z.

    Accepts datetime or pandas.Timestamp. Naive inputs are treated as
    UTC (repo convention: all bar indexes are UTC).
    """
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y%m%dT%H%M%SZ")


def make_trade_id(strand: str, symbol: str, setup: str, signal_ts) -> str:
    """Stable identifier for one logical trade (journal trade_id)."""
    sym = _ID_SAFE.sub("", symbol)
    setup_token = _ID_SAFE.sub("", str(setup))
    return f"{strand}-{sym}-{setup_token}-{format_signal_ts(signal_ts)}"


def make_client_order_id(strand: str, symbol: str, setup: str,
                         signal_ts, action: str, leg: int) -> str:
    """{strand}-{symbol}-{setup}-{signal_timestamp}-{action}-{leg}

    Deterministic: the same logical action always produces the same ID,
    so a retry after a crash can find its own order at the broker. A
    deliberate replacement gets a new leg number.
    """
    if action not in KNOWN_ACTIONS:
        raise ValueError(f"unknown action '{action}' (known: {sorted(KNOWN_ACTIONS)})")
    if not isinstance(leg, int) or leg < 0:
        raise ValueError(f"leg must be a non-negative int, got {leg!r}")
    coid = f"{make_trade_id(strand, symbol, setup, signal_ts)}-{action}-{leg}"
    if len(coid) > ALPACA_CLIENT_ORDER_ID_MAX:
        raise ValueError(
            f"client_order_id '{coid}' is {len(coid)} chars, "
            f"exceeds Alpaca's {ALPACA_CLIENT_ORDER_ID_MAX}-char limit"
        )
    return coid


# The day strand adopts this same scheme with strand="day" during the
# equity mini-audit (Addendum B) — e.g. day-NVDA-A-20260710T133500Z-entry-0.
# Its parsers must accept both the legacy DAY-{setup}-{sym}-{epoch} tags
# (still present in Alpaca order history) and the canonical form.


def next_leg(journal, trade_id: str, action: str) -> int:
    """Deterministic leg number for a deliberate replacement: the count
    of intents already journaled for this trade+action."""
    view = journal.trades().get(trade_id)
    if view is None:
        return 0
    return sum(
        1 for a in view.actions.values()
        if (a.get("intent") or {}).get("action") == action
    )


# =========================================================================
# Validated execution configuration
# =========================================================================

@dataclass(frozen=True)
class ExecConfig:
    # SELL stop-limit: limit = stop * (1 - offset). 0.005 is current
    # production behavior; candidate offsets are evaluated in the Phase 6
    # replay before any change is frozen in.
    stop_limit_offset_pct: float = 0.005
    # Actual (fill-based) risk may exceed approved risk by at most this
    # fraction before the trade is rejected/unwound.
    max_actual_risk_tolerance: float = 0.10
    # Entry fill may slip past the planned entry by at most this fraction.
    max_entry_slippage_pct: float = 0.005
    fill_poll_timeout_sec: float = 60.0
    fill_poll_interval_sec: float = 1.0
    cancel_confirm_timeout_sec: float = 30.0


_CONFIG_BOUNDS = {
    "stop_limit_offset_pct": (0.0, 0.05, False),      # 0 < x <= 5%
    "max_actual_risk_tolerance": (0.0, 0.50, True),   # 0 <= x <= 50%
    "max_entry_slippage_pct": (0.0, 0.02, False),     # 0 < x <= 2%
    "fill_poll_timeout_sec": (0.0, 600.0, False),
    "fill_poll_interval_sec": (0.0, 60.0, False),
    "cancel_confirm_timeout_sec": (0.0, 600.0, False),
}

_ENV_NAMES = {
    "stop_limit_offset_pct": "STOP_LIMIT_OFFSET_PCT",
    "max_actual_risk_tolerance": "MAX_ACTUAL_RISK_TOLERANCE",
    "max_entry_slippage_pct": "MAX_ENTRY_SLIPPAGE_PCT",
}


def load_exec_config(environ=None) -> ExecConfig:
    """Build ExecConfig from the environment, validating every value.

    Raises ValueError on any invalid setting — callers must treat that
    as "block new entries", never fall back silently.
    """
    if environ is None:
        environ = os.environ
    kwargs = {}
    for attr, env_name in _ENV_NAMES.items():
        raw = environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            kwargs[attr] = float(raw)
        except ValueError:
            raise ValueError(f"{env_name}={raw!r} is not a number")
    config = ExecConfig(**kwargs)
    for attr, (low, high, inclusive_low) in _CONFIG_BOUNDS.items():
        value = getattr(config, attr)
        ok_low = value >= low if inclusive_low else value > low
        if not (ok_low and value <= high):
            raise ValueError(
                f"{attr}={value} outside valid range "
                f"({'[' if inclusive_low else '('}{low}, {high}])"
            )
    return config


# =========================================================================
# Broker order helpers
# =========================================================================

TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "stopped",
    "done_for_day", "replaced", "suspended",
}
FILLABLE_STATUSES = {
    "new", "accepted", "pending_new", "partially_filled", "held",
    "accepted_for_bidding", "calculated", "pending_cancel", "pending_replace",
}


def order_status_str(order) -> str:
    status = getattr(order, "status", None)
    if hasattr(status, "value"):
        return str(status.value).lower()
    return str(status).lower().rsplit(".", 1)[-1]


def order_filled_qty(order) -> float:
    try:
        return float(getattr(order, "filled_qty", None) or 0)
    except (TypeError, ValueError):
        return 0.0


def order_avg_price(order) -> Optional[float]:
    raw = getattr(order, "filled_avg_price", None)
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _order_state_payload(order) -> dict:
    return {
        "broker_order_id": str(getattr(order, "id", "")),
        "status": order_status_str(order),
        "filled_qty": order_filled_qty(order),
        "avg_fill_price": order_avg_price(order),
        "requested_qty": float(getattr(order, "qty", None) or 0),
    }


# =========================================================================
# Pre-submission idempotency (steps 1-4 of the Phase 2 checklist)
# =========================================================================

@dataclass
class Preflight:
    status: str                 # "clear" | "exists" | "unknown"
    order: object = None        # broker order when status == "exists"
    source: str = ""            # where the hit came from


def find_existing_order(client, journal, client_order_id: str,
                        symbol_no_slash: Optional[str] = None) -> Preflight:
    """Determine whether this logical action already reached the broker.

    "clear"   — safe to submit
    "exists"  — an order with this client_order_id exists (any status)
    "unknown" — broker could not be consulted; DO NOT submit (rule 16)
    """
    journal_hit = journal.find_action_by_client_order_id(client_order_id)
    journal_says_submitted = journal_hit is not None and (
        journal_hit[2].get("submitted") or journal_hit[2]["states"])

    # Direct broker lookup by client order ID — authoritative.
    try:
        order = client.get_order_by_client_id(client_order_id)
        if order is not None:
            return Preflight("exists", order, "broker:by_client_id")
        not_found = True
    except Exception as exc:
        not_found = (getattr(exc, "status_code", None) == 404
                     or "not found" in str(exc).lower())
        if not not_found:
            # Real failure — scan open+recent orders as a fallback
            # before declaring the state unknown.
            found, scan_ok = _scan_orders_for_coid(client, client_order_id,
                                                   symbol_no_slash)
            if found is not None:
                return Preflight("exists", found, "broker:order_scan")
            if not scan_ok:
                return Preflight("unknown", None, "broker:unreachable")
            if journal_says_submitted:
                return Preflight("unknown", None,
                                 "journal:submitted_but_broker_lookup_failed")
            return Preflight("clear", None, "broker:scan_empty")

    if journal_says_submitted:
        # Journal says the broker acknowledged it, but the broker lookup
        # found nothing. Contradiction — do not resubmit blindly.
        return Preflight("unknown", None, "journal:contradicts_broker")
    return Preflight("clear", None, "no_prior_order")


def _scan_orders_for_coid(client, client_order_id: str,
                          symbol_no_slash: Optional[str]):
    """(found_order_or_None, scan_completed_ok)."""
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    kwargs = {"limit": 500}
    if symbol_no_slash:
        kwargs["symbols"] = [symbol_no_slash]
    for status in (QueryOrderStatus.OPEN, QueryOrderStatus.CLOSED):
        try:
            orders = client.get_orders(filter=GetOrdersRequest(
                status=status, **kwargs))
        except Exception:
            return None, False
        for order in orders:
            if str(getattr(order, "client_order_id", "")) == client_order_id:
                return order, True
    return None, True


# =========================================================================
# Persist -> submit -> verify (the 7-step contract)
# =========================================================================

@dataclass
class SubmitOutcome:
    status: str            # "submitted" | "already_exists" | "ambiguous"
                           # | "intent_not_persisted" | "submit_failed"
    order: object = None   # verified broker order (or the existing one)
    persist_failures: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("submitted", "already_exists")


def persist_submit_verify(client, journal, *, trade_id: str, action_id: str,
                          client_order_id: str, action: str, intent: dict,
                          submit_fn: Callable[[], object],
                          symbol_no_slash: Optional[str] = None) -> SubmitOutcome:
    """Execute one broker action under the persist-then-submit contract.

    `submit_fn` performs the actual client.submit_order(...) call and
    must embed `client_order_id` in the request.
    """
    preflight = find_existing_order(client, journal, client_order_id,
                                    symbol_no_slash)
    if preflight.status == "exists":
        # The logical action already happened (duplicate run / retry
        # after crash). Record what the broker has and reuse it.
        journal.record_order_state(trade_id, action_id,
                                   recovered=True,
                                   **_order_state_payload(preflight.order))
        return SubmitOutcome("already_exists", preflight.order)
    if preflight.status == "unknown":
        journal.record_error(trade_id,
                             f"preflight ambiguous for {client_order_id} "
                             f"({preflight.source}) — refusing to submit")
        return SubmitOutcome("ambiguous")

    persisted = journal.record_action_intent(
        trade_id, action_id, action,
        client_order_id=client_order_id, **intent)
    if not persisted:
        # Contract: if the intent cannot be persisted, do not submit.
        return SubmitOutcome("intent_not_persisted")

    try:
        submitted = submit_fn()
    except Exception as exc:
        journal.record_error(trade_id, f"submit failed for {client_order_id}: {exc}")
        # The request may have reached Alpaca despite the error —
        # deterministic ID lets us check instead of guessing.
        recheck = find_existing_order(client, journal, client_order_id,
                                      symbol_no_slash)
        if recheck.status == "exists":
            journal.record_order_state(trade_id, action_id,
                                       recovered=True,
                                       **_order_state_payload(recheck.order))
            return SubmitOutcome("already_exists", recheck.order)
        if recheck.status == "unknown":
            return SubmitOutcome("ambiguous")
        return SubmitOutcome("submit_failed")

    failures = []
    if not journal.record_order_submitted(
            trade_id, action_id, **_order_state_payload(submitted)):
        # Submission succeeded but response persistence failed. The
        # deterministic client order ID + Alpaca order history recover
        # this on the next run (resolve_unresolved_intent). Flag it.
        failures.append("order_submitted_event")

    # Verify with a fresh broker read (rule 16).
    verified = submitted
    try:
        verified = client.get_order_by_id(getattr(submitted, "id"))
    except Exception as exc:
        journal.record_error(trade_id,
                             f"verify read failed for {client_order_id}: {exc}")
    if not journal.record_order_state(trade_id, action_id,
                                      **_order_state_payload(verified)):
        failures.append("order_state_event")

    return SubmitOutcome("submitted", verified, persist_failures=failures)


def resolve_unresolved_intent(client, journal, trade_id: str,
                              action_id: str) -> str:
    """Recovery for 'intent persisted, response missing' after a crash.

    Returns: "recovered" (order found and journaled), "not_submitted"
    (broker has no such order — safe to retry the same ID), or
    "unknown" (broker unreachable — keep the trade frozen).
    """
    view = journal.trades().get(trade_id)
    if view is None or action_id not in view.actions:
        return "unknown"
    intent = view.actions[action_id].get("intent") or {}
    coid = intent.get("client_order_id", "")
    preflight = find_existing_order(client, journal, coid)
    if preflight.status == "exists":
        journal.record_order_submitted(trade_id, action_id,
                                       recovered=True,
                                       **_order_state_payload(preflight.order))
        journal.record_order_state(trade_id, action_id,
                                   **_order_state_payload(preflight.order))
        return "recovered"
    if preflight.status == "clear":
        journal.record_order_state(trade_id, action_id,
                                   broker_order_id="", status="not_submitted",
                                   filled_qty=0.0)
        return "not_submitted"
    return "unknown"


# =========================================================================
# Cancellation — rule 17: not canceled until Alpaca says terminal
# =========================================================================

def wait_for_terminal(client, order_id: str, timeout_sec: float,
                      poll_sec: float = 0.5,
                      sleep_fn: Callable[[float], None] = time.sleep,
                      clock_fn: Callable[[], float] = time.monotonic):
    """Poll until the order reaches a terminal state.

    Returns (reached_terminal: bool, last_order). A cancellation race
    (order FILLED while cancel was pending) still counts as terminal —
    the caller must inspect the status and filled qty, never assume
    the cancel won.
    """
    deadline = clock_fn() + timeout_sec
    last_order = None
    while True:
        try:
            last_order = client.get_order_by_id(order_id)
            if order_status_str(last_order) in TERMINAL_ORDER_STATUSES:
                return True, last_order
        except Exception:
            pass  # transient read failure — keep polling until deadline
        if clock_fn() >= deadline:
            return False, last_order
        sleep_fn(poll_sec)


# =========================================================================
# Entry fill resolution — rule 15: timeout != nothing filled
# =========================================================================

@dataclass
class FillResult:
    status: str            # "filled" | "partial" | "none" | "unknown"
    filled_qty: float = 0.0
    avg_price: Optional[float] = None
    order: object = None


def resolve_entry_fill(client, journal, *, trade_id: str, action_id: str,
                       order, client_order_id: str, config: ExecConfig,
                       sleep_fn: Callable[[float], None] = time.sleep,
                       clock_fn: Callable[[], float] = time.monotonic) -> FillResult:
    """Wait for an entry order to fill; on timeout, establish the REAL
    state instead of assuming zero fill.

    Timeout path: re-read by broker ID, then by client order ID; cancel
    only the unfilled remainder; wait for a confirmed terminal state;
    report the exact filled quantity. Unresolvable state returns
    "unknown" and marks the trade recovery-required (callers must
    freeze new entries).
    """
    order_id = str(getattr(order, "id", ""))
    deadline = clock_fn() + config.fill_poll_timeout_sec
    current = order
    while clock_fn() < deadline:
        try:
            current = client.get_order_by_id(order_id)
        except Exception:
            sleep_fn(config.fill_poll_interval_sec)
            continue
        status = order_status_str(current)
        if status == "filled":
            journal.record_order_state(trade_id, action_id,
                                       **_order_state_payload(current))
            return FillResult("filled", order_filled_qty(current),
                              order_avg_price(current), current)
        if status in TERMINAL_ORDER_STATUSES:
            # Canceled/rejected/expired — may still carry a partial fill.
            journal.record_order_state(trade_id, action_id,
                                       **_order_state_payload(current))
            qty = order_filled_qty(current)
            return FillResult("partial" if qty > 0 else "none", qty,
                              order_avg_price(current), current)
        sleep_fn(config.fill_poll_interval_sec)

    # --- timeout: determine the real order status -----------------------
    latest = None
    try:
        latest = client.get_order_by_id(order_id)
    except Exception:
        try:
            latest = client.get_order_by_client_id(client_order_id)
        except Exception:
            pass
    if latest is None:
        journal.record_recovery_required(
            trade_id, f"entry {client_order_id} unreadable after timeout")
        return FillResult("unknown")

    status = order_status_str(latest)
    journal.record_order_state(trade_id, action_id,
                               **_order_state_payload(latest))
    if status == "filled":
        # Fill landed between the last poll and the re-read.
        return FillResult("filled", order_filled_qty(latest),
                          order_avg_price(latest), latest)
    if status in TERMINAL_ORDER_STATUSES:
        qty = order_filled_qty(latest)
        return FillResult("partial" if qty > 0 else "none", qty,
                          order_avg_price(latest), latest)

    # Still working — cancel the unfilled remainder, then confirm.
    try:
        client.cancel_order_by_id(order_id)
    except Exception as exc:
        journal.record_error(trade_id, f"cancel after timeout failed: {exc}")
    reached_terminal, final = wait_for_terminal(
        client, order_id, config.cancel_confirm_timeout_sec,
        sleep_fn=sleep_fn, clock_fn=clock_fn)
    if not reached_terminal or final is None:
        journal.record_recovery_required(
            trade_id,
            f"entry {client_order_id} cancel did not reach a terminal "
            f"state within {config.cancel_confirm_timeout_sec}s")
        return FillResult("unknown",
                          order_filled_qty(final) if final else 0.0,
                          order_avg_price(final) if final else None, final)

    journal.record_order_state(trade_id, action_id,
                               **_order_state_payload(final))
    qty = order_filled_qty(final)  # cancellation race: may be fully filled
    if order_status_str(final) == "filled":
        return FillResult("filled", qty, order_avg_price(final), final)
    return FillResult("partial" if qty > 0 else "none", qty,
                      order_avg_price(final), final)


# =========================================================================
# Fill-based risk validation
# =========================================================================

@dataclass
class FillValidation:
    ok: bool
    reasons: list
    actual_risk_per_unit: Optional[float] = None
    actual_risk_dollars: Optional[float] = None
    actual_stop_distance_pct: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    stop_limit_price: Optional[float] = None
    max_loss_at_limit: Optional[float] = None


def validate_entry_fill(*, planned_entry: float, structural_stop: float,
                        approved_risk_dollars: float, filled_qty: float,
                        avg_fill_price: float, tp1_r: float, tp2_r: float,
                        min_qty_increment: float, config: ExecConfig,
                        price_decimals: int = 2) -> FillValidation:
    """Recalculate risk from the ACTUAL average fill (Phase 2 spec):

        actual_risk_per_unit  = fill - structural_stop
        actual_risk_dollars   = filled_qty * actual_risk_per_unit
        tp{n}                 = fill + actual_risk_per_unit * tp{n}_r

    Sizing/protection must also survive the stop-LIMIT price: the loss
    modeled at limit (not merely at trigger) stays within tolerance.
    """
    reasons = []
    result = FillValidation(ok=False, reasons=reasons)

    risk_per_unit = avg_fill_price - structural_stop
    if risk_per_unit <= 0:
        reasons.append(
            f"risk per unit is zero/negative (fill {avg_fill_price} vs "
            f"stop {structural_stop}) — structural stop invalid relative "
            f"to the fill")
        return result
    if structural_stop <= 0:
        reasons.append(f"structural stop {structural_stop} is not positive")
        return result

    result.actual_risk_per_unit = risk_per_unit
    result.actual_risk_dollars = filled_qty * risk_per_unit
    result.actual_stop_distance_pct = risk_per_unit / avg_fill_price
    result.tp1 = avg_fill_price + risk_per_unit * tp1_r
    result.tp2 = avg_fill_price + risk_per_unit * tp2_r

    slippage_pct = (avg_fill_price - planned_entry) / planned_entry
    if slippage_pct > config.max_entry_slippage_pct:
        reasons.append(
            f"entry slippage {slippage_pct*100:.3f}% exceeds "
            f"{config.max_entry_slippage_pct*100:.3f}% cap "
            f"(planned {planned_entry}, filled {avg_fill_price})")

    allowed_risk = approved_risk_dollars * (1 + config.max_actual_risk_tolerance)
    if result.actual_risk_dollars > allowed_risk:
        reasons.append(
            f"actual risk ${result.actual_risk_dollars:.2f} exceeds "
            f"approved ${approved_risk_dollars:.2f} "
            f"+{config.max_actual_risk_tolerance*100:.0f}% tolerance")

    if filled_qty < min_qty_increment:
        reasons.append(
            f"filled qty {filled_qty} is below the minimum tradable "
            f"increment {min_qty_increment} — cannot be safely protected")

    # Loss at the stop-LIMIT price, after price rounding — the number
    # that actually bounds a non-gapped stop execution.
    stop_limit = round(structural_stop * (1 - config.stop_limit_offset_pct),
                       price_decimals)
    result.stop_limit_price = stop_limit
    result.max_loss_at_limit = filled_qty * (avg_fill_price - stop_limit)
    if result.max_loss_at_limit > allowed_risk:
        reasons.append(
            f"modeled loss at stop-limit price ${result.max_loss_at_limit:.2f} "
            f"(limit {stop_limit}) exceeds approved "
            f"${approved_risk_dollars:.2f} +tolerance — price/qty precision "
            f"materially increases risk")

    result.ok = not reasons
    return result


# =========================================================================
# Emergency unwind — filled trade that violates approved risk
# =========================================================================

def unwind_trade(client, journal, *, trade_id: str, symbol: str,
                 strand: str, setup: str, signal_ts, reason: str,
                 alert_fn: Callable[[str], object],
                 config: ExecConfig,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 clock_fn: Callable[[], float] = time.monotonic) -> dict:
    """Flatten a filled trade whose actual risk cannot be accepted.

    Sequence (Phase 2 spec): reconcile position -> cancel conflicting
    open exits -> confirm cancellations -> re-read position -> market
    sell the EXACT remaining quantity -> poll/reconcile the fill ->
    confirm flat -> CRITICAL alert -> freeze entries.
    """
    from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
    from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

    symbol_no_slash = symbol.replace("/", "")
    out = {"trade_id": trade_id, "symbol": symbol, "reason": reason,
           "cancelled": [], "close_order_id": None, "closed": False,
           "error": None}
    journal.record_state_transition(trade_id, "UNWINDING", reason)
    journal.set_entry_freeze(True, f"unwinding {trade_id}: {reason}")

    def _fail(message: str) -> dict:
        out["error"] = message
        journal.record_recovery_required(trade_id, message)
        alert_fn(f"🚨 CRITICAL — unwind of {symbol} could not complete: "
                 f"{message}. Trade {trade_id} needs reconciliation; new "
                 f"entries are frozen.")
        return out

    # 1-3. Cancel open exit orders and confirm each cancellation.
    try:
        open_orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[symbol_no_slash]))
    except Exception as exc:
        return _fail(f"open-order fetch failed: {exc}")
    for order in open_orders:
        order_id = str(getattr(order, "id", ""))
        try:
            client.cancel_order_by_id(order_id)
        except Exception as exc:
            return _fail(f"cancel of {order_id} failed: {exc}")
        reached_terminal, final = wait_for_terminal(
            client, order_id, config.cancel_confirm_timeout_sec,
            sleep_fn=sleep_fn, clock_fn=clock_fn)
        if not reached_terminal:
            return _fail(f"cancel of {order_id} never reached terminal state")
        out["cancelled"].append(order_id)
        journal.record_order_state(trade_id, f"{trade_id}-unwind-cancel",
                                   **_order_state_payload(final))

    # 4. Re-read the position — exits may have partially filled first.
    try:
        position = client.get_open_position(symbol_no_slash)
        remaining_qty = float(getattr(position, "qty", 0))
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404 or "not found" in str(exc).lower():
            remaining_qty = 0.0
        else:
            return _fail(f"position re-read failed: {exc}")

    if remaining_qty <= 0:
        out["closed"] = True
        journal.record_state_transition(trade_id, "CLOSED",
                                        "unwind: position already flat")
        alert_fn(f"🚨 CRITICAL — {symbol} trade {trade_id} unwound: {reason}. "
                 f"Position already flat after cancellations. Entries frozen "
                 f"until reconciliation passes.")
        return out

    # 5-7. Market-sell the exact remaining quantity under the contract.
    leg = next_leg(journal, trade_id, "close")
    coid = make_client_order_id(strand, symbol, setup, signal_ts, "close", leg)
    outcome = persist_submit_verify(
        client, journal, trade_id=trade_id,
        action_id=f"{trade_id}-close-{leg}", client_order_id=coid,
        action="close", intent={"requested_qty": remaining_qty,
                                "reason": reason},
        submit_fn=lambda: client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=remaining_qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, client_order_id=coid)),
        symbol_no_slash=symbol_no_slash)
    if not outcome.ok:
        return _fail(f"unwind market sell not confirmed ({outcome.status})")
    out["close_order_id"] = str(getattr(outcome.order, "id", ""))

    fill = resolve_entry_fill(
        client, journal, trade_id=trade_id,
        action_id=f"{trade_id}-close-{leg}", order=outcome.order,
        client_order_id=coid, config=config, sleep_fn=sleep_fn,
        clock_fn=clock_fn)
    if fill.status not in ("filled",):
        return _fail(f"unwind sell did not fully fill (status {fill.status}, "
                     f"qty {fill.filled_qty})")

    # 7. Confirm flat.
    try:
        client.get_open_position(symbol_no_slash)
        return _fail("position still open after unwind sell filled")
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404 or "not found" in str(exc).lower():
            pass  # flat — expected
        else:
            return _fail(f"final position confirm failed: {exc}")

    out["closed"] = True
    journal.record_exit(trade_id, qty=fill.filled_qty, price=fill.avg_price,
                        reason="UNWIND_EXCESS_RISK")
    journal.record_trade_closed(trade_id, reason=f"unwound: {reason}")
    # 8-9. CRITICAL alert; entries stay frozen until reconciliation passes.
    alert_fn(f"🚨 CRITICAL — {symbol} trade {trade_id} UNWOUND at market: "
             f"{reason}. Sold {fill.filled_qty} @ {fill.avg_price}. New "
             f"entries frozen until a reconciliation pass succeeds.")
    return out
