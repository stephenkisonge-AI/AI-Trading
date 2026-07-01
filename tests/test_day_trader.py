"""Tests for src/day_trader.py — sizing math, gates, entry-bundle
placement, and D5b in-trade management against an in-memory fake
TradingClient.
"""
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import src.day_trader as day_trader
from src.day_trader import (
    SkipDecision,
    _wait_for_fill,
    check_pre_execution_gates,
    compute_position_size,
    day_auto_execute_enabled,
    manage_open_positions,
    manage_position,
    place_entry_bundle,
)

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc


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


def test_sizing_capped_at_30k_when_calculated_higher():
    # equity 100k, 0.5% risk = $500. Entry 100, stop 99 → stop_dist 1%.
    # Required notional = 500 / 0.01 = $50,000 → capped to $30,000.
    # Realized risk at the cap = $30,000 × 1% = $300 (under the $500 allowance).
    out = compute_position_size(equity=100_000, entry=100.0, stop=99.0)
    assert out["shares"] == 300
    assert out["notional"] == 30_000.0


def test_sizing_uses_risk_dollars_when_below_cap():
    # equity 5000 → risk = $25. Entry 100, stop 99 → 1% stop_dist.
    # Required notional = 25 / 0.01 = $2,500 → well under $30K cap, so
    # risk-dollars binds and shares = floor(2500/100) = 25.
    out = compute_position_size(equity=5_000, entry=100.0, stop=99.0)
    assert out["shares"] == 25
    # Wider stop, smaller account — risk binds first too.
    out2 = compute_position_size(equity=2_000, entry=100.0, stop=97.0)
    # risk = $10, stop_dist 3%, needed = $10 / 0.03 = $333.33, well under cap → 3 shares.
    assert out2["shares"] == 3


def test_sizing_risk_binds_at_wide_stops_on_full_account():
    # At wider stops, risk-dollars binds before the cap. equity 100k,
    # entry 100, stop 97.5 (2.5% stop) → risk $500, needed $20,000,
    # cap $30,000 → risk-dollars binds, 200 shares, $20k notional.
    out = compute_position_size(equity=100_000, entry=100.0, stop=97.5)
    assert out["shares"] == 200
    assert out["notional"] == 20_000.0
    assert out["risk_dollars"] == 500.0


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
    # Engineering a fractional result needs notional_capped < entry. With
    # the $30K cap that rarely happens for realistic tickers — instead we
    # construct it from the small-equity side: equity $100, entry $100,
    # stop $99. Risk = $0.50, stop_dist 1%, needed = $50, capped at $50
    # (above the $50 floor, just). Shares = floor(50/100) = 0 → fractional.
    out = compute_position_size(equity=100, entry=100.0, stop=99.0)
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

    def __init__(self, equity=100_000, last_equity=100_000, orders=(),
                 fill_price=None, positions=()):
        self.equity = equity
        self.last_equity = last_equity
        self._orders = list(orders)
        self._positions = list(positions)
        self.submitted: list = []
        self.fill_price = fill_price
        self._order_seq = 0
        self._order_states: dict[str, str] = {}
        self.closed_positions: list[str] = []
        self.replaced: list[tuple[str, object]] = []

    # --- Alpaca-compatible API surface ---

    def get_account(self):
        return SimpleNamespace(
            equity=str(self.equity), last_equity=str(self.last_equity)
        )

    def get_all_positions(self):
        return list(self._positions)

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
            legs=None,
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
        # OCO orders: synthesize the stop-loss child leg so the producer can
        # extract its id for later breakeven replacement.
        order_class = getattr(request, "order_class", None)
        is_oco = (
            order_class is not None
            and str(order_class).lower().endswith("oco")
        )
        if is_oco:
            stop_loss = getattr(request, "stop_loss", None)
            stop_leg = SimpleNamespace(
                id=f"{order_id}-stop-leg",
                order_type="OrderType.STOP",
                stop_price=str(getattr(stop_loss, "stop_price", "")),
            )
            rec.legs = [stop_leg]
        self.submitted.append((request, rec))
        return rec

    def get_order_by_id(self, order_id):
        # Look up the most recent submission by id.
        for req, rec in self.submitted:
            if rec.id == order_id:
                return rec
        raise KeyError(order_id)

    def close_position(self, symbol):
        # Atomic cancel-and-close on Alpaca's side. The fake records the
        # call and returns a synthetic closing order with a fresh id.
        self.closed_positions.append(symbol)
        self._order_seq += 1
        return SimpleNamespace(id=f"close-{self._order_seq}", symbol=symbol)

    def replace_order_by_id(self, order_id, replace_request):
        # Replace records what was replaced and returns a new order id.
        self.replaced.append((str(order_id), replace_request))
        self._order_seq += 1
        new_id = f"{order_id}-replaced-{self._order_seq}"
        return SimpleNamespace(id=new_id)


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


