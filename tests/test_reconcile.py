"""Tests for src/reconciliation.py — the ten Phase 1 scenarios plus
cross-strand scoping. All broker objects are SimpleNamespace fakes in
the same style as tests/test_trader.py."""
from types import SimpleNamespace

import pytest

from src.journal import Journal
from src.reconciliation import reconcile


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

def _position(symbol="BTCUSD", qty="0.005"):
    return SimpleNamespace(symbol=symbol, qty=qty, avg_entry_price="100100")


def _order(id="bo-stop-1", symbol="BTCUSD", side="OrderSide.SELL",
           status="new", order_type="OrderType.STOP_LIMIT",
           qty="0.005", filled_qty="0",
           client_order_id="swing-BTCUSD-A-20260710T120000Z-stop-0"):
    return SimpleNamespace(id=id, symbol=symbol, side=side, status=status,
                           order_type=order_type, qty=qty,
                           filled_qty=filled_qty,
                           client_order_id=client_order_id)


def _entry_order(status="filled", filled_qty="0.005"):
    return _order(id="bo-entry-1", side="OrderSide.BUY", status=status,
                  order_type="OrderType.MARKET", qty="0.005",
                  filled_qty=filled_qty,
                  client_order_id="swing-BTCUSD-A-20260710T120000Z-entry-0")


def _seed_protected_trade(journal, trade_id="T1", symbol="BTC/USD",
                          filled_qty=0.005):
    journal.record_signal(trade_id, symbol=symbol, setup="A")
    journal.record_trade_planned(trade_id, symbol=symbol,
                                 planned_entry=100000.0,
                                 structural_stop=95000.0)
    journal.record_action_intent(
        trade_id, f"{trade_id}-entry-0", "entry",
        client_order_id="swing-BTCUSD-A-20260710T120000Z-entry-0",
        requested_qty=filled_qty)
    journal.record_order_submitted(trade_id, f"{trade_id}-entry-0",
                                   broker_order_id="bo-entry-1",
                                   status="accepted")
    journal.record_order_state(trade_id, f"{trade_id}-entry-0",
                               broker_order_id="bo-entry-1",
                               status="filled", filled_qty=filled_qty,
                               avg_fill_price=100100.0)
    journal.record_action_intent(
        trade_id, f"{trade_id}-stop-0", "stop",
        client_order_id="swing-BTCUSD-A-20260710T120000Z-stop-0",
        requested_qty=filled_qty)
    journal.record_order_submitted(trade_id, f"{trade_id}-stop-0",
                                   broker_order_id="bo-stop-1",
                                   status="accepted")
    journal.record_order_state(trade_id, f"{trade_id}-stop-0",
                               broker_order_id="bo-stop-1",
                               status="new", filled_qty=0.0)
    journal.record_state_transition(trade_id, "PROTECTED", "stop verified")


def _codes(report):
    return {finding.code for finding in report.findings}


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "j")


# ---------------------------------------------------------------------------
# the ten mandated scenarios
# ---------------------------------------------------------------------------

def test_1_empty_journal_empty_broker_reconciles(journal):
    report = reconcile(journal, positions=[], open_orders=[], recent_orders=[])
    assert report.ok
    assert "RECONCILED" in report.render()


def test_2_broker_position_missing_from_journal(journal):
    report = reconcile(journal, positions=[_position()], open_orders=[],
                       recent_orders=[])
    assert "POSITION_NOT_IN_JOURNAL" in _codes(report)
    assert not report.ok


def test_3_journal_trade_missing_from_broker(journal):
    _seed_protected_trade(journal)
    report = reconcile(journal, positions=[], open_orders=[],
                       recent_orders=[])
    assert "JOURNAL_TRADE_NOT_AT_BROKER" in _codes(report)


def test_4_orphan_open_order(journal):
    _seed_protected_trade(journal)
    orphan = _order(id="bo-mystery", client_order_id="manual-order-123")
    report = reconcile(journal, positions=[_position()],
                       open_orders=[_order(), orphan],
                       recent_orders=[_entry_order()])
    assert "ORPHAN_OPEN_ORDER" in _codes(report)


def test_5_duplicate_client_order_id(journal):
    _seed_protected_trade(journal)
    dup_a = _order(id="bo-stop-1")
    dup_b = _order(id="bo-stop-2")  # same client_order_id, different order
    report = reconcile(journal, positions=[_position()],
                       open_orders=[dup_a, dup_b],
                       recent_orders=[_entry_order()])
    assert "DUPLICATE_CLIENT_ORDER_ID" in _codes(report)


def test_6_quantity_mismatch(journal):
    _seed_protected_trade(journal, filled_qty=0.005)
    report = reconcile(journal, positions=[_position(qty="0.003")],
                       open_orders=[_order(qty="0.003")],
                       recent_orders=[_entry_order()])
    assert "QTY_MISMATCH" in _codes(report)


def test_7_unknown_broker_status(journal):
    _seed_protected_trade(journal)
    weird = _order(status="quantum_flux")
    report = reconcile(journal, positions=[_position()],
                       open_orders=[weird],
                       recent_orders=[_entry_order()])
    assert "UNKNOWN_ORDER_STATUS" in _codes(report)


def test_8_partially_filled_order_detected(journal):
    # Journal believes the entry filled 0.005; the broker says it only
    # partially filled 0.002. Rule 15: a timeout never means zero fill —
    # reconciliation must surface the divergence.
    _seed_protected_trade(journal, filled_qty=0.005)
    partial_entry = _entry_order(status="partially_filled",
                                 filled_qty="0.002")
    report = reconcile(journal, positions=[_position(qty="0.002")],
                       open_orders=[_order(qty="0.002")],
                       recent_orders=[partial_entry])
    codes = _codes(report)
    assert "STALE_ORDER_STATE" in codes
    assert "QTY_MISMATCH" in codes


