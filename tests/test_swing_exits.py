"""Tests for src/swing_exits.py — Phase 3 exit lifecycle.

The stateful fake broker asserts the sell-quantity invariant
(sum of open sell remaining <= position) after EVERY mutation, so any
lifecycle path that would violate it fails immediately, in addition to
the explicit invariant tests."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from alpaca.trading.requests import MarketOrderRequest, StopLimitOrderRequest

from src.execution import ExecConfig, make_client_order_id, make_trade_id
from src.journal import Journal
from src.swing_exits import (
    check_sell_invariant,
    fresh_bid,
    manage_swing_trades,
    open_protected_trade,
    size_position_at_limit,
)

SIGNAL_TS = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
SYM = "SOL/USD"
SYM_NS = "SOLUSD"
OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled",
                 "pending_cancel"}
CONFIG = ExecConfig(fill_poll_timeout_sec=5.0, fill_poll_interval_sec=1.0,
                    cancel_confirm_timeout_sec=5.0)


class _NotFound(Exception):
    status_code = 404


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _is_sell(order) -> bool:
    side = getattr(order, "side", "")
    side = side.value if hasattr(side, "value") else str(side)
    return str(side).lower().endswith("sell")


class Broker:
    """Stateful fake Alpaca. Market orders fill instantly at
    self.prices[symbol] unless hang flags are set."""

    def __init__(self):
        self.positions: dict[str, float] = {}
        self.orders: dict[str, SimpleNamespace] = {}
        self.prices: dict[str, float] = {}
        self.seq = 0
        self.fail_always: set[str] = set()
        self.fail_get_ids: set[str] = set()
        self.fill_on_cancel: dict[str, float] = {}
        self.partial_fill_on_cancel: dict[str, float] = {}
        self.cancel_hang: set[str] = set()
        self.market_sell_hang = False
        self.fail_on_submit_n: int | None = None
        self._submit_count = 0

    # -- invariant: checked after every mutation --------------------------
    def assert_invariant(self):
        symbols = set(self.positions) | {o.symbol for o in self.orders.values()}
        for sym in symbols:
            qty = self.positions.get(sym, 0.0)
            open_sell = sum(
                float(o.qty) - float(o.filled_qty)
                for o in self.orders.values()
                if o.symbol == sym and o.status in OPEN_STATUSES and _is_sell(o))
            assert open_sell <= qty + 1e-9, (
                f"SELL INVARIANT VIOLATED: {sym} open sells {open_sell} > "
                f"position {qty}")

    # -- plumbing ----------------------------------------------------------
    def _maybe_fail(self, name):
        if name in self.fail_always:
            raise RuntimeError(f"{name} unavailable")

    def new_order(self, symbol, side, qty, otype, coid, **extra):
        self.seq += 1
        order = SimpleNamespace(
            id=f"bo-{self.seq}", symbol=symbol, side=side, qty=str(qty),
            filled_qty="0", filled_avg_price=None, status="new",
            order_type=otype, client_order_id=coid, **extra)
        self.orders[order.id] = order
        return order

    def fill(self, order, qty=None, price=None):
        fill_qty = float(order.qty) if qty is None else qty
        order.filled_qty = str(fill_qty)
        order.filled_avg_price = str(
            price if price is not None else self.prices[order.symbol])
        order.status = ("filled" if abs(fill_qty - float(order.qty)) < 1e-12
                        else "partially_filled")
        delta = -fill_qty if _is_sell(order) else fill_qty
        new_qty = self.positions.get(order.symbol, 0.0) + delta
        if new_qty <= 1e-12:
            self.positions.pop(order.symbol, None)
        else:
            self.positions[order.symbol] = new_qty
        self.assert_invariant()

    # -- API surface ---------------------------------------------------------
    def submit_order(self, request):
        self._maybe_fail("submit_order")
        self._submit_count += 1
        if self.fail_on_submit_n == self._submit_count:
            raise RuntimeError("submit rejected (scripted)")
        symbol = str(request.symbol).replace("/", "")
        side = str(request.side).lower()
        coid = getattr(request, "client_order_id", None)
        if isinstance(request, StopLimitOrderRequest):
            order = self.new_order(symbol, request.side, request.qty,
                                   "stop_limit", coid,
                                   stop_price=float(request.stop_price),
                                   limit_price=float(request.limit_price))
            self.assert_invariant()
            return order
        assert isinstance(request, MarketOrderRequest)
        order = self.new_order(symbol, request.side, request.qty, "market",
                               coid)
        if "sell" in side:
            self.assert_invariant()
            if not self.market_sell_hang:
                self.fill(order)
        else:
            self.fill(order)
        return order

    def cancel_order_by_id(self, order_id):
        self._maybe_fail("cancel_order")
        order = self.orders[str(order_id)]
        if order.status == "filled":
            return
        if str(order_id) in self.cancel_hang:
            order.status = "pending_cancel"
            return
        if str(order_id) in self.fill_on_cancel:
            self.fill(order, price=self.fill_on_cancel.pop(str(order_id)))
            return
        if str(order_id) in self.partial_fill_on_cancel:
            qty = self.partial_fill_on_cancel.pop(str(order_id))
            self.fill(order, qty=qty,
                      price=float(getattr(order, "stop_price", 0) or
                                  self.prices[order.symbol]))
            order.status = "canceled"
            self.assert_invariant()
            return
        order.status = "canceled"
        self.assert_invariant()

    def get_order_by_id(self, order_id):
        self._maybe_fail("get_order_by_id")
        if str(order_id) in self.fail_get_ids:
            raise RuntimeError("order read unavailable")
        if str(order_id) not in self.orders:
            raise _NotFound(f"order not found: {order_id}")
        return self.orders[str(order_id)]

    def get_order_by_client_id(self, coid):
        self._maybe_fail("get_order_by_client_id")
        for order in self.orders.values():
            if order.client_order_id == coid:
                return order
        raise _NotFound(f"order not found: {coid}")

    def get_orders(self, filter=None):
        self._maybe_fail("get_orders")
        status = str(getattr(filter, "status", "")).lower()
        symbols = getattr(filter, "symbols", None)
        want_open = "open" in status
        out = []
        for order in self.orders.values():
            is_open = order.status in OPEN_STATUSES
            if is_open != want_open:
                continue
            if symbols and order.symbol not in symbols:
                continue
            out.append(order)
        return out

    def get_open_position(self, symbol):
        self._maybe_fail("get_open_position")
        if symbol not in self.positions:
            raise _NotFound(f"no position: {symbol}")
        return SimpleNamespace(symbol=symbol,
                               qty=str(self.positions[symbol]))

    # -- test helpers ----------------------------------------------------
    def open_sells(self, symbol):
        return [o for o in self.orders.values()
                if o.symbol == symbol and o.status in OPEN_STATUSES
                and _is_sell(o)]


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "j")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def broker():
    b = Broker()
    b.prices[SYM_NS] = 100.05
    return b


def _entry(broker, journal, clock, alerts=None, **overrides):
    kwargs = dict(symbol=SYM, setup="A", signal_ts=SIGNAL_TS,
                  planned_entry=100.0, structural_stop=95.0, equity=5000.0,
                  config=CONFIG,
                  alert_fn=(alerts.append if alerts is not None else lambda m: None),
                  sleep_fn=clock.sleep, clock_fn=clock)
    kwargs.update(overrides)
    return open_protected_trade(broker, journal, **kwargs)


def _seed_protected(journal, broker, qty=5.0, fill_price=100.05,
                    structural=95.0, entry_age_days=0.0):
    """Journal + broker state equivalent to a completed protected entry."""
    trade_id = make_trade_id("swing", SYM, "A", SIGNAL_TS)
    risk_per_unit = fill_price - structural
    entered_at = (datetime.now(timezone.utc)
                  - timedelta(days=entry_age_days)).isoformat()
    journal.record_trade_planned(
        trade_id, symbol=SYM, setup="A", signal_bar_ts=str(SIGNAL_TS),
        planned_entry=100.0, structural_stop=structural,
        stop_limit_price=round(structural * 0.995, 2),
        approved_risk_usd=50.0, actual_fill_qty=qty,
        actual_fill_price=fill_price, actual_risk_per_unit=risk_per_unit,
        actual_risk_dollars=qty * risk_per_unit,
        tp1_price=round(fill_price + 1.5 * risk_per_unit, 2),
        tp2_price=round(fill_price + 3.0 * risk_per_unit, 2),
        tp1_qty=round(qty * 0.5, 4), tp2_qty=round(qty * 0.25, 4),
        entry_filled_at=entered_at, tp_management="application")
    entry_coid = make_client_order_id("swing", SYM, "A", SIGNAL_TS, "entry", 0)
    journal.record_action_intent(trade_id, f"{trade_id}-entry-0", "entry",
                                 client_order_id=entry_coid,
                                 requested_qty=qty)
    journal.record_order_submitted(trade_id, f"{trade_id}-entry-0",
                                   broker_order_id="bo-entry",
                                   status="accepted")
    journal.record_order_state(trade_id, f"{trade_id}-entry-0",
                               broker_order_id="bo-entry", status="filled",
                               filled_qty=qty, avg_fill_price=fill_price)

    broker.positions[SYM_NS] = qty
    stop_coid = make_client_order_id("swing", SYM, "A", SIGNAL_TS, "stop", 0)
    stop = broker.new_order(SYM_NS, "sell", qty, "stop_limit", stop_coid,
                            stop_price=structural,
                            limit_price=round(structural * 0.995, 2))
    journal.record_action_intent(trade_id, f"{trade_id}-stop-0", "stop",
                                 client_order_id=stop_coid,
                                 requested_qty=qty, stop_price=structural)
    journal.record_order_submitted(trade_id, f"{trade_id}-stop-0",
                                   broker_order_id=stop.id, status="new",
                                   requested_qty=qty)
    journal.record_order_state(trade_id, f"{trade_id}-stop-0",
                               broker_order_id=stop.id, status="new",
                               filled_qty=0.0, requested_qty=qty)
    journal.record_state_transition(trade_id, "PROTECTED", "seeded")
    broker.assert_invariant()
    return trade_id, stop


def _manage(broker, journal, clock, *, bid=None, quote_age_sec=0.0,
            regime="BULLISH", runner=None, alerts=None):
    now = datetime.now(timezone.utc)

    def get_quote(symbol):
        if bid is None:
            raise RuntimeError("quote feed down")
        return SimpleNamespace(bid_price=bid,
                               timestamp=now - timedelta(seconds=quote_age_sec))

    return manage_swing_trades(
        broker, journal, config=CONFIG,
        alert_fn=(alerts.append if alerts is not None else lambda m: None),
        get_quote_fn=get_quote, regime_fn=lambda s: regime,
        runner_ctx_fn=lambda s, v: runner, now_fn=lambda: now,
        sleep_fn=clock.sleep, clock_fn=clock)


def _kinds(actions):
    return [a.get("action") for a in actions]


# ===========================================================================
# sizing
# ===========================================================================

def test_sizing_bounded_by_stop_limit_price():
    sizing = size_position_at_limit(5000.0, 100.0, 95.0, SYM, CONFIG)
    assert sizing["skip_reason"] is None
    # Loss at the LIMIT price must stay within approved risk.
    loss_at_limit = sizing["qty"] * (100.0 - sizing["stop_limit_price"])
    assert loss_at_limit <= sizing["approved_risk"] + 1e-6


def test_sizing_respects_structural_caps():
    # 12% stop distance exceeds the strategy's 8% cap.
    sizing = size_position_at_limit(5000.0, 100.0, 88.0, SYM, CONFIG)
    assert sizing["skip_reason"] is not None


# ===========================================================================
# entry
# ===========================================================================

def test_entry_places_exactly_one_verified_stop_and_no_resting_tps(
        journal, broker, clock):
    alerts = []
    result = _entry(broker, journal, clock, alerts)
    assert result["status"] == "protected", result
    sells = broker.open_sells(SYM_NS)
    assert len(sells) == 1
    assert sells[0].order_type == "stop_limit"
    assert float(sells[0].qty) == pytest.approx(broker.positions[SYM_NS])
    # TP1/TP2 are intended levels in the journal, not resting orders.
    view = journal.trades()[result["trade_id"]]
    assert view.plan["tp1_price"] > 0 and view.plan["tp2_price"] > 0
    assert view.state == "PROTECTED"
    assert journal.entry_freeze()["frozen"] is False
    ok, _ = check_sell_invariant(broker, SYM_NS)
    assert ok is True


def test_entry_excess_slippage_unwinds_and_freezes(journal, broker, clock):
    broker.prices[SYM_NS] = 101.0  # 1% slip > 0.5% cap
    alerts = []
    result = _entry(broker, journal, clock, alerts)
    assert result["status"] == "unwound"
    assert SYM_NS not in broker.positions  # flattened
    assert journal.entry_freeze()["frozen"] is True
    assert any("CRITICAL" in a for a in alerts)


def test_entry_journal_down_never_submits(broker, clock, tmp_path):
    class DeadJournal(Journal):
        def append(self, *a, **k):
            return False

    dead = DeadJournal(tmp_path / "dead")
    result = _entry(broker, dead, clock)
    assert result["status"] == "aborted"
    assert broker.orders == {}  # nothing reached the broker


def test_entry_stop_rejection_freezes_with_critical_alert(
        journal, broker, clock):
    broker.fail_on_submit_n = 2  # 1st submit = entry, 2nd = stop
    alerts = []
    result = _entry(broker, journal, clock, alerts)
    assert result["status"] == "recovery_required"
    assert journal.entry_freeze()["frozen"] is True
    assert any("CRITICAL" in a for a in alerts)
    view = journal.trades()[result["trade_id"]]
    assert view.recovery_required is True


def test_entry_duplicate_signal_skipped(journal, broker, clock):
    first = _entry(broker, journal, clock)
    assert first["status"] == "protected"
    orders_before = len(broker.orders)
    second = _entry(broker, journal, clock)
    assert second["status"] == "skipped"
    assert len(broker.orders) == orders_before


# ===========================================================================
# take profits — application managed
# ===========================================================================

def test_tp1_full_sequence(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    broker.prices[SYM_NS] = tp1 + 0.05
    alerts = []
    actions = _manage(broker, journal, clock, bid=tp1 + 0.05, alerts=alerts)
    assert "tp1_execute" in _kinds(actions)
    # Old stop cancelled; exactly one replacement stop for the remainder.
    assert broker.orders[stop.id].status == "canceled"
    sells = broker.open_sells(SYM_NS)
    assert len(sells) == 1
    assert float(sells[0].qty) == pytest.approx(broker.positions[SYM_NS])
    assert broker.positions[SYM_NS] == pytest.approx(2.5)
    # Breakeven rule: replacement stop at avg entry (100.05 > structural).
    assert sells[0].stop_price == pytest.approx(100.05)
    view = journal.trades()[trade_id]
    assert view.state == "TP1_FILLED"
    tp_action = next(a for a in actions if a["action"] == "tp1_execute")
    assert tp_action["unprotected_seconds"] >= 0


def test_tp2_leaves_runner_protected(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    tp2 = journal.trades()[trade_id].plan["tp2_price"]
    broker.prices[SYM_NS] = tp1 + 0.05
    _manage(broker, journal, clock, bid=tp1 + 0.05)
    broker.prices[SYM_NS] = tp2 + 0.05
    actions = _manage(broker, journal, clock, bid=tp2 + 0.05)
    assert "tp2_execute" in _kinds(actions)
    view = journal.trades()[trade_id]
    assert view.state == "TP2_FILLED"
    assert broker.positions[SYM_NS] == pytest.approx(1.25)
    sells = broker.open_sells(SYM_NS)
    assert len(sells) == 1
    assert float(sells[0].qty) == pytest.approx(1.25)


def test_stale_quote_never_triggers_tp(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    actions = _manage(broker, journal, clock, bid=tp1 + 1.0,
                      quote_age_sec=999.0)
    assert "tp1_execute" not in _kinds(actions)
    assert broker.orders[stop.id].status == "new"  # stop preserved


def test_quote_unavailable_keeps_protective_stop(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    actions = _manage(broker, journal, clock, bid=None)
    assert "tp1_execute" not in _kinds(actions)
    assert broker.orders[stop.id].status == "new"
    assert journal.trades()[trade_id].state == "PROTECTED"


def test_dust_remainder_closes_entire_position(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker, qty=2.50005)
    # tp1_qty = 1.25 (half, rounded); position after tranche would be
    # ~1.25005... craft instead: intended tranche nearly everything.
    view = journal.trades()[trade_id]
    tp1 = view.plan["tp1_price"]
    # Rewrite the plan so the intended TP1 tranche leaves dust.
    journal.record_trade_planned(trade_id, **{**view.plan,
                                              "tp1_qty": 2.5})
    broker.prices[SYM_NS] = tp1 + 0.05
    actions = _manage(broker, journal, clock, bid=tp1 + 0.05)
    tp_action = next(a for a in actions if a["action"] == "tp1_execute")
    # Leftover 0.00005 < SOL increment 1e-4 — everything was sold.
    assert tp_action.get("position_closed") is True
    assert SYM_NS not in broker.positions
    assert broker.open_sells(SYM_NS) == []
    assert journal.trades()[trade_id].is_terminal()


def test_tp_cancellation_race_stop_filled_closes_trade(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    broker.fill_on_cancel[stop.id] = 94.9  # stop fills as we cancel it
    actions = _manage(broker, journal, clock, bid=tp1 + 0.05)
    assert "stop_filled" in _kinds(actions)
    assert SYM_NS not in broker.positions
    view = journal.trades()[trade_id]
    assert view.is_terminal()
    assert any(e["reason"] == "STOP_PARTIAL_ON_CANCEL" for e in view.exits)


def test_partial_stop_fill_during_cancel_is_recorded(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    broker.partial_fill_on_cancel[stop.id] = 2.0
    broker.prices[SYM_NS] = tp1 + 0.05
    actions = _manage(broker, journal, clock, bid=tp1 + 0.05)
    assert "tp1_execute" in _kinds(actions)
    view = journal.trades()[trade_id]
    reasons = [e["reason"] for e in view.exits]
    assert "STOP_PARTIAL_ON_CANCEL" in reasons and "TP1" in reasons
    # 5.0 - 2.0 partial-stop - 2.5 tranche = 0.5 runner, exactly protected.
    assert broker.positions[SYM_NS] == pytest.approx(0.5)
    sells = broker.open_sells(SYM_NS)
    assert len(sells) == 1
    assert float(sells[0].qty) == pytest.approx(0.5)


# ===========================================================================
# gap watchdog
# ===========================================================================

def test_gap_watchdog_emergency_close(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    broker.prices[SYM_NS] = 94.5  # gapped through the 95.0 trigger
    alerts = []
    actions = _manage(broker, journal, clock, bid=94.5, alerts=alerts)
    assert "emergency_close" in _kinds(actions)
    assert SYM_NS not in broker.positions
    assert broker.orders[stop.id].status == "canceled"
    assert any("CRITICAL" in a for a in alerts)
    view = journal.trades()[trade_id]
    assert view.is_terminal()
    assert view.realized_r is not None and view.realized_r < 0


def test_gap_watchdog_cancel_hang_freezes_without_conflicting_order(
        journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    broker.cancel_hang.add(stop.id)
    alerts = []
    actions = _manage(broker, journal, clock, bid=94.5, alerts=alerts)
    assert "recovery_required" in _kinds(actions)
    # No market sell was submitted while the stop state was unresolved.
    market_sells = [o for o in broker.orders.values()
                    if o.order_type == "market" and _is_sell(o)]
    assert market_sells == []
    assert journal.entry_freeze()["frozen"] is True
    assert journal.trades()[trade_id].recovery_required is True
    assert any("CRITICAL" in a for a in alerts)


def test_unknown_stop_state_freezes_without_new_orders(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    broker.fail_get_ids.add(stop.id)
    orders_before = len(broker.orders)
    actions = _manage(broker, journal, clock, bid=100.0)
    assert "recovery_required" in _kinds(actions)
    assert len(broker.orders) == orders_before  # nothing new submitted
    assert journal.entry_freeze()["frozen"] is True


# ===========================================================================
# stop fills, regime exit, time stop, runner
# ===========================================================================

def test_stop_filled_while_away_closes_trade_with_realized_r(
        journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    broker.fill(stop, price=94.7)  # stop executed between passes
    actions = _manage(broker, journal, clock, bid=94.7)
    assert "stop_filled" in _kinds(actions)
    view = journal.trades()[trade_id]
    assert view.is_terminal()
    assert view.realized_r == pytest.approx(
        (94.7 - 100.05) / (100.05 - 95.0), rel=1e-6)


def test_regime_exit_closes_position(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    actions = _manage(broker, journal, clock, bid=100.0, regime="BEARISH")
    assert "mgmt_close" in _kinds(actions)
    assert SYM_NS not in broker.positions
    assert journal.trades()[trade_id].is_terminal()


def test_unknown_regime_does_not_exit(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    actions = _manage(broker, journal, clock, bid=100.0, regime=None)
    assert "mgmt_close" not in _kinds(actions)
    assert SYM_NS in broker.positions


def test_time_stop_closes_after_ten_days_without_tp1(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker, entry_age_days=11.0)
    actions = _manage(broker, journal, clock, bid=100.0)
    assert "mgmt_close" in _kinds(actions)
    assert SYM_NS not in broker.positions


def test_trail_raise_replaces_stop_and_never_lowers(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    tp2 = journal.trades()[trade_id].plan["tp2_price"]
    broker.prices[SYM_NS] = tp1 + 0.05
    _manage(broker, journal, clock, bid=tp1 + 0.05)
    broker.prices[SYM_NS] = tp2 + 0.05
    _manage(broker, journal, clock, bid=tp2 + 0.05)
    # Runner: trail = hwm - 2*atr = 130 - 2*5 = 120 > current stop 100.05.
    # Bid stays above the new trail or the gap watchdog would (rightly)
    # take over on the next pass.
    actions = _manage(broker, journal, clock, bid=125.0,
                      runner={"close": 125.0, "ema20": 110.0, "atr14": 5.0,
                              "hwm": 130.0})
    assert "trail_raise" in _kinds(actions)
    sells = broker.open_sells(SYM_NS)
    assert len(sells) == 1
    assert sells[0].stop_price == pytest.approx(120.0)
    # A lower trail must NOT replace the stop.
    actions = _manage(broker, journal, clock, bid=125.0,
                      runner={"close": 125.0, "ema20": 110.0, "atr14": 20.0,
                              "hwm": 130.0})
    assert "trail_raise" not in _kinds(actions)
    assert broker.open_sells(SYM_NS)[0].stop_price == pytest.approx(120.0)


def test_runner_exits_on_ema_break(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    tp2 = journal.trades()[trade_id].plan["tp2_price"]
    broker.prices[SYM_NS] = tp1 + 0.05
    _manage(broker, journal, clock, bid=tp1 + 0.05)
    broker.prices[SYM_NS] = tp2 + 0.05
    _manage(broker, journal, clock, bid=tp2 + 0.05)
    broker.prices[SYM_NS] = 108.0
    actions = _manage(broker, journal, clock, bid=108.0,
                      runner={"close": 108.0, "ema20": 110.0, "atr14": 5.0,
                              "hwm": 130.0})
    assert "mgmt_close" in _kinds(actions)
    assert SYM_NS not in broker.positions
    assert journal.trades()[trade_id].is_terminal()


# ===========================================================================
# crash / exception matrix (fake-client failures + restart recovery)
# ===========================================================================

def test_crash_before_tp_sell_freezes_then_holds(journal, broker, clock):
    # Failure AFTER confirmed stop cancellation, BEFORE the TP sell:
    # the position is unprotected -> recovery + freeze; the next pass
    # must hold (no duplicate/conflicting orders).
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    broker.fail_on_submit_n = 1  # the tranche market sell fails
    actions = _manage(broker, journal, clock, bid=tp1 + 0.05)
    assert "recovery_required" in _kinds(actions)
    assert journal.entry_freeze()["frozen"] is True
    orders_before = len(broker.orders)
    second = _manage(broker, journal, clock, bid=tp1 + 0.05)
    # The pass first resolves the failed sell's dangling intent, then
    # holds — crucially, WITHOUT submitting any new broker order.
    assert "recovery_hold" in _kinds(second)
    assert len(broker.orders) == orders_before


def test_crash_after_tp_submit_before_persistence_recovers(journal, broker,
                                                           clock):
    # The sell reached Alpaca but the API call raised: the deterministic
    # client order ID finds it on the retry inside the same pass.
    trade_id, stop = _seed_protected(journal, broker)
    tp1 = journal.trades()[trade_id].plan["tp1_price"]
    broker.prices[SYM_NS] = tp1 + 0.05
    original_submit = broker.submit_order
    calls = {"n": 0}

    def flaky_submit(request):
        order = original_submit(request)
        if isinstance(request, MarketOrderRequest) and _is_sell(order):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("read timed out after submit")
        return order

    broker.submit_order = flaky_submit
    actions = _manage(broker, journal, clock, bid=tp1 + 0.05)
    assert "tp1_execute" in _kinds(actions)
    # Exactly ONE tranche sell happened despite the exception.
    market_sells = [o for o in broker.orders.values()
                    if o.order_type == "market" and _is_sell(o)]
    assert len(market_sells) == 1
    assert broker.positions[SYM_NS] == pytest.approx(2.5)


def test_restart_after_tp_fill_repairs_state_instead_of_reselling(
        journal, broker, clock):
    # Crash after the tranche filled but before exits/transitions were
    # persisted: journal still says PROTECTED, broker already sold the
    # tranche. The next pass must repair state, not sell again.
    trade_id, stop = _seed_protected(journal, broker)
    view = journal.trades()[trade_id]
    tp1 = view.plan["tp1_price"]
    # Simulate the crashed pass: stop cancelled, tranche sold, journal
    # got the order states but no EXIT_REALIZED / state transition.
    broker.orders[stop.id].status = "canceled"
    tp_coid = make_client_order_id("swing", SYM, "A", SIGNAL_TS, "tp1", 0)
    tp_order = broker.new_order(SYM_NS, "sell", 2.5, "market", tp_coid)
    broker.fill(tp_order, price=tp1)
    journal.record_order_state(trade_id, f"{trade_id}-stop-0",
                               broker_order_id=stop.id, status="canceled",
                               filled_qty=0.0)
    journal.record_action_intent(trade_id, f"{trade_id}-tp1-0", "tp1",
                                 client_order_id=tp_coid, requested_qty=2.5)
    journal.record_order_submitted(trade_id, f"{trade_id}-tp1-0",
                                   broker_order_id=tp_order.id,
                                   status="filled")
    journal.record_order_state(trade_id, f"{trade_id}-tp1-0",
                               broker_order_id=tp_order.id, status="filled",
                               filled_qty=2.5, avg_fill_price=tp1)

    actions = _manage(broker, journal, clock, bid=tp1 + 0.05)
    # Same pass: re-protects the bare position, then repairs TP1 state.
    assert "reprotect" in _kinds(actions)
    assert "tp1_state_repaired" in _kinds(actions)
    view = journal.trades()[trade_id]
    assert view.state == "TP1_FILLED"
    assert any(e["reason"] == "TP1" for e in view.exits)
    # No second tranche sell.
    market_sells = [o for o in broker.orders.values()
                    if o.order_type == "market" and _is_sell(o)]
    assert len(market_sells) == 1
    sells = broker.open_sells(SYM_NS)
    assert len(sells) == 1
    assert float(sells[0].qty) == pytest.approx(2.5)
    # The recovery reprotect used the structural stop (state was still
    # PROTECTED at that moment). Post-repair, the strategy's breakeven
    # rule applies — the next pass must raise the stop to avg entry.
    actions = _manage(broker, journal, clock, bid=100.0)
    assert "breakeven_enforce" in _kinds(actions)
    sells = broker.open_sells(SYM_NS)
    assert len(sells) == 1
    assert float(sells[0].qty) == pytest.approx(2.5)
    assert sells[0].stop_price == pytest.approx(100.05)


def test_unresolved_intent_recovery_before_management(journal, broker, clock):
    # Restart with an intent persisted but no broker response (crash
    # between submit and persistence) — the pass resolves it first.
    trade_id, stop = _seed_protected(journal, broker)
    tp_coid = make_client_order_id("swing", SYM, "A", SIGNAL_TS, "tp1", 0)
    journal.record_action_intent(trade_id, f"{trade_id}-tp1-0", "tp1",
                                 client_order_id=tp_coid, requested_qty=2.5)
    actions = _manage(broker, journal, clock, bid=100.0)
    recovery = next(a for a in actions if a["action"] == "intent_recovery")
    assert recovery["outcome"] == "not_submitted"  # broker never saw it


def test_emergency_close_sell_hang_goes_recovery(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    broker.market_sell_hang = True
    alerts = []
    actions = _manage(broker, journal, clock, bid=94.5, alerts=alerts)
    assert "recovery_required" in _kinds(actions)
    assert journal.entry_freeze()["frozen"] is True
    assert journal.trades()[trade_id].recovery_required is True


def test_position_unreadable_goes_recovery(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    broker.fail_always.add("get_open_position")
    actions = _manage(broker, journal, clock, bid=100.0)
    assert "recovery_required" in _kinds(actions)
    assert journal.entry_freeze()["frozen"] is True


# ===========================================================================
# invariant + scoping
# ===========================================================================

def test_check_sell_invariant_detects_violation(broker):
    # The OLD bundle design: stop 100% + TP1 50% + TP2 25% resting at
    # once = 175% of the position. The invariant must flag it.
    broker.positions[SYM_NS] = 4.0
    broker.new_order(SYM_NS, "sell", 4.0, "stop_limit", "x-stop",
                     stop_price=95.0, limit_price=94.5)
    broker.new_order(SYM_NS, "sell", 2.0, "limit", "x-tp1")
    broker.new_order(SYM_NS, "sell", 1.0, "limit", "x-tp2")
    ok, detail = check_sell_invariant(broker, SYM_NS)
    assert ok is False
    assert "7.0" in detail


def test_invariant_uses_remaining_not_requested(broker):
    broker.positions[SYM_NS] = 2.0
    stop = broker.new_order(SYM_NS, "sell", 5.0, "stop_limit", "x-stop",
                            stop_price=95.0, limit_price=94.5)
    stop.filled_qty = "3.0"
    stop.status = "partially_filled"
    ok, _ = check_sell_invariant(broker, SYM_NS)
    assert ok is True  # remaining 2.0 == position 2.0


def test_management_never_touches_day_strand_orders(journal, broker, clock):
    trade_id, stop = _seed_protected(journal, broker)
    broker.positions["NVDA"] = 30.0
    day_order = broker.new_order("NVDA", "sell", 30.0, "stop", "DAY-A-NVDA-1")
    _manage(broker, journal, clock, bid=100.0)
    assert broker.orders[day_order.id].status == "new"
    assert broker.positions["NVDA"] == 30.0


def test_fresh_bid_rejects_stale_and_bad_quotes():
    now = datetime.now(timezone.utc)
    good = lambda s: SimpleNamespace(bid_price=100.0, timestamp=now)
    stale = lambda s: SimpleNamespace(bid_price=100.0,
                                      timestamp=now - timedelta(minutes=10))
    zero = lambda s: SimpleNamespace(bid_price=0.0, timestamp=now)
    broken = lambda s: (_ for _ in ()).throw(RuntimeError("down"))
    assert fresh_bid(good, SYM, now_fn=lambda: now)["bid"] == 100.0
    assert fresh_bid(stale, SYM, now_fn=lambda: now) is None
    assert fresh_bid(zero, SYM, now_fn=lambda: now) is None
    assert fresh_bid(broken, SYM, now_fn=lambda: now) is None
