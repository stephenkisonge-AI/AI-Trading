"""Short-side day-trader tests: sizing, gates (incl. the new weekly-loss,
cooldown, and spread gates), entry bundle, management, and lifecycle
reconstruction for SHORT trades.

Reuses the fakes from tests/test_day_trader.py.
"""
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.day_trader import (
    _gate_consecutive_losses,
    _gate_spread,
    _gate_weekly_loss,
    _trade_pl,
    _trade_risk_per_unit,
    _trade_walk_for_symbol,
    check_pre_execution_gates,
    compute_position_size,
    manage_position,
    place_entry_bundle,
)
from tests.test_day_trader import FakeClient, MgmtFakeClient, _et_dt

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc


def _short_setup_result():
    return {
        "setup": "A", "direction": "short", "symbol": "NVDA",
        "qualified": True, "conditions": [], "entry": 100.0, "stop": 101.0,
        "atr": 0.5, "tp1": 99.0, "tp2": 98.0,
    }


# ---------------------------------------------------------------------------
# Sizing + gates
# ---------------------------------------------------------------------------


def test_sizing_short_valid():
    out = compute_position_size(
        equity=100_000, entry=100.0, stop=101.0, direction="short",
    )
    assert "skip_reason" not in out
    # 1% stop distance, $500 risk → $50K needed, capped at $30K → 300 sh.
    assert out["shares"] == 300


def test_sizing_short_rejects_stop_below_entry():
    out = compute_position_size(
        equity=100_000, entry=100.0, stop=99.0, direction="short",
    )
    assert out["skip_reason"] == "invalid_entry_or_stop"


def test_gates_block_short_when_switch_off(monkeypatch):
    monkeypatch.delenv("WATCHER_DAY_ENABLE_SHORTS", raising=False)
    client = FakeClient()
    decision = check_pre_execution_gates(client, _short_setup_result(), equity=100_000)
    assert decision.allowed is False
    assert "shorts_disabled" in decision.reason


def test_gates_allow_short_when_switch_on(monkeypatch):
    monkeypatch.setenv("WATCHER_DAY_ENABLE_SHORTS", "true")
    client = FakeClient()
    decision = check_pre_execution_gates(client, _short_setup_result(), equity=100_000)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# Entry bundle
# ---------------------------------------------------------------------------


def test_entry_bundle_short_places_sell_entry_and_buy_ocos():
    client = FakeClient(equity=100_000, fill_price=99.8)
    result = place_entry_bundle(_short_setup_result(), equity=100_000, client=client)

    assert result["placed"] is True
    assert result["protective_orders_complete"] is True
    assert result["direction"] == "short"
    assert result["shares"] == 300

    requests = [req for req, _ in client.submitted]
    # Entry is a market SELL (short).
    assert requests[0].__class__.__name__ == "MarketOrderRequest"
    assert str(requests[0].side).lower().endswith("sell")
    # client_order_id tags the short variant: DAY-AS-...
    assert requests[0].client_order_id.startswith("DAY-AS-NVDA-")
    # Both OCOs are BUY (cover) orders: TP limits below entry, stop above.
    for oco in requests[1:3]:
        assert str(oco.side).lower().endswith("buy")
        assert float(oco.stop_loss.stop_price) == 101.0
    assert float(requests[1].take_profit.limit_price) == 99.0
    assert float(requests[2].take_profit.limit_price) == 98.0


# ---------------------------------------------------------------------------
# In-trade management (short)
# ---------------------------------------------------------------------------


def _open_short_position(symbol="NVDA", qty=-5, avg_entry=100.0, current=99.5):
    return SimpleNamespace(
        symbol=symbol, qty=str(qty),
        avg_entry_price=str(avg_entry),
        current_price=str(current),
    )


def _filled_sell_entry(symbol="NVDA", qty=5, filled_at=None):
    return SimpleNamespace(
        id=f"short-entry-{symbol}", symbol=symbol, qty=str(qty),
        side="OrderSide.SELL",
        status="OrderStatus.FILLED",
        filled_at=filled_at or datetime(2026, 5, 28, 14, 0, tzinfo=_UTC),
        order_type="OrderType.MARKET",
        stop_price=None,
        client_order_id="DAY-AS-NVDA-1750000000",
    )


def _open_buy_stop(symbol="NVDA", qty=5, stop_price=101.0):
    return SimpleNamespace(
        id=f"buystop-{symbol}", symbol=symbol, qty=str(qty),
        side="OrderSide.BUY",
        status="OrderStatus.NEW",
        filled_at=None,
        order_type="OrderType.STOP",
        stop_price=str(stop_price),
    )