def test_9_unprotected_position(journal):
    _seed_protected_trade(journal)
    # Position exists but there is NO open stop order at the broker.
    report = reconcile(journal, positions=[_position()],
                       open_orders=[],
                       recent_orders=[_entry_order()])
    assert "UNPROTECTED_POSITION" in _codes(report)


def test_10_correctly_reconciled_position(journal):
    _seed_protected_trade(journal)
    report = reconcile(journal, positions=[_position()],
                       open_orders=[_order()],
                       recent_orders=[_entry_order()])
    assert report.ok, report.render()


# ---------------------------------------------------------------------------
# additional safety scenarios
# ---------------------------------------------------------------------------

def test_recovery_required_trade_blocks(journal):
    _seed_protected_trade(journal)
    journal.record_recovery_required("T1", "cancel state unknown")
    report = reconcile(journal, positions=[_position()],
                       open_orders=[_order()],
                       recent_orders=[_entry_order()])
    assert "RECOVERY_REQUIRED" in _codes(report)


def test_unresolved_intent_blocks(journal):
    journal.record_action_intent(
        "T9", "T9-entry-0", "entry",
        client_order_id="swing-ETHUSD-A-20260710T160000Z-entry-0",
        requested_qty=0.1)
    report = reconcile(journal, positions=[], open_orders=[],
                       recent_orders=[])
    assert "UNRESOLVED_INTENT" in _codes(report)


def test_sell_qty_invariant_violation(journal):
    # The old exit-bundle design: stop 100% + TP1 50% + TP2 25% = 175%
    # of the position in simultaneous resting sells. Must be flagged.
    _seed_protected_trade(journal)
    stop = _order()
    tp1 = _order(id="bo-tp1", order_type="OrderType.LIMIT", qty="0.0025",
                 client_order_id="swing-BTCUSD-A-20260710T120000Z-tp1-0")
    tp2 = _order(id="bo-tp2", order_type="OrderType.LIMIT", qty="0.00125",
                 client_order_id="swing-BTCUSD-A-20260710T120000Z-tp2-0")
    journal.record_action_intent("T1", "T1-tp1-0", "tp1",
                                 client_order_id=tp1.client_order_id,
                                 requested_qty=0.0025)
    journal.record_order_submitted("T1", "T1-tp1-0",
                                   broker_order_id="bo-tp1", status="accepted")
    journal.record_order_state("T1", "T1-tp1-0", broker_order_id="bo-tp1",
                               status="new", filled_qty=0.0)
    journal.record_action_intent("T1", "T1-tp2-0", "tp2",
                                 client_order_id=tp2.client_order_id,
                                 requested_qty=0.00125)
    journal.record_order_submitted("T1", "T1-tp2-0",
                                   broker_order_id="bo-tp2", status="accepted")
    journal.record_order_state("T1", "T1-tp2-0", broker_order_id="bo-tp2",
                               status="new", filled_qty=0.0)
    report = reconcile(journal, positions=[_position()],
                       open_orders=[stop, tp1, tp2],
                       recent_orders=[_entry_order()])
    assert "SELL_QTY_INVARIANT" in _codes(report)


def test_sell_invariant_uses_remaining_not_requested_qty(journal):
    # A stop that has partially filled: requested 0.005, filled 0.003 →
    # remaining 0.002. With the position also reduced to 0.002 the
    # invariant holds — using the ORIGINAL requested qty would be a
    # false positive (rule 14 explicitly requires remaining qty).
    _seed_protected_trade(journal)
    journal.record_exit("T1", qty=0.003, price=95000.0, reason="STOP partial")
    journal.record_order_state("T1", "T1-stop-0", broker_order_id="bo-stop-1",
                               status="partially_filled", filled_qty=0.003)
    partial_stop = _order(status="partially_filled", qty="0.005",
                          filled_qty="0.003")
    report = reconcile(journal, positions=[_position(qty="0.002")],
                       open_orders=[partial_stop],
                       recent_orders=[_entry_order()])
    assert "SELL_QTY_INVARIANT" not in _codes(report)
    assert report.ok, report.render()


def test_terminal_trades_expect_no_broker_presence(journal):
    _seed_protected_trade(journal)
    journal.record_exit("T1", qty=0.005, price=95000.0, reason="STOP")
    journal.record_trade_closed("T1", realized_r=-1.0, reason="stopped out")
    report = reconcile(journal, positions=[], open_orders=[],
                       recent_orders=[])
    assert report.ok, report.render()


def test_day_strand_equities_are_out_of_scope(journal):
    # The day-trade strand shares the paper account. Its NVDA position
    # and orders must be invisible to crypto reconciliation.
    nvda_position = _position(symbol="NVDA", qty="30")
    nvda_order = _order(id="bo-day-1", symbol="NVDA",
                        client_order_id="DAY-A-NVDA-1751900000")
    report = reconcile(journal, positions=[nvda_position],
                       open_orders=[nvda_order], recent_orders=[])
    assert report.ok, report.render()


def test_entry_freeze_reported_but_not_a_mismatch(journal):
    journal.set_entry_freeze(True, "testing freeze")
    report = reconcile(journal, positions=[], open_orders=[],
                       recent_orders=[])
    assert report.ok
    assert "ACTIVE" in report.render()
