"""Phase 4 — fail-closed entry gates + per-strand risk ledgers.

Every gate here BLOCKS new entries when its data is unavailable, stale,
invalid, or contradictory ("gate data unavailable — new entries
frozen"). This inverts the legacy behavior in src/trader.py, where
quote/portfolio-history failures logged to stderr and let the entry
through.

Blocking an entry never blocks management: position management, stop
monitoring, the gap watchdog, risk-reducing exits, reconciliation, and
alerts all run regardless of gate outcomes. Only new risk creation is
gated.

Per-strand risk ledgers (Addendum C): the two strands share one Alpaca
paper account, so account-level daily/weekly/drawdown math would let
one strand's losses freeze (or worse, unfreeze) the other. Instead:

  * realized P&L per strand comes from that strand's OWN journal
    (exits carry prices and the trade's actual fill price),
  * unrealized P&L per strand comes from broker positions filtered to
    the strand's universe,
  * thresholds are expressed as a fraction of account equity (the
    shared risk base),
  * one account-wide EMERGENCY drawdown brake (portfolio history)
    remains as a last-resort that freezes ALL strands.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.journal import Journal
from src.universe import CRYPTO_SYMBOLS_NO_SLASH


# Thresholds (fractions of account equity) — same numbers as the
# strategy doc; the *scope* changed from account-wide to per-strand.
DAILY_LOSS_CAP_PCT = 0.02
WEEKLY_LOSS_CAP_PCT = 0.05
STRAND_DRAWDOWN_CAP_PCT = 0.10
STRAND_DRAWDOWN_WINDOW_DAYS = 30
# Account-wide last-resort brake (shared; freezes ALL strands).
EMERGENCY_ACCOUNT_DRAWDOWN_PCT = 0.15
SPREAD_CAP_PCT = 0.005
MAX_QUOTE_AGE_SEC = 120.0

BLOCKED_UNAVAILABLE = "gate data unavailable — new entries frozen"


@dataclass
class GateDecision:
    allowed: bool
    gate: str = ""
    reason: str = ""

    @staticmethod
    def ok() -> "GateDecision":
        return GateDecision(True)

    @staticmethod
    def blocked(gate: str, reason: str) -> "GateDecision":
        return GateDecision(False, gate, reason)

    def __str__(self) -> str:
        return "allowed" if self.allowed else f"[{self.gate}] {self.reason}"


def _unavailable(gate: str, detail: str) -> GateDecision:
    return GateDecision.blocked(gate, f"{BLOCKED_UNAVAILABLE} ({detail})")


# =========================================================================
# Per-strand ledger (journal-derived)
# =========================================================================

def _exit_pl_events(journal: Journal) -> list[tuple[datetime, float]]:
    """(exit timestamp, realized P&L dollars) per EXIT_REALIZED event,
    chronological. P&L per exit = qty * (exit price - actual fill)."""
    out: list[tuple[datetime, float]] = []
    trades = journal.trades()
    for event in journal.events():
        if event["kind"] != "EXIT_REALIZED" or not event.get("trade_id"):
            continue
        view = trades.get(event["trade_id"])
        if view is None:
            continue
        fill_price = view.plan.get("actual_fill_price")
        payload = event.get("payload") or {}
        qty = payload.get("qty")
        price = payload.get("price")
        if fill_price is None or qty is None or price is None:
            continue
        ts = datetime.fromisoformat(event["ts"])
        out.append((ts, float(qty) * (float(price) - float(fill_price))))
    return out


def strand_realized_pl(journal: Journal, since: datetime) -> float:
    return sum(pl for ts, pl in _exit_pl_events(journal) if ts >= since)


def strand_unrealized_pl(positions, universe: set[str]) -> Optional[float]:
    """Unrealized P&L of the strand's own broker positions. None when a
    position lacks the field (treat as unavailable)."""
    total = 0.0
    for position in positions:
        symbol = str(getattr(position, "symbol", "")).replace("/", "").upper()
        if symbol not in universe:
            continue
        raw = getattr(position, "unrealized_pl", None)
        if raw is None:
            return None
        try:
            total += float(raw)
        except (TypeError, ValueError):
            return None
    return total


def strand_drawdown(journal: Journal, window_days: int,
                    now: datetime) -> float:
    """Peak-to-current drawdown (dollars, <= 0) of the strand's
    cumulative realized P&L curve over the window."""
    since = now - timedelta(days=window_days)
    curve = 0.0
    peak = 0.0
    drawdown = 0.0
    for ts, pl in _exit_pl_events(journal):
        if ts < since:
            continue
        curve += pl
        peak = max(peak, curve)
        drawdown = min(drawdown, curve - peak)
    return min(drawdown, curve - peak)


# =========================================================================
# The gate chain
# =========================================================================

def evaluate_entry_gates(
    *,
    journal: Optional[Journal],
    client,
    symbol: str,
    get_quote_fn: Callable,
    universe: set[str] = CRYPTO_SYMBOLS_NO_SLASH,
    max_positions: int = 2,
    correlated_pairs: tuple = (("BTCUSD", "ETHUSD"),),
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    portfolio_history_fn: Optional[Callable] = None,
) -> GateDecision:
    """Run every entry gate, fail-closed. First block wins.

    Order matters: cheap/structural gates first, then broker state,
    then risk ledgers, then the account-wide brake, then the quote gate
    (freshest data last, closest to submission).
    """
    now = now_fn()

    # --- journal availability + standing freeze ---------------------------
    if journal is None:
        return _unavailable("journal", "no journal configured")
    freeze = journal.entry_freeze()
    if freeze["frozen"]:
        return GateDecision.blocked(
            "entry_freeze",
            f"entry freeze active: {freeze['reason']} — run "
            f"scripts/reconcile.py; entries stay frozen until a "
            f"reconciliation pass succeeds")

    # --- account fetch (equity is the risk base for every threshold) ------
    try:
        account = client.get_account()
        equity = float(account.equity)
        buying_power = float(getattr(account, "buying_power", 0) or 0)
    except Exception as exc:
        return _unavailable("account", f"account fetch failed: {exc}")
    if equity <= 0:
        return _unavailable("account", f"non-positive equity {equity}")
    if buying_power <= 0:
        return GateDecision.blocked(
            "buying_power", f"buying power {buying_power} — cannot fund entry")

    # --- position fetch ----------------------------------------------------
    try:
        positions = client.get_all_positions()
    except Exception as exc:
        return _unavailable("positions", f"position fetch failed: {exc}")
    own_positions = [
        p for p in positions
        if str(getattr(p, "symbol", "")).replace("/", "").upper() in universe
    ]
    symbol_ns = symbol.replace("/", "").upper()
    own_symbols = {str(getattr(p, "symbol", "")).replace("/", "").upper()
                   for p in own_positions}
    if len(own_positions) >= max_positions:
        return GateDecision.blocked(
            "max_positions",
            f"max positions reached ({len(own_positions)}/{max_positions})")
    if symbol_ns in own_symbols:
        return GateDecision.blocked("already_held", f"already hold {symbol}")
    for pair in correlated_pairs:
        if symbol_ns in pair:
            other = pair[1] if symbol_ns == pair[0] else pair[0]
            if other in own_symbols:
                return GateDecision.blocked(
                    "correlation",
                    f"correlation rule: already hold {other}")

    # --- open-order fetch (needed to know nothing conflicts) --------------
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        open_orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, limit=500))
    except Exception as exc:
        return _unavailable("open_orders", f"open-order fetch failed: {exc}")
    for order in open_orders:
        osym = str(getattr(order, "symbol", "")).replace("/", "").upper()
        if osym == symbol_ns:
            return GateDecision.blocked(
                "open_order_conflict",
                f"open order already exists for {symbol}")

    # --- per-strand risk ledgers (journal + own unrealized) ---------------
    unrealized = strand_unrealized_pl(own_positions, universe)
    if unrealized is None:
        return _unavailable("unrealized",
                            "position missing unrealized_pl field")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    daily_pl = strand_realized_pl(journal, day_start) + unrealized
    if daily_pl <= -DAILY_LOSS_CAP_PCT * equity:
        return GateDecision.blocked(
            "daily_loss",
            f"strand daily P&L ${daily_pl:.2f} breaches "
            f"-{DAILY_LOSS_CAP_PCT*100:.0f}% of equity (${equity:.2f})")
    weekly_pl = strand_realized_pl(journal, week_start) + unrealized
    if weekly_pl <= -WEEKLY_LOSS_CAP_PCT * equity:
        return GateDecision.blocked(
            "weekly_loss",
            f"strand weekly P&L ${weekly_pl:.2f} breaches "
            f"-{WEEKLY_LOSS_CAP_PCT*100:.0f}% of equity (${equity:.2f})")
    drawdown = strand_drawdown(journal, STRAND_DRAWDOWN_WINDOW_DAYS, now)
    if drawdown <= -STRAND_DRAWDOWN_CAP_PCT * equity:
        return GateDecision.blocked(
            "strand_drawdown",
            f"strand {STRAND_DRAWDOWN_WINDOW_DAYS}d realized drawdown "
            f"${drawdown:.2f} breaches -{STRAND_DRAWDOWN_CAP_PCT*100:.0f}% "
            f"of equity")

    # --- 1 entry per day (strategy doc), journal-derived -------------------
    entry_action_ids_today = set()
    filled_action_ids = set()
    for event in journal.events():
        payload = event.get("payload") or {}
        if (event["kind"] == "ACTION_INTENT"
                and payload.get("action") == "entry"
                and datetime.fromisoformat(event["ts"]) >= day_start):
            entry_action_ids_today.add(payload.get("action_id"))
        elif (event["kind"] in ("ORDER_STATE", "ORDER_SUBMITTED")
                and float(payload.get("filled_qty") or 0) > 0):
            filled_action_ids.add(payload.get("action_id"))
    if entry_action_ids_today & filled_action_ids:
        return GateDecision.blocked(
            "daily_entry_cap",
            "daily entry cap hit (1 entry already filled today)")

    # --- account-wide emergency brake (shared last resort) ----------------
    if portfolio_history_fn is not None:
        try:
            equities = [e for e in (portfolio_history_fn() or [])
                        if e is not None and e > 0]
        except Exception as exc:
            return _unavailable("account_drawdown",
                                f"portfolio history failed: {exc}")
        if len(equities) >= 2:
            peak = max(equities)
            current = equities[-1]
            account_dd = (current - peak) / peak
            if account_dd <= -EMERGENCY_ACCOUNT_DRAWDOWN_PCT:
                return GateDecision.blocked(
                    "account_emergency_brake",
                    f"ACCOUNT drawdown {account_dd*100:.1f}% breaches "
                    f"-{EMERGENCY_ACCOUNT_DRAWDOWN_PCT*100:.0f}% — all "
                    f"strands frozen")

    # --- quote gate (fresh + sane + tight), last before submission --------
    try:
        quote = get_quote_fn(symbol)
        bid = float(quote.bid_price)
        ask = float(quote.ask_price)
        quote_ts = getattr(quote, "timestamp", None)
    except Exception as exc:
        return _unavailable("quote", f"quote fetch failed: {exc}")
    if bid <= 0 or ask <= 0 or ask < bid:
        return GateDecision.blocked(
            "quote", f"invalid quote (bid={bid}, ask={ask})")
    if quote_ts is None:
        return _unavailable("quote", "quote has no timestamp")
    if getattr(quote_ts, "tzinfo", None) is None:
        quote_ts = quote_ts.replace(tzinfo=timezone.utc)
    age = (now - quote_ts).total_seconds()
    if age > MAX_QUOTE_AGE_SEC or age < -MAX_QUOTE_AGE_SEC:
        return GateDecision.blocked(
            "quote_stale", f"quote is {age:.0f}s old (max {MAX_QUOTE_AGE_SEC:.0f}s)")
    spread_pct = (ask - bid) / ask
    if spread_pct > SPREAD_CAP_PCT:
        return GateDecision.blocked(
            "spread",
            f"spread {spread_pct*100:.3f}% exceeds "
            f"{SPREAD_CAP_PCT*100:.1f}% cap")

    return GateDecision.ok()