def test_gates_reject_when_day_position_already_open():
    # One-position rule is enforced against LIVE broker state — a fill
    # earlier in the same scan must block a second entry.
    client = FakeClient(positions=[SimpleNamespace(symbol="TSLA")])
    decision = check_pre_execution_gates(client, _setup_result(), equity=100_000)
    assert decision.allowed is False
    assert "position_already_open_TSLA" in decision.reason


def test_gates_ignore_crypto_positions_from_swing_strand():
    # The crypto swing strand shares the paper account — a BTC swing hold
    # must NOT block day-trade entries.
    client = FakeClient(positions=[SimpleNamespace(symbol="BTCUSD")])
    decision = check_pre_execution_gates(client, _setup_result(), equity=100_000)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# _wait_for_fill status parsing
# ---------------------------------------------------------------------------


class _FixedStatusClient:
    def __init__(self, status):
        self._status = status

    def get_order_by_id(self, order_id):
        return SimpleNamespace(status=self._status)


def test_wait_for_fill_partial_fill_is_not_filled(monkeypatch):
    # PARTIALLY_FILLED must not satisfy the fill wait — sizing the OCOs
    # off a partial fill would leave sell qty exceeding the position.
    monkeypatch.setattr(day_trader, "_FILL_POLL_INTERVAL_SEC", 0.01)
    client = _FixedStatusClient("OrderStatus.PARTIALLY_FILLED")
    with pytest.raises(TimeoutError):
        _wait_for_fill(client, "ord-1", timeout_sec=0.05)


def test_wait_for_fill_rejected_raises_immediately():
    client = _FixedStatusClient("OrderStatus.REJECTED")
    with pytest.raises(RuntimeError, match="rejected"):
        _wait_for_fill(client, "ord-1", timeout_sec=5)


def test_wait_for_fill_accepts_enum_and_plain_filled():
    for status in ("OrderStatus.FILLED", "filled"):
        client = _FixedStatusClient(status)
        order = _wait_for_fill(client, "ord-1", timeout_sec=1)
        assert order is not None


# ---------------------------------------------------------------------------
# place_entry_bundle
# ---------------------------------------------------------------------------


def test_entry_bundle_places_entry_and_two_ocos():
    client = FakeClient(equity=100_000, fill_price=100.5)
    setup = _setup_result()  # entry 100, stop 99, tp1 101, tp2 102
    result = place_entry_bundle(setup, equity=100_000, client=client)

    assert result["placed"] is True
    assert result["protective_orders_complete"] is True
    assert result["fill_price"] == 100.5
    # $30K notional cap binds (needed $50K for 0.5% risk at 1% stop).
    # 300 shares × $100 = $30K notional. Realized risk = $300.
    assert result["shares"] == 300
    assert all(v is not None for v in result["order_ids"].values())
    # Entry + OCO1 + OCO2 = 3 submitted requests (each OCO is one submit).
    assert len(client.submitted) == 3

    # Verify each leg's request shape.
    requests = [req for req, _ in client.submitted]
    assert requests[0].__class__.__name__ == "MarketOrderRequest"
    assert requests[1].__class__.__name__ == "LimitOrderRequest"
    assert requests[2].__class__.__name__ == "LimitOrderRequest"
    # Both OCOs carry order_class=OCO with BOTH take_profit and stop_loss
    # children populated. Alpaca rejects OCOs that only set parent
    # limit_price + stop_loss with "oco orders require take_profit.limit_price".
    from alpaca.trading.enums import OrderClass
    assert requests[1].order_class == OrderClass.OCO
    assert requests[2].order_class == OrderClass.OCO
    assert requests[1].take_profit is not None
    assert requests[2].take_profit is not None
    assert float(requests[1].stop_loss.stop_price) == 99.0
    assert float(requests[2].stop_loss.stop_price) == 99.0
    # OCO1 takes TP1's half (150 shares @ $101), OCO2 takes TP2's (150 shares @ $102).
    assert requests[1].qty == 150
    assert float(requests[1].limit_price) == 101.0
    assert float(requests[1].take_profit.limit_price) == 101.0
    assert requests[2].qty == 150
    assert float(requests[2].limit_price) == 102.0
    assert float(requests[2].take_profit.limit_price) == 102.0


def test_entry_bundle_returns_skip_reason_when_sizing_rejected():
    client = FakeClient()
    bad_setup = dict(_setup_result())
    bad_setup["stop"] = 99.8
    result = place_entry_bundle(bad_setup, equity=100_000, client=client)
    assert result["placed"] is False
    assert "stop_too_tight" in result["skip_reason"]
    assert len(client.submitted) == 0


