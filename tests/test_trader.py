"""Tests for src/trader.py cross-strand scoping.

The crypto swing strand and the day-trade strand share one Alpaca paper
account. Each strand must only count and manage its OWN symbols — these
tests pin that boundary from the crypto side (the day side's mirror
tests live in tests/test_day_trader.py).
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from src.trader import _gate_daily_entry_cap, manage_open_positions, summarize_lifecycle


class _OrdersClient:
    def __init__(self, orders):
        self._orders = orders

    def get_orders(self, filter=None):
        return list(self._orders)


def _filled_buy(symbol):
    return SimpleNamespace(symbol=symbol, side="OrderSide.BUY", filled_qty="1")


def test_daily_entry_cap_ignores_day_trade_equity_buys():
    # A day-trade NVDA entry earlier today must not consume the crypto
    # strand's 1-entry/day budget.
    client = _OrdersClient([_filled_buy("NVDA"), _filled_buy("TSLA")])
    assert _gate_daily_entry_cap(client) is None


def test_daily_entry_cap_trips_on_crypto_buy_slash_form():
    client = _OrdersClient([_filled_buy("BTC/USD")])
    decision = _gate_daily_entry_cap(client)
    assert decision is not None
    assert decision.allowed is False


def test_daily_entry_cap_trips_on_crypto_buy_no_slash_form():
    client = _OrdersClient([_filled_buy("BTCUSD")])
    decision = _gate_daily_entry_cap(client)
    assert decision is not None
    assert decision.allowed is False


def _closed_order(symbol, side, qty, price, ts):
    return SimpleNamespace(
        symbol=symbol, side=f"OrderSide.{side}", order_type="OrderType.MARKET",
        filled_qty=str(qty), filled_avg_price=str(price), stop_price=None,
        filled_at=ts, submitted_at=ts, created_at=ts,
    )


def test_lifecycle_excludes_day_trade_equity_orders(monkeypatch):
    # Before the symbol filter, summarize_lifecycle reconstructed the day
    # strand's NFLX round-trip as a crypto trade — the crypto STAND DOWN
    # alert then reported the day strand's P&L/R stats as its own
    # (observed 2026-07-07: near-identical stats in both strands' alerts).
    monkeypatch.setenv("WATCHER_AUTO_EXECUTE", "true")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "True")
    t1 = datetime(2026, 7, 6, 15, 11, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 6, 15, 45, tzinfo=timezone.utc)
    orders = [
        _closed_order("NFLX", "BUY", 392, 76.64, t1),
        _closed_order("NFLX", "SELL", 392, 76.28, t2),
        _closed_order("BTC/USD", "BUY", 0.005, 100_000.0, t1),
        _closed_order("BTC/USD", "SELL", 0.005, 101_000.0, t2),
    ]
    with patch("src.trader.get_client", return_value=_OrdersClient(orders)):
        stats = summarize_lifecycle()
    assert stats["total_closed"] == 1
    assert stats["total_pl_usd"] == 5.0  # 0.005 × (101000 − 100000); no NFLX


def test_crypto_management_skips_equity_positions(monkeypatch):
    # A day-trade (or manual) equity position must be invisible to the
    # crypto strand's regime-exit / time-stop / breakeven management.
    monkeypatch.setenv("WATCHER_AUTO_EXECUTE", "true")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "True")

    class _PosClient:
        def get_all_positions(self):
            return [SimpleNamespace(symbol="NVDA", qty="5", avg_entry_price="100")]

    with patch("src.trader.get_client", return_value=_PosClient()):
        actions = manage_open_positions()
    assert actions == []
