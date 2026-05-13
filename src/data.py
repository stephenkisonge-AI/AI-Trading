"""Alpaca data + trading client wrappers. No silent fallbacks — every failure raises."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest


_TIMEFRAME_MAP = {
    "1Day": (TimeFrame(amount=1, unit=TimeFrameUnit.Day), timedelta(days=1)),
    "4Hour": (TimeFrame(amount=4, unit=TimeFrameUnit.Hour), timedelta(hours=4)),
    "1Hour": (TimeFrame(amount=1, unit=TimeFrameUnit.Hour), timedelta(hours=1)),
}


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"Required env var {name} is not set")
    return value


def _assert_paper_mode() -> None:
    """Hard-check that ALPACA_PAPER_TRADE is exactly the string 'True'.
    The watcher refuses to do anything otherwise — this is the single
    guardrail preventing accidental live trading.
    """
    flag = os.environ.get("ALPACA_PAPER_TRADE")
    if flag != "True":
        raise RuntimeError(
            f"ALPACA_PAPER_TRADE must be exactly the string 'True'. Got: {flag!r}"
        )


def get_client() -> TradingClient:
    """Return a paper-mode TradingClient. Raises if ALPACA_PAPER_TRADE != 'True'."""
    _assert_paper_mode()
    api_key = _require_env("ALPACA_API_KEY")
    secret_key = _require_env("ALPACA_SECRET_KEY")
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=True)


def _get_crypto_data_client() -> CryptoHistoricalDataClient:
    api_key = _require_env("ALPACA_API_KEY")
    secret_key = _require_env("ALPACA_SECRET_KEY")
    return CryptoHistoricalDataClient(api_key=api_key, secret_key=secret_key)


def get_bars(symbol: str, timeframe: str, limit: int = 250) -> pd.DataFrame:
    """Pull historical crypto bars and return a DataFrame.

    timeframe must be one of '1Day', '4Hour', '1Hour'. Returns columns
    [open, high, low, close, volume] indexed by timestamp (UTC). The
    most recent `limit` closed bars are returned (newest last).
    """
    if timeframe not in _TIMEFRAME_MAP:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}. Use one of: {list(_TIMEFRAME_MAP)}"
        )

    tf, bar_delta = _TIMEFRAME_MAP[timeframe]
    # Fetch a generous window (3x) to absorb gaps and ensure `limit` candles available
    start = datetime.now(timezone.utc) - bar_delta * limit * 3

    client = _get_crypto_data_client()
    request = CryptoBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start,
    )
    barset = client.get_crypto_bars(request)
    df = barset.df
    if df is None or df.empty:
        raise RuntimeError(f"No bars returned for {symbol} @ {timeframe}")

    # alpaca-py returns a MultiIndex (symbol, timestamp). Flatten to timestamp only.
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")

    keep = ["open", "high", "low", "close", "volume"]
    missing = [c for c in keep if c not in df.columns]
    if missing:
        raise RuntimeError(f"Bars DataFrame missing columns {missing} for {symbol}")
    df = df[keep].sort_index().tail(limit)
    return df


def get_account():
    """Account snapshot from Alpaca (paper). Raises on API failure."""
    return get_client().get_account()


def get_positions():
    """All open positions. Returns list (possibly empty)."""
    return get_client().get_all_positions()


def get_open_orders(symbol: Optional[str] = None):
    """Open orders, optionally filtered to a single symbol."""
    request_kwargs = {"status": QueryOrderStatus.OPEN}
    if symbol is not None:
        request_kwargs["symbols"] = [symbol]
    return get_client().get_orders(filter=GetOrdersRequest(**request_kwargs))