def _filled_buy_limit_cover(symbol="NVDA", qty=2, filled_at=None):
    return SimpleNamespace(
        id=f"cover-tp1-{symbol}", symbol=symbol, qty=str(qty),
        side="OrderSide.BUY",
        status="OrderStatus.FILLED",
        filled_at=filled_at or datetime(2026, 5, 28, 14, 10, tzinfo=_UTC),
        order_type="OrderType.LIMIT",
        stop_price=None,
    )


def test_management_short_spy_vwap_reclaim_exits():
    # For a SHORT, the adverse SPY signal is a 5-min close ABOVE VWAP.
    pos = _open_short_position()
    client = MgmtFakeClient(orders=[
        _filled_sell_entry(filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_buy_stop(),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 30))
    spy = pd.DataFrame(
        {"close": [400.0, 401.0, 402.0],
         "vwap":  [400.5, 400.5, 400.5]},
        index=pd.DatetimeIndex([
            datetime(2026, 5, 28, 14, 5, tzinfo=_UTC),
            datetime(2026, 5, 28, 14, 10, tzinfo=_UTC),  # close > vwap → exit
            datetime(2026, 5, 28, 14, 15, tzinfo=_UTC),  # in-progress (dropped)
        ]),
    )
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=spy,
    )
    assert res is not None
    assert res["action"] == "hard_exit_spy_vwap_break"
    assert "above" in res["reason"]


