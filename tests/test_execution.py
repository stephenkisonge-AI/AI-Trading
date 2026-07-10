"""Tests for src/execution.py — Phase 2 idempotency, partial fills, and
fill-based risk. All timing is driven by a fake clock (sleep advances
it), so every timeout/race scenario is deterministic."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.execution import (
    ExecConfig,
    find_existing_order,
    load_exec_config,
    make_client_order_id,
    make_trade_id,
    next_leg,
    persist_submit_verify,
    resolve_entry_fill,
    resolve_unresolved_intent,
    unwind_trade,
    validate_entry_fill,
    wait_for_terminal,
)
from src.journal import Journal


SIGNAL_TS = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _NotFound(Exception):
    status_code = 404


class _BrokerDown(Exception):
    status_code = 503


def _order(id="o1", status="new", filled_qty="0", qty="1",
           filled_avg_price=None, client_order_id="coid-1",
           symbol="BTCUSD", side="OrderSide.BUY",
           order_type="OrderType.MARKET"):
    return SimpleNamespace(id=id, status=status, filled_qty=filled_qty,
                           qty=qty, filled_avg_price=filled_avg_price,
                           client_order_id=client_order_id, symbol=symbol,
                           side=side, order_type=order_type)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeClient:
    """Scriptable broker. get_order_by_id pops from a per-order script
    (last entry repeats); submit_order registers orders by coid."""

    def __init__(self):
        self.orders_by_coid = {}
        self.scripts = {}          # order_id -> [order states in sequence]
        self.open_orders = []
        self.closed_orders = []
        self.position_scripts = {} # symbol -> [position | Exception]
        self.submitted = []
        self.cancelled = []
        self.fail_get_by_coid = None
        self.fail_get_orders = None

    # --- lookups ---
    def get_order_by_client_id(self, coid):
        if self.fail_get_by_coid is not None:
            raise self.fail_get_by_coid
        if coid in self.orders_by_coid:
            return self.orders_by_coid[coid]
        raise _NotFound(f"order not found: {coid}")

    def get_order_by_id(self, order_id):
        script = self.scripts.get(str(order_id))
        if script:
            return script.pop(0) if len(script) > 1 else script[0]
        raise _NotFound(f"order not found: {order_id}")

    def get_orders(self, filter=None):
        if self.fail_get_orders is not None:
            raise self.fail_get_orders
        status = str(getattr(filter, "status", "")).lower()
        return list(self.open_orders if "open" in status else self.closed_orders)

    def get_open_position(self, symbol):
        script = self.position_scripts.get(symbol)
        if not script:
            raise _NotFound(f"no position: {symbol}")
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        return item

    # --- mutations ---
    def submit_order(self, request):
        coid = getattr(request, "client_order_id", None) or f"auto-{len(self.submitted)}"
        order = _order(id=f"bo-{len(self.submitted) + 1}", status="accepted",
                       qty=str(getattr(request, "qty", 1)),
                       client_order_id=coid,
                       symbol=str(getattr(request, "symbol", "")).replace("/", ""))
        self.submitted.append(order)
        self.orders_by_coid[coid] = order
        self.scripts.setdefault(order.id, [order])
        return order

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(str(order_id))


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "j")


@pytest.fixture
def clock():
    return FakeClock()


def _fast_config(**overrides):
    defaults = dict(fill_poll_timeout_sec=3.0, fill_poll_interval_sec=1.0,
                    cancel_confirm_timeout_sec=3.0)
    defaults.update(overrides)
    return ExecConfig(**defaults)


# ---------------------------------------------------------------------------
# deterministic IDs
# ---------------------------------------------------------------------------

def test_client_order_id_format_and_determinism():
    a = make_client_order_id("swing", "BTC/USD", "A", SIGNAL_TS, "entry", 0)
    b = make_client_order_id("swing", "BTC/USD", "A", SIGNAL_TS, "entry", 0)
    assert a == "swing-BTCUSD-A-20260710T120000Z-entry-0"
    assert a == b  # retry produces the identical ID
    assert "/" not in a
    assert len(a) <= 48


def test_client_order_id_replacement_leg_differs():
    stop0 = make_client_order_id("swing", "BTC/USD", "A", SIGNAL_TS, "stop", 0)
    stop1 = make_client_order_id("swing", "BTC/USD", "A", SIGNAL_TS, "stop", 1)
    assert stop0 != stop1
    assert stop0.endswith("-stop-0") and stop1.endswith("-stop-1")


def test_client_order_id_naive_ts_treated_as_utc():
    naive = datetime(2026, 7, 10, 12, 0, 0)
    assert make_client_order_id("swing", "ETH/USD", "B", naive, "tp1", 0) \
        == "swing-ETHUSD-B-20260710T120000Z-tp1-0"


def test_client_order_id_rejects_bad_inputs():
    with pytest.raises(ValueError):
        make_client_order_id("swing", "BTC/USD", "A", SIGNAL_TS, "yolo", 0)
    with pytest.raises(ValueError):
        make_client_order_id("swing", "BTC/USD", "A", SIGNAL_TS, "entry", -1)
    with pytest.raises(ValueError):  # over 48 chars
        make_client_order_id("swing" * 6, "BTC/USD", "A", SIGNAL_TS, "entry", 0)


def test_day_strand_canonical_id_fits_limit():
    # Addendum B: the day strand adopts the canonical scheme with
    # strand="day" during the equity mini-audit. Longest realistic
    # combination must fit Alpaca's 48-char limit.
    bar_ts = datetime(2026, 7, 10, 13, 35, 0, tzinfo=timezone.utc)
    coid = make_client_order_id("day", "GOOGL", "BS", bar_ts, "entry", 0)
    assert coid == "day-GOOGL-BS-20260710T133500Z-entry-0"
    assert len(coid) <= 48


def test_next_leg_counts_prior_intents(journal):
    trade_id = make_trade_id("swing", "BTC/USD", "A", SIGNAL_TS)
    assert next_leg(journal, trade_id, "stop") == 0
    journal.record_action_intent(trade_id, f"{trade_id}-stop-0", "stop",
                                 client_order_id="x-stop-0")
    assert next_leg(journal, trade_id, "stop") == 1
    assert next_leg(journal, trade_id, "tp1") == 0


# ---------------------------------------------------------------------------
# validated config
# ---------------------------------------------------------------------------

def test_config_defaults_are_valid():
    config = load_exec_config({})
    assert config.stop_limit_offset_pct == 0.005


def test_config_env_overrides():
    config = load_exec_config({"STOP_LIMIT_OFFSET_PCT": "0.01",
                               "MAX_ACTUAL_RISK_TOLERANCE": "0.2"})
    assert config.stop_limit_offset_pct == 0.01
    assert config.max_actual_risk_tolerance == 0.2


@pytest.mark.parametrize("env", [
    {"STOP_LIMIT_OFFSET_PCT": "0"},        # offset must be > 0
    {"STOP_LIMIT_OFFSET_PCT": "0.10"},     # > 5% cap
    {"STOP_LIMIT_OFFSET_PCT": "banana"},   # not a number
    {"MAX_ENTRY_SLIPPAGE_PCT": "-0.01"},
    {"MAX_ACTUAL_RISK_TOLERANCE": "0.9"},
])
def test_config_rejects_invalid_values(env):
    with pytest.raises(ValueError):
        load_exec_config(env)


# ---------------------------------------------------------------------------
# pre-submission idempotency (scenarios 1-4)
# ---------------------------------------------------------------------------

def _submit_args(journal, coid="swing-BTCUSD-A-20260710T120000Z-entry-0"):
    trade_id = make_trade_id("swing", "BTC/USD", "A", SIGNAL_TS)
    return dict(trade_id=trade_id, action_id=f"{trade_id}-entry-0",
                client_order_id=coid, action="entry",
                intent={"requested_qty": 0.005},
                symbol_no_slash="BTCUSD")


def test_duplicate_workflow_execution_submits_once(journal):
    # Two runs of the same logical entry (duplicate workflow execution):
    # the second must find the first's order and NOT submit again.
    client = FakeClient()
    args = _submit_args(journal)
    first = persist_submit_verify(
        client, journal,
        submit_fn=lambda: client.submit_order(
            SimpleNamespace(client_order_id=args["client_order_id"],
                            qty=0.005, symbol="BTC/USD")),
        **args)
    assert first.status == "submitted"
    second = persist_submit_verify(
        client, journal,
        submit_fn=lambda: client.submit_order(
            SimpleNamespace(client_order_id=args["client_order_id"],
                            qty=0.005, symbol="BTC/USD")),
        **args)
    assert second.status == "already_exists"
    assert len(client.submitted) == 1  # duplicate submission prevented


def test_filled_order_found_by_client_order_id(journal):
    client = FakeClient()
    args = _submit_args(journal)
    client.orders_by_coid[args["client_order_id"]] = _order(
        status="filled", filled_qty="0.005", filled_avg_price="100100",
        client_order_id=args["client_order_id"])
    outcome = persist_submit_verify(
        client, journal, submit_fn=lambda: pytest.fail("must not submit"),
        **args)
    assert outcome.status == "already_exists"
    assert outcome.order.status == "filled"


def test_canceled_order_found_by_client_order_id(journal):
    client = FakeClient()
    args = _submit_args(journal)
    client.orders_by_coid[args["client_order_id"]] = _order(
        status="canceled", client_order_id=args["client_order_id"])
    outcome = persist_submit_verify(
        client, journal, submit_fn=lambda: pytest.fail("must not submit"),
        **args)
    assert outcome.status == "already_exists"
    assert outcome.order.status == "canceled"


def test_broker_unreachable_blocks_submission(journal):
    # Preflight can't establish state -> ambiguous -> nothing submitted.
    client = FakeClient()
    client.fail_get_by_coid = _BrokerDown("api down")
    client.fail_get_orders = _BrokerDown("api down")
    args = _submit_args(journal)
    outcome = persist_submit_verify(
        client, journal, submit_fn=lambda: pytest.fail("must not submit"),
        **args)
    assert outcome.status == "ambiguous"


def test_journal_contradicting_broker_is_unknown(journal):
    # Journal says submitted; broker has no such order -> do not resubmit.
    args = _submit_args(journal)
    journal.record_action_intent(args["trade_id"], args["action_id"],
                                 "entry",
                                 client_order_id=args["client_order_id"])
    journal.record_order_submitted(args["trade_id"], args["action_id"],
                                   broker_order_id="bo-ghost",
                                   status="accepted")
    client = FakeClient()  # broker 404s on the coid
    preflight = find_existing_order(client, journal,
                                    args["client_order_id"], "BTCUSD")
    assert preflight.status == "unknown"


def test_intent_persistence_failure_blocks_submission(journal, tmp_path):
    class BrokenJournal(Journal):
        def record_action_intent(self, *a, **k):
            return False

    broken = BrokenJournal(tmp_path / "broken")
    client = FakeClient()
    args = _submit_args(broken)
    outcome = persist_submit_verify(
        client, broken, submit_fn=lambda: pytest.fail("must not submit"),
        **args)
    assert outcome.status == "intent_not_persisted"
    assert client.submitted == []


def test_submit_exception_but_order_landed_recovers(journal):
    # The API call raised, yet the order reached Alpaca. The retry check
    # by deterministic coid must find it instead of resubmitting.
    client = FakeClient()
    args = _submit_args(journal)

    def flaky_submit():
        client.orders_by_coid[args["client_order_id"]] = _order(
            status="accepted", client_order_id=args["client_order_id"])
        raise TimeoutError("read timed out")

    outcome = persist_submit_verify(client, journal, submit_fn=flaky_submit,
                                    **args)
    assert outcome.status == "already_exists"


def test_submit_exception_nothing_landed(journal):
    client = FakeClient()
    args = _submit_args(journal)

    def failing_submit():
        raise RuntimeError("rejected at the door")

    outcome = persist_submit_verify(client, journal, submit_fn=failing_submit,
                                    **args)
    assert outcome.status == "submit_failed"


# ---------------------------------------------------------------------------
# restart recovery (scenarios 13-15)
# ---------------------------------------------------------------------------

def test_restart_after_submission_before_response_persistence(journal):
    # Crash between broker submit and ORDER_SUBMITTED persistence:
    # journal holds only the intent; the broker holds the order.
    args = _submit_args(journal)
    journal.record_action_intent(args["trade_id"], args["action_id"],
                                 "entry",
                                 client_order_id=args["client_order_id"],
                                 requested_qty=0.005)
    client = FakeClient()
    client.orders_by_coid[args["client_order_id"]] = _order(
        id="bo-lost", status="accepted",
        client_order_id=args["client_order_id"])

    assert resolve_unresolved_intent(client, journal, args["trade_id"],
                                     args["action_id"]) == "recovered"
    view = journal.trades()[args["trade_id"]]
    assert view.unresolved_intents() == []
    assert view.actions[args["action_id"]]["submitted"]["broker_order_id"] == "bo-lost"


def test_restart_intent_never_reached_broker(journal):
    args = _submit_args(journal)
    journal.record_action_intent(args["trade_id"], args["action_id"],
                                 "entry",
                                 client_order_id=args["client_order_id"])
    client = FakeClient()  # broker has nothing
    assert resolve_unresolved_intent(client, journal, args["trade_id"],
                                     args["action_id"]) == "not_submitted"
    assert journal.trades()[args["trade_id"]].unresolved_intents() == []


def test_restart_after_partial_fill(journal):
    args = _submit_args(journal)
    journal.record_action_intent(args["trade_id"], args["action_id"],
                                 "entry",
                                 client_order_id=args["client_order_id"],
                                 requested_qty=0.005)
    client = FakeClient()
    client.orders_by_coid[args["client_order_id"]] = _order(
        id="bo-p", status="partially_filled", filled_qty="0.002", qty="0.005",
        filled_avg_price="100000", client_order_id=args["client_order_id"])
    assert resolve_unresolved_intent(client, journal, args["trade_id"],
                                     args["action_id"]) == "recovered"
    view = journal.trades()[args["trade_id"]]
    assert view.entry_filled_qty() == pytest.approx(0.002)


def test_restart_after_confirmed_fill_is_idempotent(journal):
    args = _submit_args(journal)
    journal.record_action_intent(args["trade_id"], args["action_id"],
                                 "entry",
                                 client_order_id=args["client_order_id"],
                                 requested_qty=0.005)
    journal.record_order_submitted(args["trade_id"], args["action_id"],
                                   broker_order_id="bo-1", status="accepted")
    journal.record_order_state(args["trade_id"], args["action_id"],
                               broker_order_id="bo-1", status="filled",
                               filled_qty=0.005, avg_fill_price=100100.0)
    client = FakeClient()
    client.orders_by_coid[args["client_order_id"]] = _order(
        id="bo-1", status="filled", filled_qty="0.005", qty="0.005",
        filled_avg_price="100100", client_order_id=args["client_order_id"])
    assert resolve_unresolved_intent(client, journal, args["trade_id"],
                                     args["action_id"]) == "recovered"
    view = journal.trades()[args["trade_id"]]
    assert view.entry_filled_qty() == pytest.approx(0.005)  # unchanged


# ---------------------------------------------------------------------------
# fill resolution (scenarios 5-9)
# ---------------------------------------------------------------------------

def _resolve(client, journal, clock, order, config=None):
    return resolve_entry_fill(
        client, journal,
        trade_id="T1", action_id="T1-entry-0", order=order,
        client_order_id=order.client_order_id,
        config=config or _fast_config(),
        sleep_fn=clock.sleep, clock_fn=clock)


def test_delayed_fill_resolves_filled(journal, clock):
    client = FakeClient()
    pending = _order(id="bo-1", status="new")
    filled = _order(id="bo-1", status="filled", filled_qty="0.005",
                    filled_avg_price="100100")
    client.scripts["bo-1"] = [pending, pending, filled]
    result = _resolve(client, journal, clock, pending)
    assert result.status == "filled"
    assert result.filled_qty == pytest.approx(0.005)
    assert result.avg_price == pytest.approx(100100.0)


def test_fill_after_timeout_detected(journal, clock):
    # Order fills between the last poll and the post-timeout re-read.
    # Rule 15: never assume the filled quantity is zero.
    client = FakeClient()
    pending = _order(id="bo-1", status="new")
    filled = _order(id="bo-1", status="filled", filled_qty="0.005",
                    filled_avg_price="100200")
    client.scripts["bo-1"] = [pending, pending, pending, filled]
    result = _resolve(client, journal, clock, pending)
    assert result.status == "filled"
    assert result.filled_qty == pytest.approx(0.005)
    assert client.cancelled == []  # already terminal — nothing to cancel


def test_partially_filled_entry_cancels_remainder(journal, clock):
    # Still working at timeout with a partial fill: cancel the
    # remainder, wait for the terminal state, report the EXACT qty.
    client = FakeClient()
    partial = _order(id="bo-1", status="partially_filled",
                     filled_qty="0.002", qty="0.005")
    canceled = _order(id="bo-1", status="canceled", filled_qty="0.002",
                      qty="0.005", filled_avg_price="100000")
    client.scripts["bo-1"] = [partial, partial, partial, partial, canceled]
    result = _resolve(client, journal, clock, partial)
    assert client.cancelled == ["bo-1"]
    assert result.status == "partial"
    assert result.filled_qty == pytest.approx(0.002)


def test_cancellation_race_fill_wins(journal, clock):
    # Cancel requested after timeout, but the order FILLED before the
    # cancel took effect. Rule 17: trust only the terminal state.
    client = FakeClient()
    working = _order(id="bo-1", status="new", qty="0.005")
    filled = _order(id="bo-1", status="filled", filled_qty="0.005",
                    qty="0.005", filled_avg_price="100300")
    client.scripts["bo-1"] = [working, working, working, working, filled]
    result = _resolve(client, journal, clock, working)
    assert client.cancelled == ["bo-1"]
    assert result.status == "filled"
    assert result.filled_qty == pytest.approx(0.005)


def test_cancel_never_terminal_marks_recovery(journal, clock):
    # The cancel request never reaches a terminal state: the trade is
    # recovery-required and the result is unknown (entries freeze).
    client = FakeClient()
    stuck = _order(id="bo-1", status="pending_cancel", qty="0.005",
                   filled_qty="0.001")
    client.scripts["bo-1"] = [stuck]
    result = _resolve(client, journal, clock, stuck)
    assert result.status == "unknown"
    view = journal.trades()["T1"]
    assert view.recovery_required is True


def test_unknown_broker_status_treated_as_nonterminal_then_recovers(journal, clock):
    # A status we don't recognize is never assumed terminal; after the
    # cancel path it still can't terminalize -> recovery required.
    client = FakeClient()
    weird = _order(id="bo-1", status="quantum_flux", qty="0.005")
    client.scripts["bo-1"] = [weird]
    result = _resolve(client, journal, clock, weird)
    assert result.status == "unknown"
    assert journal.trades()["T1"].recovery_required is True


def test_order_unreadable_after_timeout_marks_recovery(journal, clock):
    client = FakeClient()  # no scripts: every read raises
    ghost = _order(id="bo-ghost", status="new")
    result = _resolve(client, journal, clock, ghost)
    assert result.status == "unknown"
    assert journal.trades()["T1"].recovery_required is True


def test_wait_for_terminal_reports_last_seen_order(clock):
    client = FakeClient()
    working = _order(id="bo-1", status="new")
    client.scripts["bo-1"] = [working]
    reached, last = wait_for_terminal(client, "bo-1", timeout_sec=2.0,
                                      poll_sec=1.0, sleep_fn=clock.sleep,
                                      clock_fn=clock)
    assert reached is False
    assert last is working


# ---------------------------------------------------------------------------
# fill-based risk validation (scenarios 10-12)
# ---------------------------------------------------------------------------

# approved_risk_dollars is sized against the stop-LIMIT price (Phase 3
# sizing rule), so it carries headroom beyond the trigger-price loss.
_PLAN = dict(planned_entry=100000.0, structural_stop=95000.0,
             approved_risk_dollars=26.0, tp1_r=1.5, tp2_r=3.0,
             min_qty_increment=1e-6)


def test_validation_happy_path_computes_from_actual_fill():
    config = ExecConfig()
    v = validate_entry_fill(filled_qty=0.005, avg_fill_price=100100.0,
                            config=config, **_PLAN)
    assert v.ok, v.reasons
    assert v.actual_risk_per_unit == pytest.approx(5100.0)
    assert v.actual_risk_dollars == pytest.approx(25.5)
    assert v.actual_stop_distance_pct == pytest.approx(5100.0 / 100100.0)
    assert v.tp1 == pytest.approx(100100.0 + 5100.0 * 1.5)
    assert v.tp2 == pytest.approx(100100.0 + 5100.0 * 3.0)


def test_excess_entry_slippage_rejected():
    config = ExecConfig(max_entry_slippage_pct=0.005)
    v = validate_entry_fill(filled_qty=0.005, avg_fill_price=100600.0,
                            config=config, **_PLAN)  # 0.6% slip
    assert not v.ok
    assert any("slippage" in r for r in v.reasons)


def test_excess_actual_risk_rejected():
    config = ExecConfig(max_actual_risk_tolerance=0.10)
    v = validate_entry_fill(filled_qty=0.006, avg_fill_price=100100.0,
                            config=config, **_PLAN)
    # 0.006 * 5100 = $30.6 > 26 * 1.1 = $28.6
    assert not v.ok
    assert any("actual risk" in r for r in v.reasons)


def test_invalid_structural_stop_rejected():
    config = ExecConfig()
    plan = dict(_PLAN, structural_stop=100500.0)  # stop above the fill
    v = validate_entry_fill(filled_qty=0.005, avg_fill_price=100100.0,
                            config=config, **plan)
    assert not v.ok
    assert any("structural stop invalid" in r for r in v.reasons)


def test_unprotectable_dust_qty_rejected():
    config = ExecConfig()
    plan = dict(_PLAN, min_qty_increment=1e-3)
    v = validate_entry_fill(filled_qty=1e-4, avg_fill_price=100100.0,
                            config=config, **plan)
    assert not v.ok
    assert any("cannot be safely protected" in r for r in v.reasons)


def test_loss_at_stop_limit_price_bounds_risk():
    # The limit price (stop minus offset) is where the loss is actually
    # bounded; a wide offset can push modeled loss past the tolerance
    # even when the trigger-price loss looks fine.
    config = ExecConfig(stop_limit_offset_pct=0.05,
                        max_actual_risk_tolerance=0.10)
    v = validate_entry_fill(filled_qty=0.005, avg_fill_price=100100.0,
                            config=config, **_PLAN)
    # loss at limit = 0.005 * (100100 - 95000*0.95) = $49.25 > $27.5
    assert not v.ok
    assert any("stop-limit" in r for r in v.reasons)


# ---------------------------------------------------------------------------
# emergency unwind
# ---------------------------------------------------------------------------

def _seed_filled_trade(journal, trade_id):
    journal.record_trade_planned(trade_id, symbol="BTC/USD",
                                 planned_entry=100000.0,
                                 structural_stop=95000.0)
    journal.record_action_intent(trade_id, f"{trade_id}-entry-0", "entry",
                                 client_order_id=f"{trade_id}-entry-0",
                                 requested_qty=0.006)
    journal.record_order_submitted(trade_id, f"{trade_id}-entry-0",
                                   broker_order_id="bo-e", status="accepted")
    journal.record_order_state(trade_id, f"{trade_id}-entry-0",
                               broker_order_id="bo-e", status="filled",
                               filled_qty=0.006, avg_fill_price=100100.0)


def test_unwind_flattens_and_freezes(journal, clock):
    trade_id = make_trade_id("swing", "BTC/USD", "A", SIGNAL_TS)
    _seed_filled_trade(journal, trade_id)
    client = FakeClient()
    resting_stop = _order(id="bo-s", status="new", qty="0.006",
                          side="OrderSide.SELL",
                          order_type="OrderType.STOP_LIMIT",
                          client_order_id="x-stop-0")
    client.open_orders = [resting_stop]
    client.scripts["bo-s"] = [_order(id="bo-s", status="canceled",
                                     qty="0.006")]
    client.position_scripts["BTCUSD"] = [
        SimpleNamespace(symbol="BTCUSD", qty="0.006"),
        _NotFound("flat"),
    ]
    alerts = []

    def fake_submit(request):
        order = FakeClient.submit_order(client, request)
        client.scripts[order.id] = [_order(
            id=order.id, status="filled", filled_qty=str(request.qty),
            qty=str(request.qty), filled_avg_price="100050",
            client_order_id=request.client_order_id)]
        return order

    client.submit_order = fake_submit
    out = unwind_trade(client, journal, trade_id=trade_id, symbol="BTC/USD",
                       strand="swing", setup="A", signal_ts=SIGNAL_TS,
                       reason="actual risk exceeds approved",
                       alert_fn=alerts.append, config=_fast_config(),
                       sleep_fn=clock.sleep, clock_fn=clock)
    assert out["closed"] is True
    assert out["cancelled"] == ["bo-s"]
    assert client.cancelled == ["bo-s"]
    # The market sell was for the EXACT remaining broker quantity.
    close_orders = [o for o in client.submitted]
    assert len(close_orders) == 1
    assert float(close_orders[0].qty) == pytest.approx(0.006)
    assert journal.entry_freeze()["frozen"] is True
    assert any("CRITICAL" in a for a in alerts)
    assert journal.trades()[trade_id].is_terminal()


def test_unwind_unconfirmed_cancel_goes_recovery(journal, clock):
    trade_id = make_trade_id("swing", "BTC/USD", "A", SIGNAL_TS)
    _seed_filled_trade(journal, trade_id)
    client = FakeClient()
    stuck = _order(id="bo-s", status="pending_cancel", qty="0.006",
                   side="OrderSide.SELL", order_type="OrderType.STOP_LIMIT")
    client.open_orders = [stuck]
    client.scripts["bo-s"] = [stuck]  # never terminalizes
    alerts = []
    out = unwind_trade(client, journal, trade_id=trade_id, symbol="BTC/USD",
                       strand="swing", setup="A", signal_ts=SIGNAL_TS,
                       reason="excess risk", alert_fn=alerts.append,
                       config=_fast_config(), sleep_fn=clock.sleep,
                       clock_fn=clock)
    assert out["closed"] is False
    assert out["error"] is not None
    view = journal.trades()[trade_id]
    assert view.recovery_required is True
    assert journal.entry_freeze()["frozen"] is True
    assert any("CRITICAL" in a for a in alerts)
    assert client.submitted == []  # no sell while state is unresolved
