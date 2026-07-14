"""Tests for src/watcher.py helpers — closed-bar slicing."""
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.watcher import _drop_in_progress_candle

# Cron fires at :17 of every 4th hour — a mid-window scan time.
NOW = datetime(2026, 7, 14, 8, 17, tzinfo=timezone.utc)


def _bars(start: datetime, period: timedelta, n: int) -> pd.DataFrame:
    idx = pd.DatetimeIndex([start + i * period for i in range(n)], tz="UTC")
    return pd.DataFrame({"close": [100.0] * n}, index=idx)


def test_partial_bar_is_dropped():
    # 1H bars ending with the in-progress 08:00 bucket at the 08:17 scan.
    df = _bars(datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc),
               timedelta(hours=1), 9)   # 00:00 .. 08:00
    out = _drop_in_progress_candle(df, timedelta(hours=1), now_utc=NOW)
    assert len(out) == 8
    assert out.index[-1] == pd.Timestamp("2026-07-14 07:00", tz="UTC")


def test_thin_symbol_without_partial_keeps_last_completed_bar():
    # Zero trades since 08:00 -> Alpaca returns no partial bucket; the
    # 07:00 bar is already complete and must survive. The old blind
    # iloc[:-1] cut evaluated one completed bar behind (Phase 6 finding).
    df = _bars(datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc),
               timedelta(hours=1), 8)   # 00:00 .. 07:00, all closed
    out = _drop_in_progress_candle(df, timedelta(hours=1), now_utc=NOW)
    assert len(out) == 8
    assert out.index[-1] == pd.Timestamp("2026-07-14 07:00", tz="UTC")


def test_bar_exactly_at_close_is_kept():
    # start + period == now -> the window just ended -> closed.
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    df = _bars(datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc),
               timedelta(hours=1), 5)   # 03:00 .. 07:00
    out = _drop_in_progress_candle(df, timedelta(hours=1), now_utc=now)
    assert len(out) == 5


def test_4h_and_daily_periods():
    # 4H: at 08:17 the 08:00 bucket is 17 min into its window; the bar
    # that completed at 08:00 (started 04:00) is the newest closed one.
    h4 = _bars(datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc),
               timedelta(hours=4), 9)   # .. 2026-07-14 08:00
    out4 = _drop_in_progress_candle(h4, timedelta(hours=4), now_utc=NOW)
    assert out4.index[-1] == pd.Timestamp("2026-07-14 04:00", tz="UTC")
    # Daily: today's 00:00 bar is 8h17m into its 24h window -> dropped.
    d = _bars(datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
              timedelta(days=1), 5)     # 07-10 .. 07-14
    outd = _drop_in_progress_candle(d, timedelta(days=1), now_utc=NOW)
    assert outd.index[-1] == pd.Timestamp("2026-07-13 00:00", tz="UTC")


def test_empty_df_passthrough():
    empty = pd.DataFrame({"close": []}, index=pd.DatetimeIndex([], tz="UTC"))
    out = _drop_in_progress_candle(empty, timedelta(hours=1), now_utc=NOW)
    assert len(out) == 0