def test_entry_bundle_handles_single_share_split():
    # Engineer shares == 1 to exercise the "1 share, no TP2" branch.
    # Need notional_capped in [entry, 2 * entry). Use small equity so the
    # risk-dollars side binds well below the $30K cap:
    # equity 200, entry 100, stop 99 (1% stop) → risk $1, needed $100,
    # capped at $100 (above the $50 floor). shares = floor(100/100) = 1.
    client = FakeClient(equity=200, fill_price=100.5)
    setup = {
        "setup": "A", "symbol": "ARM", "qualified": True, "conditions": [],
        "entry": 100.0, "stop": 99.0, "atr": 0.5,
        "tp1": 101.0, "tp2": 102.0,
    }
    result = place_entry_bundle(setup, equity=200, client=client)
    assert result["placed"] is True
    assert result["shares"] == 1
    assert result["tp1_qty"] == 1
    assert result["tp2_qty"] == 0
    assert result["tp2_price"] is None
    # Entry + one OCO (TP1's only) = 2 submitted requests.
    assert len(client.submitted) == 2
    assert result["order_ids"]["oco_tp2"] is None
    assert result["order_ids"]["oco_tp2_stop"] is None


def test_entry_bundle_partial_failure_marks_incomplete():
    # OCO submit fails — bundle returns protective_orders_complete=False.
    class StubFailingClient(FakeClient):
        def submit_order(self, request):
            order_class = getattr(request, "order_class", None)
            if order_class is not None and str(order_class).lower().endswith("oco"):
                raise RuntimeError("oco rejected by broker")
            return super().submit_order(request)

    client = StubFailingClient(equity=100_000, fill_price=100.5)
    result = place_entry_bundle(_setup_result(), equity=100_000, client=client)
    assert result["placed"] is True
    assert result["protective_orders_complete"] is False
    components = {c for c, _ in result["errors"]}
    assert "oco_tp1" in components
    assert "oco_tp2" in components


def test_entry_bundle_returns_failure_when_market_buy_rejected():
    class StubFailingClient(FakeClient):
        def submit_order(self, request):
            raise RuntimeError("market buy rejected")

    client = StubFailingClient()
    result = place_entry_bundle(_setup_result(), equity=100_000, client=client)
    assert result["placed"] is False
    assert any(c == "entry" for c, _ in result["errors"])


# ---------------------------------------------------------------------------
# Phase D5b — in-trade management
# ---------------------------------------------------------------------------


def _et_dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=_ET)


def _open_position(symbol="NVDA", qty=5, avg_entry=100.0, current=101.0):
    return SimpleNamespace(
        symbol=symbol, qty=str(qty),
        avg_entry_price=str(avg_entry),
        current_price=str(current),
    )


def _filled_buy_order(symbol="NVDA", qty=5, filled_at=None, side="buy"):
    return SimpleNamespace(
        id=f"buy-{symbol}", symbol=symbol, qty=str(qty),
        side=f"OrderSide.{side.upper()}",
        status="OrderStatus.FILLED",
        filled_at=filled_at or datetime(2026, 5, 28, 14, 0, tzinfo=_UTC),
        order_type="OrderType.MARKET",
        stop_price=None,
    )


def _open_stop_order(symbol="NVDA", qty=5, stop_price=99.0):
    return SimpleNamespace(
        id=f"stop-{symbol}", symbol=symbol, qty=str(qty),
        side="OrderSide.SELL",
        status="OrderStatus.NEW",
        filled_at=None,
        order_type="OrderType.STOP",
        stop_price=str(stop_price),
    )


def _filled_limit_sell(symbol="NVDA", qty=2, filled_at=None, limit_price=101.0):
    return SimpleNamespace(
        id=f"tp1-{symbol}", symbol=symbol, qty=str(qty),
        side="OrderSide.SELL",
        status="OrderStatus.FILLED",
        filled_at=filled_at or datetime(2026, 5, 28, 14, 10, tzinfo=_UTC),
        order_type="OrderType.LIMIT",
        limit_price=str(limit_price),
        stop_price=None,
    )


class MgmtFakeClient(FakeClient):
    """Extends FakeClient with cancel + custom orders list for D5b paths."""

    def __init__(self, orders=(), **kwargs):
        super().__init__(orders=orders, **kwargs)
        self.cancelled: list[str] = []

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(str(order_id))

    def get_all_positions(self):
        return getattr(self, "_positions", [])


def test_management_3_55pm_hard_close():
    pos = _open_position()
    client = MgmtFakeClient(orders=[_filled_buy_order(), _open_stop_order()])
    now = _et_dt(date(2026, 5, 28), time(15, 56))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is not None
    assert res["action"] == "hard_close_355pm"
    assert res["close_order_id"] is not None
    assert res["error"] is None
    # close_position is atomic — server-side cancels related orders and closes.
    assert "NVDA" in client.closed_positions


