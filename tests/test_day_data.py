"""Tests for src/day_data.py — focused on the SIP/IEX feed bug regression.

The full Alpaca round-trip isn't tested here (would need network); we
only verify that get_stock_bars passes the correct feed parameter to
alpaca-py so the free-tier "subscription does not permit querying
recent SIP data" error can't silently come back.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src import day_data


def test_get_stock_bars_passes_iex_feed(monkeypatch):
    """The free-tier paper feed rejects SIP requests for recent data with
    'subscription does not permit querying recent SIP data'. Without an
    explicit feed=IEX argument, alpaca-py defaults to SIP and the
    day-watcher crashes on every intraday scan tick.

    Lock the contract: get_stock_bars MUST pass feed=DataFeed.IEX.
    """
    captured_requests = []

    class FakeBarset:
        @property
        def df(self):
            import pandas as pd
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"],
                index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
            )

    class FakeClient:
        def get_stock_bars(self, request):
            captured_requests.append(request)
            return FakeBarset()

    monkeypatch.setattr(day_data, "_get_stock_data_client", lambda: FakeClient())

    day_data.get_stock_bars(
        "NVDA", "5Min",
        start=datetime(2026, 5, 29, 13, 30, tzinfo=timezone.utc),
        end=datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc),
    )
    assert len(captured_requests) == 1
    req = captured_requests[0]
    feed = getattr(req, "feed", None)
    assert feed is not None, "get_stock_bars did not set the feed argument"
    # alpaca-py exposes DataFeed as an enum with .value=='iex' for IEX.
    feed_str = str(feed).lower()
    assert "iex" in feed_str, f"expected IEX feed, got {feed!r}"


def test_get_stock_bars_rejects_unknown_timeframe():
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        day_data.get_stock_bars(
            "NVDA", "30Min",
            start=datetime(2026, 5, 29, tzinfo=timezone.utc),
        )