def test_management_short_spy_below_vwap_no_exit():
    # SPY below VWAP is GOOD for a short — no action.
    pos = _open_short_position()
    client = MgmtFakeClient(orders=[
        _filled_sell_entry(filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_buy_stop(),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 20))  # < 30 min, no time stop
    spy = pd.DataFrame(
        {"close": [399.0, 398.0, 397.0],
         "vwap":  [400.5, 400.5, 400.5]},
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
    assert res is None


def test_management_short_tp1_fill_moves_stop_down_to_breakeven():
    pos = _open_short_position(qty=-3, avg_entry=100.0)
    client = MgmtFakeClient(orders=[
        _filled_sell_entry(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_buy_stop(stop_price=101.0),  # above entry = loss side for shorts
        _filled_buy_limit_cover(qty=2, filled_at=datetime(2026, 5, 28, 14, 10, tzinfo=_UTC)),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 30))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is not None
    assert res["action"] == "breakeven_move"
    assert res["success"] is True
    assert res["stop_price"] == 100.0  # moved DOWN to avg_entry
    replaced_id, replace_req = client.replaced[0]
    assert replaced_id == "buystop-NVDA"
    assert float(replace_req.stop_price) == 100.0


def test_management_short_stop_already_at_breakeven_no_move():
    pos = _open_short_position(qty=-3, avg_entry=100.0)
    client = MgmtFakeClient(orders=[
        _filled_sell_entry(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_buy_stop(stop_price=100.0),  # already at BE
        _filled_buy_limit_cover(qty=2, filled_at=datetime(2026, 5, 28, 14, 10, tzinfo=_UTC)),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 30))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is None


def test_management_short_time_stop_fires_when_stalled():
    # Entry 100, buy stop 101.5 → R = 1.5, threshold = 100 − 0.375 = 99.625.
    # 35 min after fill, price 99.9 (> threshold → insufficient progress).
    pos = _open_short_position(qty=-5, avg_entry=100.0, current=99.9)
    client = MgmtFakeClient(orders=[
        _filled_sell_entry(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_buy_stop(stop_price=101.5),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 35))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is not None
    assert res["action"] == "time_stop"


def test_management_short_time_stop_skipped_when_in_profit():
    # Price 99.5 < threshold 99.625 → short is working, leave it alone.
    pos = _open_short_position(qty=-5, avg_entry=100.0, current=99.5)
    client = MgmtFakeClient(orders=[
        _filled_sell_entry(qty=5, filled_at=datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)),
        _open_buy_stop(stop_price=101.5),
    ])
    now = _et_dt(date(2026, 5, 28), time(10, 35))
    res = manage_position(
        client=client, position=pos, now_et=now,
        spy_5min_with_indicators=None,
    )
    assert res is None


# ---------------------------------------------------------------------------
# Lifecycle reconstruction (short)
# ---------------------------------------------------------------------------


def test_lifecycle_walk_reconstructs_short_trade():
    t0 = datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)
    t1 = datetime(2026, 5, 28, 14, 30, tzinfo=_UTC)
    entry = SimpleNamespace(
        symbol="NVDA", side="OrderSide.SELL", order_type="OrderType.MARKET",
        filled_qty="5", filled_avg_price="100.0", stop_price=None,
        filled_at=t0, submitted_at=t0, created_at=t0,
        client_order_id="DAY-AS-NVDA-1750000000",
    )
    resting_stop = SimpleNamespace(
        symbol="NVDA", side="OrderSide.BUY", order_type="OrderType.STOP",
        filled_qty="0", filled_avg_price=None, stop_price="101.0",
        filled_at=None, submitted_at=t0, created_at=t0,
        client_order_id=None,
    )
    cover = SimpleNamespace(
        symbol="NVDA", side="OrderSide.BUY", order_type="OrderType.LIMIT",
        filled_qty="5", filled_avg_price="98.0", stop_price=None,
        filled_at=t1, submitted_at=t1, created_at=t1,
        client_order_id=None,
    )
    trades = _trade_walk_for_symbol([entry, resting_stop, cover])
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "short"
    assert t["setup"] == "AS"
    assert t["qty_remaining"] == 0
    # P&L: sold 5 @ 100, covered 5 @ 98 → +$10. Risk 1/sh → +2.0R.
    assert _trade_pl(t) == pytest.approx(10.0)
    assert _trade_risk_per_unit(t) == pytest.approx(1.0)


def test_lifecycle_walk_long_then_short_same_symbol():
    # A long trade closed by TP, then a tagged short later the same day.
    t0 = datetime(2026, 5, 28, 14, 0, tzinfo=_UTC)
    t1 = datetime(2026, 5, 28, 14, 30, tzinfo=_UTC)
    t2 = datetime(2026, 5, 28, 15, 0, tzinfo=_UTC)
    t3 = datetime(2026, 5, 28, 15, 30, tzinfo=_UTC)
    long_entry = SimpleNamespace(
        symbol="NVDA", side="OrderSide.BUY", order_type="OrderType.MARKET",
        filled_qty="4", filled_avg_price="100.0", stop_price=None,
        filled_at=t0, submitted_at=t0, created_at=t0,
        client_order_id="DAY-A-NVDA-1",
    )
    long_tp = SimpleNamespace(
        symbol="NVDA", side="OrderSide.SELL", order_type="OrderType.LIMIT",
        filled_qty="4", filled_avg_price="102.0", stop_price=None,
        filled_at=t1, submitted_at=t1, created_at=t1,
        client_order_id=None,
    )
    short_entry = SimpleNamespace(
        symbol="NVDA", side="OrderSide.SELL", order_type="OrderType.MARKET",
        filled_qty="4", filled_avg_price="99.0", stop_price=None,
        filled_at=t2, submitted_at=t2, created_at=t2,
        client_order_id="DAY-BS-NVDA-2",
    )
    short_cover = SimpleNamespace(
        symbol="NVDA", side="OrderSide.BUY", order_type="OrderType.MARKET",
        filled_qty="4", filled_avg_price="98.5", stop_price=None,
        filled_at=t3, submitted_at=t3, created_at=t3,
        client_order_id=None,
    )
    trades = _trade_walk_for_symbol([long_entry, long_tp, short_entry, short_cover])
    assert len(trades) == 2
    assert trades[0]["direction"] == "long"
    assert trades[1]["direction"] == "short"
    assert _trade_pl(trades[0]) == pytest.approx(8.0)   # 4 × (102−100)
    assert _trade_pl(trades[1]) == pytest.approx(2.0)   # 4 × (99−98.5)


# ---------------------------------------------------------------------------
# New risk gates: weekly loss, consecutive-loss cooldowns, spread
# ---------------------------------------------------------------------------


class _PortfolioHistoryClient:
    def __init__(self, equities):
        self._equities = equities

    # Mirrors the real alpaca-py signature: the parameter is named
    # `history_filter`, NOT `filter` like get_orders. The production bug
    # this pins down: calling with filter= raised TypeError on every scan
    # and silently fail-opened the weekly-loss/drawdown gates.
    def get_portfolio_history(self, history_filter=None):
        return SimpleNamespace(equity=self._equities)


def test_weekly_loss_gate_blocks_at_minus_4pct():
    client = _PortfolioHistoryClient([100_000, 98_000, 95_900])
    decision = _gate_weekly_loss(client)
    assert decision is not None
    assert decision.allowed is False
    assert "weekly_loss_cap" in decision.reason


def test_weekly_loss_gate_passes_when_flat():
    client = _PortfolioHistoryClient([100_000, 99_500, 100_200])
    assert _gate_weekly_loss(client) is None


def test_weekly_loss_gate_fails_open_on_infra_error():
    assert _gate_weekly_loss(FakeClient()) is None  # no portfolio history attr


def _now_et_fixed():
    return datetime(2026, 5, 28, 14, 0, tzinfo=_ET)


def test_cooldown_blocks_after_two_losses_today():
    stats = {"sessions": {
        "2026-05-28": {"net_pl": -120.0, "closed": 2, "losing": 2},
    }}
    decision = _gate_consecutive_losses(stats, now_et=_now_et_fixed())
    assert decision is not None
    assert "session_loss_cooldown" in decision.reason


def test_cooldown_allows_one_loss_today():
    stats = {"sessions": {
        "2026-05-28": {"net_pl": -60.0, "closed": 1, "losing": 1},
    }}
    assert _gate_consecutive_losses(stats, now_et=_now_et_fixed()) is None


def test_cooldown_blocks_after_three_losing_sessions():
    stats = {"sessions": {
        "2026-05-22": {"net_pl": -50.0, "closed": 1, "losing": 1},
        "2026-05-26": {"net_pl": -80.0, "closed": 2, "losing": 1},
        "2026-05-27": {"net_pl": -30.0, "closed": 1, "losing": 1},
    }}
    decision = _gate_consecutive_losses(stats, now_et=_now_et_fixed())
    assert decision is not None
    assert "losing_streak_pause" in decision.reason


def test_cooldown_streak_expires_after_pause_window():
    stats = {"sessions": {
        "2026-05-12": {"net_pl": -50.0, "closed": 1, "losing": 1},
        "2026-05-13": {"net_pl": -80.0, "closed": 2, "losing": 1},
        "2026-05-14": {"net_pl": -30.0, "closed": 1, "losing": 1},
    }}
    # Last losing session was 14 days before now — pause has lapsed.
    assert _gate_consecutive_losses(stats, now_et=_now_et_fixed()) is None


def test_cooldown_no_block_when_one_recent_session_won():
    stats = {"sessions": {
        "2026-05-22": {"net_pl": -50.0, "closed": 1, "losing": 1},
        "2026-05-26": {"net_pl": 90.0, "closed": 2, "losing": 1},
        "2026-05-27": {"net_pl": -30.0, "closed": 1, "losing": 1},
    }}
    assert _gate_consecutive_losses(stats, now_et=_now_et_fixed()) is None


def test_cooldown_fails_open_without_stats():
    assert _gate_consecutive_losses(None, now_et=_now_et_fixed()) is None
    assert _gate_consecutive_losses({"error": "boom"}, now_et=_now_et_fixed()) is None


def _wide_quote(monkeypatch):
    import src.day_data as day_data_mod
    monkeypatch.setattr(
        day_data_mod, "get_stock_latest_quote",
        lambda s: SimpleNamespace(bid_price=100.0, ask_price=100.5),
    )
    return day_data_mod


def test_spread_gate_blocks_wide_spread_with_stale_tape(monkeypatch):
    day_data_mod = _wide_quote(monkeypatch)
    stale_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
    monkeypatch.setattr(
        day_data_mod, "get_stock_latest_trade",
        lambda s: SimpleNamespace(timestamp=stale_ts, price=100.2),
    )
    decision = _gate_spread("NVDA")
    assert decision is not None
    assert decision.allowed is False
    assert "tape_stale" in decision.reason


def test_spread_gate_passes_wide_spread_with_fresh_tape(monkeypatch):
    # The MSFT 2026-07-06 case: IEX prints an artifact multi-dollar-wide
    # quote on an ultra-liquid name while trades keep printing. A fresh
    # tape proves the market is alive — the wide quote is not trusted.
    day_data_mod = _wide_quote(monkeypatch)
    fresh_ts = datetime.now(timezone.utc) - timedelta(seconds=5)
    monkeypatch.setattr(
        day_data_mod, "get_stock_latest_trade",
        lambda s: SimpleNamespace(timestamp=fresh_ts, price=100.2),
    )
    assert _gate_spread("MSFT") is None


def test_spread_gate_blocks_wide_spread_when_trade_fetch_fails(monkeypatch):
    # Wide quote + unknown tape → fail toward skipping (the quote is the
    # only evidence we have, and it says the market is wide).
    day_data_mod = _wide_quote(monkeypatch)

    def _boom(s):
        raise RuntimeError("api down")

    monkeypatch.setattr(day_data_mod, "get_stock_latest_trade", _boom)
    decision = _gate_spread("NVDA")
    assert decision is not None
    assert decision.allowed is False


def test_spread_gate_passes_tight_spread(monkeypatch):
    import src.day_data as day_data_mod
    monkeypatch.setattr(
        day_data_mod, "get_stock_latest_quote",
        lambda s: SimpleNamespace(bid_price=100.00, ask_price=100.02),
    )
    assert _gate_spread("NVDA") is None


def test_spread_gate_fails_open_on_zero_quote(monkeypatch):
    import src.day_data as day_data_mod
    monkeypatch.setattr(
        day_data_mod, "get_stock_latest_quote",
        lambda s: SimpleNamespace(bid_price=0.0, ask_price=100.0),
    )
    assert _gate_spread("NVDA") is None


def test_compute_week_pnl_pct():
    from src.day_trader import compute_week_pnl_pct
    assert compute_week_pnl_pct(
        _PortfolioHistoryClient([100_000, 102_000])
    ) == pytest.approx(2.0)
    assert compute_week_pnl_pct(_PortfolioHistoryClient([100_000])) is None
    assert compute_week_pnl_pct(FakeClient()) is None  # no history attr
