"""Alpaca stock-data wrappers + yfinance pre-market helper.

Mirrors src/data.py for crypto, but for the day-trade universe:
- get_stock_bars(...) — historical OHLCV via Alpaca's IEX feed.
- get_pre_market_high_low(...) — yfinance fallback for PMH/PML
  (Alpaca's free tier doesn't carry pre-market data — see D-doc
  §"Data fidelity note"). Returns None on any failure; pre-market
  data is descriptive context, never a setup trigger.

No silent fallbacks for Alpaca calls — anything that errors propagates
so the day-watcher can decide whether to skip a scan or alert.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from src.data import _require_env

_TIMEFRAME_MAP = {
    "1Day":  TimeFrame(amount=1, unit=TimeFrameUnit.Day),
    "1Hour": TimeFrame(amount=1, unit=TimeFrameUnit.Hour),
    "5Min":  TimeFrame(amount=5, unit=TimeFrameUnit.Minute),
    "1Min":  TimeFrame(amount=1, unit=TimeFrameUnit.Minute),
}


def _get_stock_data_client() -> StockHistoricalDataClient:
    api_key = _require_env("ALPACA_API_KEY")
    secret_key = _require_env("ALPACA_SECRET_KEY")
    return StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)


def get_stock_bars(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Pull historical stock bars and return a DataFrame.

    timeframe ∈ {'1Day','1Hour','5Min','1Min'}. Returns columns
    [open, high, low, close, volume] indexed by tz-aware UTC timestamp.
    Caller is responsible for choosing start/end windows that match
    the desired number of bars; the IEX free feed honors them.
    """
    if timeframe not in _TIMEFRAME_MAP:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}. "
            f"Use one of: {list(_TIMEFRAME_MAP)}"
        )

    client = _get_stock_data_client()
    # feed=IEX is mandatory on the free tier. Without it alpaca-py
    # defaults to SIP, which the free subscription rejects for any
    # recent (last 15 min) data with:
    #   "subscription does not permit querying recent SIP data"
    # Strategy doc §"Data fidelity note" already accepts the IEX-only
    # caveat — this just makes the request explicit.
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=_TIMEFRAME_MAP[timeframe],
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    barset = client.get_stock_bars(request)
    df = barset.df
    if df is None or len(df) == 0:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )

    # alpaca-py returns a MultiIndex (symbol, timestamp) when single-symbol —
    # collapse to single timestamp index.
    if isinstance(df.index, pd.MultiIndex):
        df = df.droplevel(0)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def get_stock_latest_quote(symbol: str):
    """Latest top-of-book bid/ask for `symbol` from the IEX feed (free
    tier). Raises on API failure — the caller (spread gate) treats any
    failure as fail-open, since IEX quote gaps are a data quirk, not a
    trade veto.
    """
    client = _get_stock_data_client()
    quotes = client.get_stock_latest_quote(StockLatestQuoteRequest(
        symbol_or_symbols=symbol, feed=DataFeed.IEX,
    ))
    if symbol not in quotes:
        raise RuntimeError(f"No quote returned for {symbol}")
    return quotes[symbol]


def get_pre_market_high_low(
    symbol: str,
    session_date_et: date,
) -> tuple[float, float] | None:
    """Return (PMH, PML) for `symbol` on `session_date_et` from yfinance,
    or None if the fetch fails or yfinance returns no pre-market bars.

    Strategy doc: pre-market data is descriptive context only — never
    a setup trigger. Watcher proceeds without these levels on failure.

    Failure reasons are printed to stderr so they're visible in the GH
    Actions logs — the alert just shows "unavailable" but the logs
    distinguish install gap vs Yahoo outage vs empty data.
    """
    try:
        import yfinance as yf  # local import — keeps the watcher tolerant
                               # of yfinance being absent in test envs
    except ImportError as exc:
        print(f"[day_data] PMH/PML unavailable ({symbol}): yfinance not installed "
              f"({exc}) — add to requirements.txt", file=sys.stderr)
        return None

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d", interval="1m", prepost=True)
        if hist is None or len(hist) == 0:
            print(f"[day_data] PMH/PML unavailable ({symbol}): yfinance returned empty",
                  file=sys.stderr)
            return None

        hist = hist.reset_index()
        hist["Datetime_ET"] = pd.to_datetime(hist["Datetime"]).dt.tz_convert(
            "America/New_York"
        )
        # Pre-market is 04:00–09:30 ET on the requested session date.
        same_day = hist["Datetime_ET"].dt.date == session_date_et
        before_open = (hist["Datetime_ET"].dt.hour < 9) | (
            (hist["Datetime_ET"].dt.hour == 9)
            & (hist["Datetime_ET"].dt.minute < 30)
        )
        pre = hist[same_day & before_open]
        if len(pre) == 0:
            print(f"[day_data] PMH/PML unavailable ({symbol}): no pre-market bars "
                  f"for {session_date_et}", file=sys.stderr)
            return None
        return float(pre["High"].max()), float(pre["Low"].min())
    except Exception as exc:
        print(f"[day_data] PMH/PML unavailable ({symbol}): yfinance error: {exc}",
              file=sys.stderr)
        return None
