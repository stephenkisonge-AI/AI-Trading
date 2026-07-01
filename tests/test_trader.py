"""Tests for src/trader.py cross-strand scoping.

The crypto swing strand and the day-trade strand share one Alpaca paper
account. Each strand must only count and manage its OWN symbols — these
tests pin that boundary from the crypto side (the day side's mirror
tests live in tests/test_day_trader.py).
"""
from types import SimpleNamespace
from unittest.mock import patch

from src.trader import _gate_daily_entry_cap, manage_open_positions


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
