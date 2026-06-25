"""Day-trade universe — single source of truth.

The watcher (`src/day_watcher.py`), the trader (`src/day_trader.py`),
and the calendar refresh script (`scripts/refresh_day_calendars.py`)
all consume these constants. Adding or removing a symbol here is
sufficient — there's no other location to update.

ETFs are excluded from the earnings filter because they have no
quarterly earnings to gap on. Stocks must all be in `STOCKS_WITH_EARNINGS`
so the watcher can block them on report days.
"""
from __future__ import annotations


# Liquid, low-spread names with reliable intraday opening-range and
# VWAP behavior. Mega-caps + sector ETFs. No leveraged ETFs (slippage
# risk on 3x funds) and no small-caps (can't absorb $30K orders cleanly).
UNIVERSE: list[str] = [
    # Mega-cap tech (original universe)
    "NVDA", "TSLA", "AAPL", "AMZN", "GOOGL", "MSFT",
    # Tech additions
    "META", "NFLX",
    # Semiconductors
    "AMD", "AVGO", "MU",
    # Financials
    "JPM", "V",
    # Healthcare / pharma
    "LLY", "UNH",
    # Consumer / staples
    "WMT", "COST",
    # Energy
    "XOM",
    # Broad-market ETFs (ultra-liquid)
    "QQQ", "SPY",
    # Gold (uncorrelated to equity regime)
    "GLD",
]

# Symbols subject to the earnings-blackout filter. ETFs have no earnings.
ETFS: set[str] = {"QQQ", "SPY", "GLD"}
STOCKS_WITH_EARNINGS: set[str] = {s for s in UNIVERSE if s not in ETFS}
