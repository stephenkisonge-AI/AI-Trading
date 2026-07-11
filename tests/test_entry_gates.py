"""Tests for src/entry_gates.py — Phase 4 fail-closed gates and
Addendum C per-strand risk ledgers. Each mandated failure mode gets its
own independent test proving the entry is BLOCKED (never fail-open),
and that management/alerts continue while entries are frozen."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.entry_gates import (
    BLOCKED_UNAVAILABLE,
    evaluate_entry_gates,
    strand_drawdown,
    strand_realized_pl,
)
from src.journal import Journal


NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


class _Failing:
    def __init__(self, *names):
        self.names = set(names)

    def __getattr__(self, name):
        if name in self.names:
            def boom(*a, **k):
                raise RuntimeError(f"{name} unavailable")
            return boom
        raise AttributeError(name)


class FakeAccountClient:
    """Healthy broker by default; break individual calls per test."""

    def __init__(self, equity=10000.0, buying_power=10000.0, positions=(),
                 open_orders=()):
        self.equity = equity
        self.buying_power = buying_power
        self.positions = list(positions)
        self.open_orders = list(open_orders)
        self.fail: set[str] = set()

    def get_account(self):
        if "account" in self.fail:
            raise RuntimeError("account endpoint down")
        return SimpleNamespace(equity=str(self.equity),
                               buying_power=str(self.buying_power))

    def get_all_positions(self):
        if "positions" in self.fail:
            raise RuntimeError("positions endpoint down")
        return list(self.positions)

    def get_orders(self, filter=None):
        if "orders" in self.fail:
            raise RuntimeError("orders endpoint down")
        return list(self.open_orders)


def _position(symbol="ETHUSD", qty="1.0", unrealized_pl="0"):
    return SimpleNamespace(symbol=symbol, qty=qty,
                           unrealized_pl=unrealized_pl)


def _good_quote(symbol):
    return SimpleNamespace(bid_price=100.0, ask_price=100.1, timestamp=NOW)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "j")


def _gates(journal, client, *, symbol="BTC/USD", quote_fn=_good_quote,
           history_fn=None):
    return evaluate_entry_gates(
        journal=journal, client=client, symbol=symbol,
        get_quote_fn=quote_fn, now_fn=lambda: NOW,
        portfolio_history_fn=history_fn)


def _seed_closed_trade(journal, trade_id, *, fill_price, exit_price, qty,
                       exit_ts):
    """A closed trade whose single exit realizes qty*(exit-fill)."""
    journal.record_trade_planned(trade_id, symbol="SOL/USD",
                                 actual_fill_price=fill_price,
                                 actual_fill_qty=qty,
                                 actual_risk_per_unit=5.0)
    # Backdate the exit event by writing it then rewriting its ts.
    journal.record_exit(trade_id, qty=qty, price=exit_price, reason="STOP")
    events = journal.events()
    events[-1]["ts"] = exit_ts.isoformat()
    with open(journal.events_path, "w", encoding="utf-8") as fh:
        import json
        for event in events:
            fh.write(json.dumps(event, default=str) + "\n")
    journal.record_trade_closed(trade_id, realized_r=-1.0, reason="stop")


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_all_gates_pass_on_healthy_state(journal):
    decision = _gates(journal, FakeAccountClient(),
                      history_fn=lambda: [10000.0, 10100.0])
    assert decision.allowed, str(decision)


# ---------------------------------------------------------------------------
# mandated fail-closed scenarios — each blocks INDEPENDENTLY
# ---------------------------------------------------------------------------

def test_no_journal_blocks_entry():
    decision = _gates(None, FakeAccountClient())
    assert not decision.allowed
    assert BLOCKED_UNAVAILABLE in decision.reason


def test_quote_failure_blocks_entry(journal):
    def broken(symbol):
        raise RuntimeError("quote feed down")
    decision = _gates(journal, FakeAccountClient(), quote_fn=broken)
    assert not decision.allowed
    assert decision.gate == "quote"
    assert BLOCKED_UNAVAILABLE in decision.reason


def test_stale_quote_blocks_entry(journal):
    def stale(symbol):
        return SimpleNamespace(bid_price=100.0, ask_price=100.1,
                               timestamp=NOW - timedelta(minutes=10))
    decision = _gates(journal, FakeAccountClient(), quote_fn=stale)
    assert not decision.allowed
    assert decision.gate == "quote_stale"


def test_quote_without_timestamp_blocks_entry(journal):
    def no_ts(symbol):
        return SimpleNamespace(bid_price=100.0, ask_price=100.1,
                               timestamp=None)
    decision = _gates(journal, FakeAccountClient(), quote_fn=no_ts)
    assert not decision.allowed


def test_invalid_quote_blocks_entry(journal):
    def crossed(symbol):
        return SimpleNamespace(bid_price=100.2, ask_price=100.0,
                               timestamp=NOW)
    decision = _gates(journal, FakeAccountClient(), quote_fn=crossed)
    assert not decision.allowed
    assert decision.gate == "quote"


def test_wide_spread_blocks_entry(journal):
    def wide(symbol):
        return SimpleNamespace(bid_price=99.0, ask_price=100.0, timestamp=NOW)
    decision = _gates(journal, FakeAccountClient(), quote_fn=wide)
    assert not decision.allowed
    assert decision.gate == "spread"


def test_account_failure_blocks_entry(journal):
    client = FakeAccountClient()
    client.fail.add("account")
    decision = _gates(journal, client)
    assert not decision.allowed
    assert decision.gate == "account"
    assert BLOCKED_UNAVAILABLE in decision.reason


def test_position_failure_blocks_entry(journal):
    client = FakeAccountClient()
    client.fail.add("positions")
    decision = _gates(journal, client)
    assert not decision.allowed
    assert decision.gate == "positions"
    assert BLOCKED_UNAVAILABLE in decision.reason


def test_open_order_failure_blocks_entry(journal):
    client = FakeAccountClient()
    client.fail.add("orders")
    decision = _gates(journal, client)
    assert not decision.allowed
    assert decision.gate == "open_orders"
    assert BLOCKED_UNAVAILABLE in decision.reason


def test_missing_unrealized_field_blocks_entry(journal):
    client = FakeAccountClient(
        positions=[SimpleNamespace(symbol="SOLUSD", qty="1",
                                   unrealized_pl=None)])
    decision = _gates(journal, client)
    assert not decision.allowed
    assert decision.gate == "unrealized"


def test_account_drawdown_history_failure_blocks_entry(journal):
    def broken_history():
        raise RuntimeError("portfolio history down")
    decision = _gates(journal, FakeAccountClient(), history_fn=broken_history)
    assert not decision.allowed
    assert decision.gate == "account_drawdown"
    assert BLOCKED_UNAVAILABLE in decision.reason


# ---------------------------------------------------------------------------
# rule-based blocks
# ---------------------------------------------------------------------------

def test_standing_freeze_blocks_until_reconciliation(journal):
    journal.set_entry_freeze(True, "sell invariant violated")
    decision = _gates(journal, FakeAccountClient())
    assert not decision.allowed
    assert decision.gate == "entry_freeze"
    assert "reconcil" in decision.reason.lower()
    # Only a clean reconciliation lifts it — simulate that.
    journal.set_entry_freeze(False, "reconciliation passed clean")
    assert _gates(journal, FakeAccountClient(),
                  history_fn=lambda: [10000.0]).allowed


def test_max_positions_blocks(journal):
    client = FakeAccountClient(positions=[_position("ETHUSD"),
                                          _position("SOLUSD")])
    decision = _gates(journal, client, symbol="LINK/USD")
    assert not decision.allowed
    assert decision.gate == "max_positions"


def test_already_held_blocks(journal):
    client = FakeAccountClient(positions=[_position("BTCUSD")])
    decision = _gates(journal, client, symbol="BTC/USD")
    assert not decision.allowed
    assert decision.gate == "already_held"


def test_btc_eth_correlation_blocks(journal):
    client = FakeAccountClient(positions=[_position("ETHUSD")])
    decision = _gates(journal, client, symbol="BTC/USD")
    assert not decision.allowed
    assert decision.gate == "correlation"


def test_day_strand_positions_do_not_consume_crypto_slots(journal):
    # NVDA/TSLA belong to the day strand — invisible to crypto gates.
    client = FakeAccountClient(positions=[
        SimpleNamespace(symbol="NVDA", qty="30", unrealized_pl="0"),
        SimpleNamespace(symbol="TSLA", qty="20", unrealized_pl="0")])
    decision = _gates(journal, client, history_fn=lambda: [10000.0])
    assert decision.allowed, str(decision)


def test_open_order_conflict_blocks(journal):
    client = FakeAccountClient(open_orders=[
        SimpleNamespace(symbol="BTCUSD", side="sell")])
    decision = _gates(journal, client, symbol="BTC/USD")
    assert not decision.allowed
    assert decision.gate == "open_order_conflict"


def test_zero_buying_power_blocks(journal):
    decision = _gates(journal, FakeAccountClient(buying_power=0.0))
    assert not decision.allowed
    assert decision.gate == "buying_power"


def test_daily_entry_cap_blocks_second_entry(journal):
    journal.record_action_intent("T1", "T1-entry-0", "entry",
                                 client_order_id="swing-x-entry-0")
    journal.record_order_state("T1", "T1-entry-0",
                               broker_order_id="bo", status="filled",
                               filled_qty=0.5)
    decision = _gates(journal, FakeAccountClient(),
                      history_fn=lambda: [10000.0])
    assert not decision.allowed
    assert decision.gate == "daily_entry_cap"


# ---------------------------------------------------------------------------
# per-strand risk ledgers (Addendum C)
# ---------------------------------------------------------------------------

def test_strand_daily_loss_blocks_only_this_strand(journal, tmp_path):
    # Crypto strand realized -$250 today on $10k equity (-2.5% > 2% cap).
    _seed_closed_trade(journal, "T-loss", fill_price=100.0, exit_price=50.0,
                       qty=5.0, exit_ts=NOW - timedelta(hours=2))
    decision = _gates(journal, FakeAccountClient())
    assert not decision.allowed
    assert decision.gate == "daily_loss"

    # The DAY strand's journal is a separate subtree — same loss event
    # does not exist there, so day entries are unaffected (structural
    # isolation, not filtering).
    day_journal = Journal(journal.root, strand="day")
    assert day_journal.events() == []
    day_decision = evaluate_entry_gates(
        journal=day_journal, client=FakeAccountClient(), symbol="NVDA",
        get_quote_fn=_good_quote, universe={"NVDA", "TSLA"},
        now_fn=lambda: NOW, portfolio_history_fn=lambda: [10000.0])
    assert day_decision.allowed, str(day_decision)


def test_strand_weekly_loss_blocks(journal):
    # -$520 loss three days ago: outside today, inside this week
    # (NOW is a Saturday — 2026-07-11).
    _seed_closed_trade(journal, "T-weekly", fill_price=100.0,
                       exit_price=48.0, qty=10.0,
                       exit_ts=NOW - timedelta(days=3))
    decision = _gates(journal, FakeAccountClient())
    assert not decision.allowed
    assert decision.gate == "weekly_loss"


def test_strand_drawdown_blocks(journal):
    # +$2000 gain then -$1200 loss within the window: cumulative curve
    # peaks at +2000, drops to +800 → drawdown -1200 (-12% of $10k).
    _seed_closed_trade(journal, "T-win", fill_price=100.0, exit_price=300.0,
                       qty=10.0, exit_ts=NOW - timedelta(days=20))
    _seed_closed_trade(journal, "T-lose", fill_price=100.0, exit_price=40.0,
                       qty=20.0, exit_ts=NOW - timedelta(days=10))
    assert strand_drawdown(journal, 30, NOW) == pytest.approx(-1200.0)
    decision = _gates(journal, FakeAccountClient())
    assert not decision.allowed
    assert decision.gate == "strand_drawdown"


def test_unrealized_loss_counts_toward_daily_cap(journal):
    client = FakeAccountClient(positions=[
        _position("SOLUSD", unrealized_pl="-250.0")])
    decision = _gates(journal, client, symbol="BTC/USD")
    assert not decision.allowed
    assert decision.gate == "daily_loss"


def test_account_emergency_brake_freezes_all_strands(journal, tmp_path):
    history = [10000.0, 9000.0, 8000.0]  # -20% from peak
    decision = _gates(journal, FakeAccountClient(equity=8000.0),
                      history_fn=lambda: history)
    assert not decision.allowed
    assert decision.gate == "account_emergency_brake"
    # Same brake blocks the day strand too — shared last resort.
    day_journal = Journal(tmp_path / "j2", strand="day")
    day_decision = evaluate_entry_gates(
        journal=day_journal, client=FakeAccountClient(equity=8000.0),
        symbol="NVDA", get_quote_fn=_good_quote, universe={"NVDA"},
        now_fn=lambda: NOW, portfolio_history_fn=lambda: history)
    assert not day_decision.allowed
    assert day_decision.gate == "account_emergency_brake"


def test_realized_pl_helper(journal):
    _seed_closed_trade(journal, "T1", fill_price=100.0, exit_price=110.0,
                       qty=2.0, exit_ts=NOW - timedelta(hours=1))
    assert strand_realized_pl(journal, NOW - timedelta(days=1)) == pytest.approx(20.0)
    assert strand_realized_pl(journal, NOW + timedelta(hours=1)) == 0.0


# ---------------------------------------------------------------------------
# management continues while entries are frozen
# ---------------------------------------------------------------------------

def test_management_and_alerts_continue_while_frozen(journal, tmp_path):
    """A standing entry freeze must not stop position management,
    risk-reducing exits, or alerts (proven on the real management pass
    from swing_exits with a live position + gap scenario)."""
    from datetime import datetime as dt
    from tests.test_swing_exits import (
        Broker, FakeClock, _seed_protected, _manage, SYM_NS)

    broker = Broker()
    broker.prices[SYM_NS] = 100.05
    swing_journal = Journal(tmp_path / "mgmt", strand="swing")
    trade_id, stop = _seed_protected(swing_journal, broker)
    swing_journal.set_entry_freeze(True, "reconcile mismatch elsewhere")

    # Entry side: blocked.
    decision = _gates(swing_journal, FakeAccountClient())
    assert not decision.allowed
    assert decision.gate == "entry_freeze"

    # Management side: the gap watchdog still closes the position and
    # still alerts (risk REDUCTION is never gated).
    broker.prices[SYM_NS] = 94.5
    clock = FakeClock()
    alerts = []
    actions = _manage(broker, swing_journal, clock, bid=94.5, alerts=alerts)
    assert "emergency_close" in [a.get("action") for a in actions]
    assert SYM_NS not in broker.positions
    assert any("CRITICAL" in a for a in alerts)