def test_management_3_55pm_takes_priority_over_other_triggers():
    # Time is past 3:55 AND SPY just broke VWAP — 3:55 wins.
    pos = _open_position()
    client = MgmtFakeClient(orders=[_filled_buy_order(), _open_stop_order()])
    now = _et_dt(date(2026, 5, 28), time(16, 0))
    # SPY 5-min with a fresh below-VWAP close.
    spy = pd.DataFrame(
        {"close": [400.0, 399.0, 398.0], "vwap": [400.5, 400.5, 400.5]},
        index=pd.DatetimeIndex([
            datetime(2026, 5, 28, 14, 5, tzinfo=_UTC),
            datetime(2026, 5, 28, 14, 10, tzinfo=_UTC),
            datetime(2026, 5, 28, 14, 15, tzinfo=_UTC),
        ]),
    )
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=spy,
    )
    assert res["action"] == "hard_close_355pm"


def test_management_spy_vwap_break_hard_exit():
    pos = _open_position()
    client = MgmtFakeClient(orders=[
        _filled_buy_order(filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_stop_order(),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 30))
    # 3 SPY bars: two after fill_time. Final two close BELOW their VWAP.
    # Last bar is in-progress (dropped). Penultimate is "closed" and triggers.
    spy = pd.DataFrame(
        {"close": [400.0, 399.0, 398.0],
         "vwap":  [400.5, 400.5, 400.5]},
        index=pd.DatetimeIndex([
            datetime(2026, 5, 28, 14, 5, tzinfo=_UTC),
            datetime(2026, 5, 28, 14, 10, tzinfo=_UTC),
            datetime(2026, 5, 28, 14, 15, tzinfo=_UTC),  # in-progress (dropped)
        ]),
    )
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=spy,
    )
    assert res is not None
    assert res["action"] == "hard_exit_spy_vwap_break"
    assert res["close_order_id"] is not None


def test_management_spy_vwap_break_ignores_bars_before_entry():
    # SPY broke VWAP at 13:55 — BEFORE our 14:00 fill. Should not trigger.
    pos = _open_position()
    client = MgmtFakeClient(orders=[
        _filled_buy_order(filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_stop_order(),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 30))
    spy = pd.DataFrame(
        {"close": [399.0, 401.0, 400.0],
         "vwap":  [400.0, 400.0, 400.0]},
        index=pd.DatetimeIndex([
            datetime(2026, 5, 28, 13, 55, tzinfo=_UTC),  # before entry
            datetime(2026, 5, 28, 14, 5, tzinfo=_UTC),
            datetime(2026, 5, 28, 14, 10, tzinfo=_UTC),  # in-progress (dropped)
        ]),
    )
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=spy,
    )
    # No action — pre-entry break doesn't count.
    assert res is None


