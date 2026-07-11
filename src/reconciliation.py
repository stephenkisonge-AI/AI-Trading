"""Read-only journal-vs-broker reconciliation — Phase 1.

Alpaca paper-account state is the final source of truth (rule 20); the
journal is reconciled AGAINST it. This module never submits, cancels,
or replaces an order — it only reads. Any future repair functionality
must live behind a separate, explicit paper-repair command.

Scoping: both strands share one Alpaca paper account, so reconciliation
only inspects broker positions/orders whose symbol is inside the given
universe (default: the crypto swing universe). Day-strand equities are
invisible to it by design — see tests/test_trader.py for the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.journal import Journal, TradeView
from src.universe import CRYPTO_SYMBOLS_NO_SLASH


# Broker order statuses we understand. Anything else is treated as a
# reconciliation failure (rule: never guess at unknown broker state).
KNOWN_ORDER_STATUSES = {
    "new", "accepted", "pending_new", "accepted_for_bidding", "calculated",
    "partially_filled", "filled", "done_for_day",
    "canceled", "cancelled", "expired", "rejected", "stopped", "suspended",
    "replaced", "pending_cancel", "pending_replace", "held",
}

# Statuses under which an order can still (partially) execute.
OPEN_ORDER_STATUSES = {
    "new", "accepted", "pending_new", "accepted_for_bidding", "calculated",
    "partially_filled", "held", "pending_cancel", "pending_replace",
}

QTY_TOLERANCE = 1e-8


@dataclass
class Finding:
    code: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


@dataclass
class ReconcileReport:
    findings: list[Finding] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    def add(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message))

    def note(self, message: str) -> None:
        self.info.append(message)

    @property
    def ok(self) -> bool:
        return not self.findings

    def render(self) -> str:
        lines = ["=== RECONCILIATION REPORT ==="]
        for note in self.info:
            lines.append(f"  {note}")
        # ASCII only — this report must render on Windows cp1252 consoles
        # and inside GH Actions logs without encoding surprises.
        if self.ok:
            lines.append("RESULT: RECONCILED - journal and broker agree.")
        else:
            lines.append(f"RESULT: {len(self.findings)} MISMATCH(ES):")
            for finding in self.findings:
                lines.append(f"  x {finding}")
        return "\n".join(lines)


def _norm_symbol(symbol) -> str:
    return str(symbol or "").replace("/", "").upper()


def _status_str(order) -> str:
    status = getattr(order, "status", None)
    if hasattr(status, "value"):
        return str(status.value).lower()
    # Enum reprs like "OrderStatus.FILLED" → "filled"
    return str(status).lower().rsplit(".", 1)[-1]


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _order_remaining_qty(order) -> float:
    qty = _f(getattr(order, "qty", None))
    filled = _f(getattr(order, "filled_qty", None))
    return max(qty - filled, 0.0)


def _side_str(order) -> str:
    side = getattr(order, "side", None)
    if hasattr(side, "value"):
        return str(side.value).lower()
    return str(side).lower().rsplit(".", 1)[-1]


def _is_stop_order(order) -> bool:
    order_type = getattr(order, "order_type", None) or getattr(order, "type", None)
    if hasattr(order_type, "value"):
        order_type = order_type.value
    return "stop" in str(order_type).lower()


def reconcile(
    journal: Journal,
    positions: list,
    open_orders: list,
    recent_orders: list,
    universe: Optional[set[str]] = None,
) -> ReconcileReport:
    """Compare journal state against broker state. Pure/read-only.

    positions      — broker positions (all symbols; filtered here)
    open_orders    — broker open orders (all symbols; filtered here)
    recent_orders  — recent broker orders in ALL statuses, for looking
                     up client order IDs and terminal fills
    """
    if universe is None:
        universe = CRYPTO_SYMBOLS_NO_SLASH
    report = ReconcileReport()

    positions = [p for p in positions
                 if _norm_symbol(getattr(p, "symbol", "")) in universe]
    open_orders = [o for o in open_orders
                   if _norm_symbol(getattr(o, "symbol", "")) in universe]
    recent_orders = [o for o in recent_orders
                     if _norm_symbol(getattr(o, "symbol", "")) in universe]

    trades = journal.trades()
    open_trades = {tid: v for tid, v in trades.items() if not v.is_terminal()}
    freeze = journal.entry_freeze()
    known_coids = journal.known_client_order_ids()

    report.note(f"journal: {len(trades)} trades total, "
                f"{len(open_trades)} nonterminal")
    report.note(f"broker: {len(positions)} position(s), "
                f"{len(open_orders)} open order(s), "
                f"{len(recent_orders)} recent order(s) in scope")
    report.note(f"entry freeze: {'ACTIVE - ' + freeze['reason'] if freeze['frozen'] else 'off'}")

    positions_by_symbol = {
        _norm_symbol(getattr(p, "symbol", "")): p for p in positions
    }
    all_broker_orders = list(open_orders) + list(recent_orders)
    broker_orders_by_coid: dict[str, list] = {}
    for order in all_broker_orders:
        coid = getattr(order, "client_order_id", None)
        if coid:
            broker_orders_by_coid.setdefault(str(coid), []).append(order)

    # --- 1. Unknown broker order statuses ---------------------------------
    for order in all_broker_orders:
        status = _status_str(order)
        if status not in KNOWN_ORDER_STATUSES:
            report.add(
                "UNKNOWN_ORDER_STATUS",
                f"order {getattr(order, 'id', '?')} "
                f"({_norm_symbol(getattr(order, 'symbol', ''))}) has "
                f"unrecognized status '{status}'",
            )

    # --- 2. Duplicate client order IDs at the broker -----------------------
    for coid, orders in broker_orders_by_coid.items():
        distinct_ids = {str(getattr(o, "id", "")) for o in orders}
        if len(distinct_ids) > 1:
            report.add(
                "DUPLICATE_CLIENT_ORDER_ID",
                f"client_order_id '{coid}' maps to {len(distinct_ids)} "
                f"distinct broker orders: {sorted(distinct_ids)}",
            )

    # --- 3. Trades flagged for recovery ------------------------------------
    for trade_id, view in open_trades.items():
        if view.recovery_required:
            report.add(
                "RECOVERY_REQUIRED",
                f"trade {trade_id} is marked recovery-required: "
                f"{view.recovery_reason or 'no reason recorded'}",
            )

    # --- 4. Intents persisted but never resolved ---------------------------
    for trade_id, view in open_trades.items():
        for action_id in view.unresolved_intents():
            intent = view.actions[action_id]["intent"] or {}
            coid = intent.get("client_order_id", "")
            at_broker = coid in broker_orders_by_coid
            report.add(
                "UNRESOLVED_INTENT",
                f"trade {trade_id} action {action_id}: intent persisted but "
                f"no broker response recorded "
                f"({'order EXISTS at broker' if at_broker else 'no matching broker order found'})",
            )

    # --- 5. Journal trades vs broker positions -----------------------------
    for trade_id, view in open_trades.items():
        symbol = _norm_symbol(view.symbol)
        expected_qty = view.expected_position_qty()
        position = positions_by_symbol.get(symbol)
        if expected_qty > QTY_TOLERANCE:
            if position is None:
                report.add(
                    "JOURNAL_TRADE_NOT_AT_BROKER",
                    f"trade {trade_id} expects position "
                    f"{expected_qty:.8f} {symbol} but the broker holds none",
                )
            else:
                broker_qty = _f(getattr(position, "qty", None))
                if abs(broker_qty - expected_qty) > QTY_TOLERANCE:
                    report.add(
                        "QTY_MISMATCH",
                        f"trade {trade_id} {symbol}: journal expects "
                        f"{expected_qty:.8f}, broker holds {broker_qty:.8f}",
                    )

    # --- 6. Broker positions vs journal ------------------------------------
    journal_symbols_with_qty = {
        _norm_symbol(v.symbol)
        for v in open_trades.values()
        if v.expected_position_qty() > QTY_TOLERANCE
    }
    for symbol, position in positions_by_symbol.items():
        if symbol not in journal_symbols_with_qty:
            report.add(
                "POSITION_NOT_IN_JOURNAL",
                f"broker holds {_f(getattr(position, 'qty', None)):.8f} "
                f"{symbol} with no nonterminal journal trade expecting it",
            )

    # --- 7. Orphan open orders ----------------------------------------------
    for order in open_orders:
        coid = str(getattr(order, "client_order_id", "") or "")
        if coid not in known_coids:
            report.add(
                "ORPHAN_OPEN_ORDER",
                f"open order {getattr(order, 'id', '?')} "
                f"({_norm_symbol(getattr(order, 'symbol', ''))}, "
                f"client_order_id='{coid or 'none'}') is not in the journal",
            )

    # --- 8. Journal order-state staleness (partial fills etc.) -------------
    broker_by_id = {str(getattr(o, "id", "")): o for o in all_broker_orders}
    for trade_id, view in open_trades.items():
        for action_id, action in view.actions.items():
            states = action["states"]
            if not states:
                continue
            last = states[-1]
            broker_order_id = str(
                last.get("broker_order_id")
                or (action.get("submitted") or {}).get("broker_order_id")
                or ""
            )
            order = broker_by_id.get(broker_order_id)
            if order is None:
                continue  # not in the recent window — nothing to compare
            journal_status = str(last.get("status", "")).lower()
            journal_filled = _f(last.get("filled_qty"))
            broker_status = _status_str(order)
            broker_filled = _f(getattr(order, "filled_qty", None))
            if (journal_status != broker_status
                    or abs(journal_filled - broker_filled) > QTY_TOLERANCE):
                report.add(
                    "STALE_ORDER_STATE",
                    f"trade {trade_id} action {action_id} order "
                    f"{broker_order_id}: journal has "
                    f"({journal_status}, filled {journal_filled:.8f}) but "
                    f"broker reports ({broker_status}, filled "
                    f"{broker_filled:.8f})",
                )

    # --- 9. Protection + sell-quantity invariant per position --------------
    for symbol, position in positions_by_symbol.items():
        position_qty = _f(getattr(position, "qty", None))
        symbol_sells = [
            o for o in open_orders
            if _norm_symbol(getattr(o, "symbol", "")) == symbol
            and _side_str(o) == "sell"
            and _status_str(o) in OPEN_ORDER_STATUSES
        ]
        stop_orders = [o for o in symbol_sells if _is_stop_order(o)]
        if not stop_orders:
            report.add(
                "UNPROTECTED_POSITION",
                f"position {symbol} ({position_qty:.8f}) has no open "
                f"stop order protecting it",
            )
        total_sell_remaining = sum(_order_remaining_qty(o) for o in symbol_sells)
        if total_sell_remaining > position_qty + QTY_TOLERANCE:
            report.add(
                "SELL_QTY_INVARIANT",
                f"{symbol}: open sell orders total remaining "
                f"{total_sell_remaining:.8f} > position {position_qty:.8f}",
            )

    return report
