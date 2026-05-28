"""Tests for src/day_trader.py — sizing math, gates, and entry-bundle
placement against an in-memory fake TradingClient.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.day_trader import (
    SkipDecision,
    check_pre_execution_gates,
    compute_position_size,
    day_auto_execute_enabled,
    place_entry_bundle,
)


# ---------------------------------------------------------------------------
# day_auto_execute_enabled
# ---------------------------------------------------------------------------


def test_auto_execute_disabled_when_flag_unset(monkeypatch):
    monkeypatch.delenv("WATCHER_DAY_AUTO_EXECUTE", raising=False)
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "True")
    assert day_auto_execute_enabled() is False


def test_auto_execute_disabled_when_flag_false(monkeypatch):
    monkeypatch.setenv("WATCHER_DAY_AUTO_EXECUTE", "false")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "True")
    assert day_auto_execute_enabled() is False


def test_auto_execute_requires_paper_mode(monkeypatch):
    # Live mode must hard-refuse auto-execute even if the kill switch
    # is on. This is the two-switches-in-two-files contract.
    monkeypatch.setenv("WATCHER_DAY_AUTO_EXECUTE", "true")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "False")
    assert day_auto_execute_enabled() is False


def test_auto_execute_enabled_when_both_flags_set(monkeypatch):
    monkeypatch.setenv("WATCHER_DAY_AUTO_EXECUTE", "true")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "True")
    assert day_auto_execute_enabled() is True


# ---------------------------------------------------------------------------
# compute_position_size
# ---------------------------------------------------------------------------


def test_sizing_capped_at_500_when_calculated_higher():
    # equity 100k, 0.5% risk = $500. Entry 100, stop 99 → stop_dist 1%.
    # Required notional = 500 / 0.01 = $50,000 → capped to $500.
    out = compute_position_size(equity=100_000, entry=100.0, stop=99.0)
    assert out["shares"] == 5
    assert out["notional"] == 500.0


def test_sizing_uses_risk_dollars_when_below_cap():
    # equity 5000 → risk = $25. Entry 100, stop 95 → 5% stop_dist.
    # Required notional = 25 / 0.05 = $500 → exactly cap.
    out = compute_position_size(equity=5_000, entry=100.0, stop=99.0)
    # entry 100, stop 99 → stop_dist 1%, risk = $25, needed = $2500, cap = $500 → 5 shares.
    assert out["shares"] == 5
    # Re-test with wider stop where notional < cap.
    out2 = compute_position_size(equity=2_000, entry=100.0, stop=97.0)
    # risk = $10, stop_dist 3%, needed = $10 / 0.03 = $333.33, cap = $500, accept.
    assert out2["shares"] == 3


def test_sizing_rejects_stop_too_tight():
    # Stop dist 0.2% < 0.3% min → reject.
    out = compute_position_size(equity=100_000, entry=100.0, stop=99.8)
    assert out["shares"] == 0
    assert "stop_too_tight" in out["skip_reason"]


def test_sizing_rejects_stop_too_wide():
    # Stop dist 4% > 3% max → reject.
    out = compute_position_size(equity=100_000, entry=100.0, stop=96.0)
    assert out["shares"] == 0
    assert "stop_too_wide" in out["skip_reason"]


def test_sizing_rejects_when_under_50_floor():
    # equity small enough that risk × 0.5% × 1/stop_dist < $50.
    # equity=10, risk=$0.05, stop_dist=1%, needed=$5 < $50 → reject.
    out = compute_position_size(equity=10, entry=100.0, stop=99.0)
    assert out["shares"] == 0
    assert "notional_below_50_floor" in out["skip_reason"]


def test_sizing_rejects_fractional_under_one_share():
    # High-priced share on $500 cap: needs entry > $500 to floor to 0
    # shares, but stop_dist must still be in [0.3%, 3%] to avoid the
    # earlier gates. Entry $600, stop $594 → 1% stop_dist, notional
    # capped at $500 → floor(500/600) = 0 shares.
    out = compute_position_size(equity=100_000, entry=600.0, stop=594.0)
    assert out["shares"] == 0
    assert "fractional_under_one_share" in out["skip_reason"]


def test_sizing_rejects_invalid_entry_or_stop():
    # stop >= entry is nonsense — must reject.
    out = compute_position_size(equity=100_000, entry=100.0, stop=100.0)
    assert out["shares"] == 0
    assert out["skip_reason"] == "invalid_entry_or_stop"
    out2 = compute_position_size(equity=100_000, entry=100.0, stop=101.0)
    assert out2["skip_reason"] == "invalid_entry_or_stop"


# ---------------------------------------------------------------------------
# check_pre_execution_gates
# ---------------------------------------------------------------------------


class FakeClient:
    """In-memory TradingClient stub. Records orders submitted and returns
    a configurable account / orders list."""

    def __init__(self, equity=100_000, last_equity=100_000, orders=(), fill_price=None):
        self.equity = equity
        self.last_equity = last_equity
        self._orders = list(orders)
        self.submitted: list = []
        self.fill_price = fill_price
        self._order_seq = 0
        self._order_states: dict[str, str] = {}

    # --- Alpaca-compatible API surface ---

    def get_account(self):
        return SimpleNamespace(
            equity=str(self.equity), last_equity=str(self.last_equity)
        )

    def get_orders(self, filter=None):
        return list(self._orders)

    def submit_order(self, request):
        self._order_seq += 1
        order_id = f"ord-{self._order_seq}"
        side = (
            getattr(request, "side", None).value
            if hasattr(getattr(request, "side", None), "value")
            else getattr(request, "side", None)
        )
        rec = SimpleNamespace(
            id=order_id,
            side=str(side),
            status="accepted",
            symbol=getattr(request, "symbol", None),
            qty=getattr(request, "qty", None),
            filled_avg_price=None,
            filled_at=None,
        )
        # Market BUY auto-fills for the test path.
        is_market_buy = (
            request.__class__.__name__ == "MarketOrderRequest"
            and str(side).lower().endswith("buy")
        )
        if is_market_buy and self.fill_price is not None:
            rec.filled_avg_price = str(self.fill_price)
            # Production code stringifies status and checks .endswith("filled")
            # — that works against the real alpaca-py OrderStatus enum (which
            # stringifies to "OrderStatus.FILLED"). Use a string literal in
            # the fake.
            rec.status = "OrderStatus.FILLED"
            rec.filled_at = datetime(2026, 5, 27, 14, 30, tzinfo=timezone.utc)
        self.submitted.append((request, rec))
        return rec

    def get_order_by_id(self, order_id):
        # Look up the most recent submission by id.
        for req, rec in self.submitted:
            if rec.id == order_id:
                return rec
        raise KeyError(order_id)


def _setup_result():
    return {
        "setup": "A", "symbol": "NVDA", "qualified": True,
        "conditions": [], "entry": 100.0, "stop": 99.0,
        "atr": 0.5, "tp1": 101.0, "tp2": 102.0,
    }


def test_gates_allow_when_clean():
    client = FakeClient(equity=100_000, last_equity=100_000)
    decision = check_pre_execution_gates(client, _setup_result(), equity=100_000)
    assert decision.allowed is True


def test_gates_reject_when_3_trades_already_today():
    today_filled = [
        SimpleNamespace(filled_at=datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc))
        for _ in range(3)
    ]
    client = FakeClient(orders=today_filled)
    decision = check_pre_execution_gates(client, _setup_result(), equity=100_000)
    assert decision.allowed is False
    assert "session_trade_cap" in decision.reason


def test_gates_reject_on_daily_loss_cap():
    # Equity dropped 2% from yesterday → exceeds -1.5% cap.
    client = FakeClient(equity=98_000, last_equity=100_000)
    decision = check_pre_execution_gates(client, _setup_result(), equity=98_000)
    assert decision.allowed is False
    assert "daily_loss_limit_hit" in decision.reason


def test_gates_reject_on_invalid_sizing():
    # stop_dist 0.2% — below 0.3% floor.
    bad_setup = dict(_setup_result())
    bad_setup["stop"] = 99.8
    client = FakeClient()
    decision = check_pre_execution_gates(client, bad_setup, equity=100_000)
    assert decision.allowed is False
    assert "stop_too_tight" in decision.reason


# ---------------------------------------------------------------------------
# place_entry_bundle
# ---------------------------------------------------------------------------


def test_entry_bundle_places_all_four_orders():
    client = FakeClient(equity=100_000, fill_price=100.5)
    setup = _setup_result()  # entry 100, stop 99, tp1 101, tp2 102
    result = place_entry_bundle(setup, equity=100_000, client=client)

    assert result["placed"] is True
    assert result["protective_orders_complete"] is True
    assert result["fill_price"] == 100.5
    assert result["shares"] == 5  # $500 cap / $100 = 5
    assert all(v is not None for v in result["order_ids"].values())
    # Entry + stop + TP1 + TP2 = 4 orders submitted.
    assert len(client.submitted) == 4

    # Verify each leg's request shape.
    requests = [req for req, _ in client.submitted]
    assert requests[0].__class__.__name__ == "MarketOrderRequest"
    assert requests[1].__class__.__name__ == "StopOrderRequest"
    assert requests[2].__class__.__name__ == "LimitOrderRequest"
    assert requests[3].__class__.__name__ == "LimitOrderRequest"
    # TP1 = 50% of 5 → 2 shares; TP2 = remainder 3.
    assert requests[2].qty == 2
    assert requests[3].qty == 3


def test_entry_bundle_returns_skip_reason_when_sizing_rejected():
    client = FakeClient()
    bad_setup = dict(_setup_result())
    bad_setup["stop"] = 99.8
    result = place_entry_bundle(bad_setup, equity=100_000, client=client)
    assert result["placed"] is False
    assert "stop_too_tight" in result["skip_reason"]
    assert len(client.submitted) == 0


def test_entry_bundle_handles_single_share_split():
    # equity 1000 → risk $5. stop_dist 1% → needed $500 → 5 shares.
    # But push to make shares == 1: entry 400, stop 399 → 0.25% < min 0.3%.
    # Try entry 100, stop 96 → 4% > max 3%, rejected.
    # Try entry 100, stop 99 (1%), equity 100 → risk $0.5, needed $50, capped at $50,
    # shares = 0 → "fractional_under_one_share". Won't work.
    # Try equity 10_000, entry 600, stop 590 (1.67% dist) → risk $50, needed $3000,
    # cap $500, shares = 0 → fractional. Still no.
    # Direct test: equity that gives exactly 1 share. risk=0.5%*equity, cap $500,
    # entry P → shares = floor($500/P). For 1 share, $500/P ∈ [1,2) → P ∈ (250, 500].
    # Try entry 400, stop 396 (1% dist), equity 1_000_000 → risk $5000, needed $500000,
    # cap $500 → 1 share, tp1 splits to 1, tp2 to 0. TP2 should be skipped.
    client = FakeClient(equity=1_000_000, fill_price=400.5)
    setup = {
        "setup": "A", "symbol": "GOOGL", "qualified": True, "conditions": [],
        "entry": 400.0, "stop": 396.0, "atr": 2.0,
        "tp1": 404.0, "tp2": 408.0,
    }
    result = place_entry_bundle(setup, equity=1_000_000, client=client)
    assert result["placed"] is True
    assert result["shares"] == 1
    assert result["tp1_qty"] == 1
    assert result["tp2_qty"] == 0
    assert result["tp2_price"] is None
    # Entry + stop + TP1 = 3 orders, no TP2.
    assert len(client.submitted) == 3


def test_entry_bundle_partial_failure_marks_incomplete():
    # Stop submit fails — bundle returns protective_orders_complete=False.
    class StubFailingClient(FakeClient):
        def submit_order(self, request):
            if request.__class__.__name__ == "StopOrderRequest":
                raise RuntimeError("stop order rejected by broker")
            return super().submit_order(request)

    client = StubFailingClient(equity=100_000, fill_price=100.5)
    result = place_entry_bundle(_setup_result(), equity=100_000, client=client)
    assert result["placed"] is True
    assert result["protective_orders_complete"] is False
    assert any(c == "stop" for c, _ in result["errors"])


def test_entry_bundle_returns_failure_when_market_buy_rejected():
    class StubFailingClient(FakeClient):
        def submit_order(self, request):
            raise RuntimeError("market buy rejected")

    client = StubFailingClient()
    result = place_entry_bundle(_setup_result(), equity=100_000, client=client)
    assert result["placed"] is False
    assert any(c == "entry" for c, _ in result["errors"])