def test_management_tp1_fill_moves_stop_to_breakeven():
    pos = _open_position(qty=3, avg_entry=100.0)  # 5 - 2 (TP1 took 2) = 3 remaining
    client = MgmtFakeClient(orders=[
        _filled_buy_order(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_stop_order(stop_price=99.0),
        _filled_limit_sell(qty=2, filled_at=datetime(2026, 5, 28, 14, 10, tzinfo=_UTC)),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 30))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is not None
    assert res["action"] == "breakeven_move"
    assert res["success"] is True
    assert res["stop_price"] == 100.0  # avg_entry
    assert res["qty"] == 3
    # Stop replaced in place — OCO link (and TP2 limit) preserved.
    assert len(client.replaced) == 1
    replaced_id, replace_req = client.replaced[0]
    assert replaced_id == "stop-NVDA"
    assert float(replace_req.stop_price) == 100.0
    # No cancel_order_by_id — that would have killed the OCO's TP2 leg.
    assert "stop-NVDA" not in client.cancelled


def test_management_tp1_already_at_breakeven_no_action():
    # Stop already at or above avg_entry — don't re-place.
    pos = _open_position(qty=3, avg_entry=100.0)
    client = MgmtFakeClient(orders=[
        _filled_buy_order(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_stop_order(stop_price=100.0),  # already at BE
        _filled_limit_sell(qty=2, filled_at=datetime(2026, 5, 28, 14, 10, tzinfo=_UTC)),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 30))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is None


def test_management_time_stop_fires_after_30_min_below_threshold():
    # Fill at 14:00. Now 14:35 (35 min). Entry 100, stop 99 → R=1, threshold
    # entry + 0.25R = 100.25. Current price 100.10 < 100.25 → time stop fires.
    pos = _open_position(qty=5, avg_entry=100.0, current=100.10)
    client = MgmtFakeClient(orders=[
        _filled_buy_order(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_stop_order(stop_price=99.0),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 35))  # 14:35 UTC
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is not None
    assert res["action"] == "time_stop"
    assert res["close_order_id"] is not None
    assert "time stop" in res["reason"]


def test_management_time_stop_does_not_fire_if_progress_made():
    # Same as above but current price 100.50 ≥ entry+0.25R → no time stop.
    pos = _open_position(qty=5, avg_entry=100.0, current=100.50)
    client = MgmtFakeClient(orders=[
        _filled_buy_order(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_stop_order(stop_price=99.0),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 35))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is None


def test_management_time_stop_does_not_fire_before_30_min():
    pos = _open_position(qty=5, avg_entry=100.0, current=100.10)
    client = MgmtFakeClient(orders=[
        _filled_buy_order(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_stop_order(stop_price=99.0),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 25))  # 14:25 UTC = 25 min
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is None


def test_management_time_stop_skipped_after_tp1_fills():
    # TP1 has already fired → strategy says we already made +1R, skip
    # time stop and let the BE stop handle the rest.
    pos = _open_position(qty=3, avg_entry=100.0, current=100.10)
    client = MgmtFakeClient(orders=[
        _filled_buy_order(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_stop_order(stop_price=100.0),  # already at BE
        _filled_limit_sell(qty=2, filled_at=datetime(2026, 5, 28, 14, 10, tzinfo=_UTC)),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 40))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    # No time stop — TP1 fill exempts. Stop already at BE so no breakeven_move either.
    assert res is None


def test_management_no_entry_fill_bails_safely():
    # Position exists but no matching BUY order found — bail rather than
    # misbehave on weird Alpaca state.
    pos = _open_position()
    client = MgmtFakeClient(orders=[_open_stop_order()])  # no buy
    now = _et_dt(date(2026, 5, 28), time(10, 30))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is None


def test_manage_open_positions_never_touches_crypto_positions():
    # The crypto swing strand shares this paper account. Its positions must
    # be invisible to day-trade management — the 3:55 PM hard close would
    # otherwise liquidate a multi-day BTC swing hold.
    client = MgmtFakeClient(
        orders=[],
        positions=[_open_position(symbol="BTCUSD")],
    )
    now = _et_dt(date(2026, 5, 28), time(15, 56))  # inside hard-close window
    actions = manage_open_positions(
        now_et=now, spy_5min_with_indicators=None, client=client,
    )
    assert actions == []
    assert client.closed_positions == []


def test_manage_open_positions_still_hard_closes_universe_position():
    client = MgmtFakeClient(
        orders=[_filled_buy_order(), _open_stop_order()],
        positions=[_open_position(symbol="NVDA")],
    )
    now = _et_dt(date(2026, 5, 28), time(15, 56))
    actions = manage_open_positions(
        now_et=now, spy_5min_with_indicators=None, client=client,
    )
    assert len(actions) == 1
    assert actions[0]["action"] == "hard_close_355pm"
    assert client.closed_positions == ["NVDA"]


# ---------------------------------------------------------------------------
# Phase D5c — lifecycle stats
# ---------------------------------------------------------------------------


from src.day_trader import (
    _parse_setup_from_client_order_id,
    _trade_walk_for_symbol,
    summarize_day_lifecycle,
)


def _closed_buy(symbol, qty, price, filled_at, client_order_id=None):
    return SimpleNamespace(
        id=f"buy-{symbol}-{filled_at.isoformat()}",
        symbol=symbol, qty=str(qty),
        side="OrderSide.BUY",
        order_type="OrderType.MARKET",
        status="OrderStatus.FILLED",
        filled_qty=str(qty), filled_avg_price=str(price),
        filled_at=filled_at,
        submitted_at=filled_at, created_at=filled_at,
        stop_price=None,
        client_order_id=client_order_id,
    )


def _closed_sell_limit(symbol, qty, price, filled_at):
    return SimpleNamespace(
        id=f"tp-{symbol}-{filled_at.isoformat()}",
        symbol=symbol, qty=str(qty),
        side="OrderSide.SELL",
        order_type="OrderType.LIMIT",
        status="OrderStatus.FILLED",
        filled_qty=str(qty), filled_avg_price=str(price),
        filled_at=filled_at,
        submitted_at=filled_at, created_at=filled_at,
        stop_price=None,
        client_order_id=None,
    )


def _closed_sell_stop(symbol, qty, price, stop_price, filled_at):
    return SimpleNamespace(
        id=f"stop-{symbol}-{filled_at.isoformat()}",
        symbol=symbol, qty=str(qty),
        side="OrderSide.SELL",
        order_type="OrderType.STOP",
        status="OrderStatus.FILLED",
        filled_qty=str(qty), filled_avg_price=str(price),
        filled_at=filled_at,
        submitted_at=filled_at, created_at=filled_at,
        stop_price=str(stop_price),
        client_order_id=None,
    )


def _closed_sell_market(symbol, qty, price, filled_at):
    return SimpleNamespace(
        id=f"mgmt-{symbol}-{filled_at.isoformat()}",
        symbol=symbol, qty=str(qty),
        side="OrderSide.SELL",
        order_type="OrderType.MARKET",
        status="OrderStatus.FILLED",
        filled_qty=str(qty), filled_avg_price=str(price),
        filled_at=filled_at,
        submitted_at=filled_at, created_at=filled_at,
        stop_price=None,
        client_order_id=None,
    )


class LifecycleFakeClient(MgmtFakeClient):
    def __init__(self, closed_orders=()):
        super().__init__()
        self._closed_orders = list(closed_orders)

    def get_orders(self, filter=None):
        return list(self._closed_orders)


def test_parse_setup_from_client_order_id():
    assert _parse_setup_from_client_order_id("DAY-A-NVDA-1748448000") == "A"
    assert _parse_setup_from_client_order_id("DAY-B-AAPL-1748448000") == "B"
    assert _parse_setup_from_client_order_id(None) == "unknown"
    assert _parse_setup_from_client_order_id("") == "unknown"
    # Wrong prefix or unknown setup letter falls back gracefully.
    assert _parse_setup_from_client_order_id("CRYPTO-A-BTC-1") == "unknown"
    assert _parse_setup_from_client_order_id("DAY-Z-NVDA-1") == "unknown"


def test_lifecycle_empty_history():
    client = LifecycleFakeClient(closed_orders=[])
    stats = summarize_day_lifecycle(client=client)
    assert stats["total_closed"] == 0
    assert stats["open_trades"] == 0
    assert stats["win_rate"] is None
    assert stats["mean_r"] is None
    assert stats["expectancy_warning"] is None
    assert stats["error"] is None


def test_lifecycle_single_winning_trade_via_tp():
    # Bought 5 @ $100 with Setup A tag, stop at $99, TP1 at $101 (2 sh),
    # TP2 at $102 (3 sh). Both TPs fill. PL = 2*1 + 3*2 = $8.
    # R = 5 * (100 - 99) = $5 risk. trade R-multiple = 8/5 = 1.6R.
    entry_t = datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)
    stop_t = entry_t + timedelta(minutes=5)  # stop order accepted but didn't fill
    tp1_t = entry_t + timedelta(minutes=15)
    tp2_t = entry_t + timedelta(minutes=45)
    orders = [
        _closed_buy("NVDA", 5, 100.0, entry_t,
                    client_order_id=f"DAY-A-NVDA-{int(entry_t.timestamp())}"),
        # Stop SELL with stop_price recorded (qty 0 = never filled, but
        # the stop_price is still on the order and gets recorded in
        # stops_seen by the trade walker).
        SimpleNamespace(
            id="stop-NVDA", symbol="NVDA", qty="5",
            side="OrderSide.SELL", order_type="OrderType.STOP",
            status="OrderStatus.CANCELED",
            filled_qty="0", filled_avg_price=None,
            filled_at=None, submitted_at=stop_t, created_at=stop_t,
            stop_price="99.0", client_order_id=None,
        ),
        _closed_sell_limit("NVDA", 2, 101.0, tp1_t),
        _closed_sell_limit("NVDA", 3, 102.0, tp2_t),
    ]
    client = LifecycleFakeClient(closed_orders=orders)
    stats = summarize_day_lifecycle(client=client)
    assert stats["total_closed"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 0
    assert stats["win_rate"] == 1.0
    assert stats["total_pl_usd"] == pytest.approx(8.0)
    assert stats["mean_r"] == pytest.approx(1.6)
    assert stats["best_r"] == pytest.approx(1.6)
    assert stats["worst_r"] == pytest.approx(1.6)
    assert stats["by_setup"]["A"]["closed"] == 1
    assert stats["by_setup"]["A"]["wins"] == 1
    assert stats["by_setup"]["B"]["closed"] == 0
    assert stats["by_symbol"]["NVDA"]["closed"] == 1
    # Avg duration ≈ 45 min (entry to last exit).
    assert stats["avg_minutes_in_trade"] == pytest.approx(45.0)


def test_lifecycle_single_losing_trade_via_stop():
    # Bought 5 @ $100, stop at $99 fills full position. PL = 5 * -1 = -$5.
    # R = $5 risk → R-multiple = -1.0.
    entry_t = datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)
    stop_t = entry_t + timedelta(minutes=20)
    orders = [
        _closed_buy("AAPL", 5, 100.0, entry_t,
                    client_order_id=f"DAY-B-AAPL-{int(entry_t.timestamp())}"),
        _closed_sell_stop("AAPL", 5, 99.0, 99.0, stop_t),
    ]
    client = LifecycleFakeClient(closed_orders=orders)
    stats = summarize_day_lifecycle(client=client)
    assert stats["total_closed"] == 1
    assert stats["wins"] == 0
    assert stats["losses"] == 1
    assert stats["win_rate"] == 0.0
    assert stats["total_pl_usd"] == pytest.approx(-5.0)
    assert stats["mean_r"] == pytest.approx(-1.0)
    assert stats["by_setup"]["B"]["closed"] == 1
    assert stats["by_setup"]["A"]["closed"] == 0


def test_lifecycle_mgmt_close_classified_correctly():
    # Time-stop or 3:55 close: stop order accepted (recorded for R), then
    # a market SELL closes the position.
    entry_t = datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)
    stop_t = entry_t + timedelta(minutes=1)
    close_t = entry_t + timedelta(minutes=35)
    orders = [
        _closed_buy("TSLA", 5, 100.0, entry_t,
                    client_order_id=f"DAY-A-TSLA-{int(entry_t.timestamp())}"),
        # Cancelled stop with stop_price still on the order.
        SimpleNamespace(
            id="stop-TSLA", symbol="TSLA", qty="5",
            side="OrderSide.SELL", order_type="OrderType.STOP",
            status="OrderStatus.CANCELED",
            filled_qty="0", filled_avg_price=None,
            filled_at=None, submitted_at=stop_t, created_at=stop_t,
            stop_price="99.0", client_order_id=None,
        ),
        _closed_sell_market("TSLA", 5, 100.10, close_t),
    ]
    client = LifecycleFakeClient(closed_orders=orders)
    stats = summarize_day_lifecycle(client=client)
    assert stats["total_closed"] == 1
    # PL = 5 * 0.10 = $0.50, R = $5 risk → 0.10R.
    assert stats["total_pl_usd"] == pytest.approx(0.5)
    assert stats["mean_r"] == pytest.approx(0.1)
    assert stats["wins"] == 1  # positive PL counts as win


def test_lifecycle_expectancy_warning_threshold():
    # Need ≥50 closed trades AND mean R < 0.2 for the warning to fire.
    # Below 50: no warning even at bad R.
    orders = []
    for i in range(49):
        et = datetime(2026, 5, 1, 14, 0, tzinfo=_UTC) + timedelta(days=i)
        st = et + timedelta(minutes=10)
        orders.append(_closed_buy(
            "NVDA", 5, 100.0, et,
            client_order_id=f"DAY-A-NVDA-{int(et.timestamp())}",
        ))
        orders.append(_closed_sell_stop("NVDA", 5, 99.0, 99.0, st))
    client = LifecycleFakeClient(closed_orders=orders)
    stats = summarize_day_lifecycle(client=client)
    assert stats["total_closed"] == 49
    assert stats["expectancy_warning"] is None  # below sample threshold

    # Now 50 trades, all losing 1R → mean_r = -1.0 < 0.2 → warning fires.
    et = datetime(2026, 5, 1, 14, 0, tzinfo=_UTC) + timedelta(days=49)
    st = et + timedelta(minutes=10)
    orders.append(_closed_buy(
        "NVDA", 5, 100.0, et,
        client_order_id=f"DAY-A-NVDA-{int(et.timestamp())}",
    ))
    orders.append(_closed_sell_stop("NVDA", 5, 99.0, 99.0, st))
    client = LifecycleFakeClient(closed_orders=orders)
    stats = summarize_day_lifecycle(client=client)
    assert stats["total_closed"] == 50
    assert stats["expectancy_warning"] is not None
    assert "STOP the experiment" in stats["expectancy_warning"]


def test_lifecycle_orders_fetch_failure_returns_error():
    class FailingClient(LifecycleFakeClient):
        def get_orders(self, filter=None):
            raise RuntimeError("alpaca timeout")
    stats = summarize_day_lifecycle(client=FailingClient())
    assert stats["error"] is not None
    assert "alpaca timeout" in stats["error"]
    # Other fields not populated when fetch fails.
    assert "total_closed" not in stats


def test_lifecycle_open_trade_not_counted_as_closed():
    # Bought but not exited yet → counted as open, not closed.
    entry_t = datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)
    orders = [
        _closed_buy("NVDA", 5, 100.0, entry_t,
                    client_order_id=f"DAY-A-NVDA-{int(entry_t.timestamp())}"),
    ]
    client = LifecycleFakeClient(closed_orders=orders)
    stats = summarize_day_lifecycle(client=client)
    assert stats["total_closed"] == 0
    assert stats["open_trades"] == 1
    assert stats["win_rate"] is None


def test_lifecycle_per_symbol_breakdown():
    e1 = datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)
    e2 = datetime(2026, 5, 28, 14, 30, tzinfo=_UTC)
    orders = [
        _closed_buy("NVDA", 5, 100.0, e1,
                    client_order_id=f"DAY-A-NVDA-{int(e1.timestamp())}"),
        _closed_sell_stop("NVDA", 5, 99.0, 99.0, e1 + timedelta(minutes=10)),
        _closed_buy("AAPL", 5, 100.0, e2,
                    client_order_id=f"DAY-B-AAPL-{int(e2.timestamp())}"),
        _closed_sell_limit("AAPL", 5, 102.0, e2 + timedelta(minutes=20)),
    ]
    client = LifecycleFakeClient(closed_orders=orders)
    stats = summarize_day_lifecycle(client=client)
    assert stats["total_closed"] == 2
    assert stats["by_symbol"]["NVDA"]["closed"] == 1
    assert stats["by_symbol"]["NVDA"]["wins"] == 0
    assert stats["by_symbol"]["AAPL"]["closed"] == 1
    assert stats["by_symbol"]["AAPL"]["wins"] == 1


def test_entry_bundle_sets_client_order_id_for_setup_tagging():
    """D4c expects D4a to tag entry BUYs with client_order_id so the
    lifecycle walker can recover the setup type. Verify the tag format.
    """
    client = FakeClient(equity=100_000, fill_price=100.5)
    setup = _setup_result()  # setup == "A", symbol == "NVDA"
    result = place_entry_bundle(setup, equity=100_000, client=client)
    assert result["placed"] is True

    entry_req, _ = client.submitted[0]
    assert hasattr(entry_req, "client_order_id")
    assert entry_req.client_order_id.startswith("DAY-A-NVDA-")


# ---------------------------------------------------------------------------
# Expectancy circuit breaker
# ---------------------------------------------------------------------------


def test_gate_no_lifecycle_stats_passes_through():
    """When lifecycle_stats is None, expectancy gate is skipped."""
    client = FakeClient(equity=100_000)
    decision = check_pre_execution_gates(
        client, _setup_result(), equity=100_000, lifecycle_stats=None,
    )
    assert decision.allowed is True


def test_gate_lifecycle_with_error_field_fails_open():
    """Lifecycle fetch failed — that's a data problem, not a strategy
    violation. Gate must NOT block on stats errors.
    """
    client = FakeClient(equity=100_000)
    decision = check_pre_execution_gates(
        client, _setup_result(), equity=100_000,
        lifecycle_stats={"error": "alpaca timeout", "days_back": 90},
    )
    assert decision.allowed is True


def test_gate_clean_lifecycle_passes():
    client = FakeClient(equity=100_000)
    decision = check_pre_execution_gates(
        client, _setup_result(), equity=100_000,
        lifecycle_stats={"expectancy_warning": None, "total_closed": 100},
    )
    assert decision.allowed is True


def test_gate_expectancy_warning_blocks_new_entries(monkeypatch):
    monkeypatch.delenv("WATCHER_DAY_OVERRIDE_EXPECTANCY", raising=False)
    client = FakeClient(equity=100_000)
    decision = check_pre_execution_gates(
        client, _setup_result(), equity=100_000,
        lifecycle_stats={
            "expectancy_warning": "mean R -0.30R below +0.2R after 50 trades",
            "total_closed": 50,
        },
    )
    assert decision.allowed is False
    assert "expectancy_circuit_breaker" in decision.reason
    assert "WATCHER_DAY_OVERRIDE_EXPECTANCY" in decision.reason


def test_gate_expectancy_override_allows_through(monkeypatch):
    monkeypatch.setenv("WATCHER_DAY_OVERRIDE_EXPECTANCY", "true")
    client = FakeClient(equity=100_000)
    decision = check_pre_execution_gates(
        client, _setup_result(), equity=100_000,
        lifecycle_stats={
            "expectancy_warning": "mean R -0.30R below +0.2R after 50 trades",
            "total_closed": 50,
        },
    )
    assert decision.allowed is True


def test_gate_expectancy_override_only_honors_true(monkeypatch):
    """Any value other than literal 'true' (case-insensitive) leaves the
    circuit breaker active. Guards against `=1`, `=yes`, `=on` etc.
    accidentally disabling the protection.
    """
    monkeypatch.setenv("WATCHER_DAY_OVERRIDE_EXPECTANCY", "1")
    client = FakeClient(equity=100_000)
    decision = check_pre_execution_gates(
        client, _setup_result(), equity=100_000,
        lifecycle_stats={
            "expectancy_warning": "mean R -0.30R below +0.2R after 50 trades",
            "total_closed": 50,
        },
    )
    assert decision.allowed is False


def test_gate_expectancy_runs_before_alpaca_calls(monkeypatch):
    """Expectancy circuit breaker should fire BEFORE the gate hits Alpaca
    for the session-cap check. Verify by giving a client whose
    get_orders would raise — if the breaker fires first, we never call it.
    """
    monkeypatch.delenv("WATCHER_DAY_OVERRIDE_EXPECTANCY", raising=False)

    class ExplodingClient(FakeClient):
        def get_orders(self, filter=None):
            raise RuntimeError("session-cap check should never run")

    client = ExplodingClient(equity=100_000)
    decision = check_pre_execution_gates(
        client, _setup_result(), equity=100_000,
        lifecycle_stats={
            "expectancy_warning": "mean R -0.50R below +0.2R after 60 trades",
            "total_closed": 60,
        },
    )
    assert decision.allowed is False
    assert "expectancy_circuit_breaker" in decision.reason
