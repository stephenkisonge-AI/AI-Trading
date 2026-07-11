import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _healthy_day_market_data(monkeypatch):
    """The day-strand spread gate FAILS CLOSED on quote/tape fetch
    problems (Addendum B audit) — without stubs, every gate test would
    block at the spread gate when it hits the network-less test env.
    Give all tests healthy market data by default; tests that exercise
    the spread gate override these inside their own bodies.
    """
    import src.day_data as day_data_mod

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        day_data_mod, "get_stock_latest_quote",
        lambda s: SimpleNamespace(bid_price=100.0, ask_price=100.02,
                                  timestamp=now))
    monkeypatch.setattr(
        day_data_mod, "get_stock_latest_trade",
        lambda s: SimpleNamespace(price=100.01, timestamp=now))
