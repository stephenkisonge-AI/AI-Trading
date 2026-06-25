"""Tests for src/day_watcher.py — phase detection, eligibility resolution,
gap math. Network-free; the orchestration that calls Alpaca/yfinance is
tested via monkeypatched fakes elsewhere.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import math
import pandas as pd
import pytest

from src.day_calendar import EarningsPayload, EconEventsPayload
from src.day_watcher import (
    PHASE_INTRADAY_SCAN,
    PHASE_OPENING_OBSERVE,
    PHASE_OUTSIDE,
    PHASE_PRE_SESSION,
    _overnight_gap_pct,
    _resolve_eligibility,
    determine_phase,
)

_ET = ZoneInfo("America/New_York")
_UTC = timezone.utc


def _et(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=_ET)


def _fresh_earnings(earnings: dict[str, list[date]] | None = None) -> EarningsPayload:
    return EarningsPayload(
        refreshed_at=datetime(2026, 5, 27, tzinfo=_UTC),
        source="test",
        earnings=earnings or {},
    )


def _fresh_econ(events: list[tuple[str, datetime]] | None = None) -> EconEventsPayload:
    return EconEventsPayload(
        refreshed_at=datetime(2026, 5, 27, tzinfo=_UTC),
        source="test",
        events=events or [],
    )


# ---------------------------------------------------------------------------
# determine_phase
# ---------------------------------------------------------------------------


def test_phase_pre_session_only_fires_at_925():
    # Pre-session is a single-tick window [9:25, 9:30) — exactly one cron
    # firing per weekday morning sends the summary.
    assert determine_phase(_et(date(2026, 5, 27), time(9, 25))) is PHASE_PRE_SESSION


def test_phase_outside_before_925():
    # Earlier ticks in the cron window (9:00-9:20 ET) used to fire the
    # full pre-session summary too — leading to 6 redundant Telegrams
    # per morning. They now map to PHASE_OUTSIDE and no-op.
    assert determine_phase(_et(date(2026, 5, 27), time(9, 0))) is PHASE_OUTSIDE
    assert determine_phase(_et(date(2026, 5, 27), time(9, 20))) is PHASE_OUTSIDE
    # 9:24 is the last tick before the window opens — still OUTSIDE.
    assert determine_phase(_et(date(2026, 5, 27), time(9, 24))) is PHASE_OUTSIDE


def test_phase_pre_session_boundary_at_930_exclusive():
    # 09:30 ET is the regular session open, not pre-session.
    assert determine_phase(_et(date(2026, 5, 27), time(9, 30))) is PHASE_OPENING_OBSERVE


def test_phase_opening_observe_between_930_and_945():
    assert determine_phase(_et(date(2026, 5, 27), time(9, 40))) is PHASE_OPENING_OBSERVE


def test_phase_intraday_scan_at_1000():
    assert determine_phase(_et(date(2026, 5, 27), time(10, 0))) is PHASE_INTRADAY_SCAN


def test_phase_intraday_scan_at_1554():
    assert determine_phase(_et(date(2026, 5, 27), time(15, 54))) is PHASE_INTRADAY_SCAN


def test_phase_intraday_scan_at_1555_so_hard_close_can_fire():
    # The 15:55 ET tick MUST land inside the intraday window so
    # manage_open_positions runs the 3:55 PM hard close. The previous
    # boundary at 15:55 (exclusive) made the hard-close branch unreachable.
    assert determine_phase(_et(date(2026, 5, 27), time(15, 55))) is PHASE_INTRADAY_SCAN
    assert determine_phase(_et(date(2026, 5, 27), time(15, 59))) is PHASE_INTRADAY_SCAN


def test_phase_outside_at_market_close():
    # 16:00 ET = market close. No further management actions are useful.
    assert determine_phase(_et(date(2026, 5, 27), time(16, 0))) is PHASE_OUTSIDE
    assert determine_phase(_et(date(2026, 5, 27), time(16, 1))) is PHASE_OUTSIDE


def test_phase_outside_at_3am():
    assert determine_phase(_et(date(2026, 5, 27), time(3, 0))) is PHASE_OUTSIDE


def test_end_of_session_window_includes_1550_excludes_1555():
    """End-of-session summary only fires inside [15:50, 15:55) ET — the
    single 5-min tick before the 15:55 hard close. Earlier intraday
    ticks log to stdout only; 15:55 itself is still intraday but runs
    management (hard close) instead of resending the EOS summary.
    """
    from src.day_watcher import _END_OF_SESSION_START, _END_OF_SESSION_END
    # The cron fires on 5-min boundaries. 15:50 is inside; 15:55 is the
    # exclusive end so only one tick (15:50) lands in the window.
    assert _END_OF_SESSION_START == time(15, 50)
    assert _END_OF_SESSION_END == time(15, 55)
    assert _END_OF_SESSION_START <= time(15, 50) < _END_OF_SESSION_END
    assert not (_END_OF_SESSION_START <= time(15, 45) < _END_OF_SESSION_END)
    assert not (_END_OF_SESSION_START <= time(15, 55) < _END_OF_SESSION_END)


def test_phase_outside_during_dead_zone_is_actually_intraday():
    # The doc's "midday dead zone" 11:30-14:00 — for our phase logic
    # this is still PHASE_INTRADAY_SCAN, because the watcher's time-of-day
    # filtering happens inside the setup evaluators (Setup B's window).
    # The watcher always runs the scan; the setups self-reject.
    assert determine_phase(_et(date(2026, 5, 27), time(13, 0))) is PHASE_INTRADAY_SCAN


def test_phase_requires_tz_aware():
    naive = datetime(2026, 5, 27, 10, 0)
    with pytest.raises(ValueError):
        determine_phase(naive)


# ---------------------------------------------------------------------------
# _overnight_gap_pct
# ---------------------------------------------------------------------------


def _daily(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1.0] * len(closes)},
        index=pd.DatetimeIndex(pd.date_range("2026-05-22", periods=len(closes), tz="UTC")),
    )


def _5min_today(opens: list[float]) -> pd.DataFrame:
    base = pd.Timestamp("2026-05-27 13:30", tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": opens, "low": opens, "close": opens, "volume": [1.0] * len(opens)},
        index=pd.DatetimeIndex([base + pd.Timedelta(minutes=5 * i) for i in range(len(opens))]),
    )


def test_overnight_gap_positive():
    yesterday = _daily([100.0])
    today = _5min_today([105.0])  # 5% gap up
    assert _overnight_gap_pct(yesterday, today) == pytest.approx(0.05)


def test_overnight_gap_negative():
    yesterday = _daily([100.0])
    today = _5min_today([96.0])  # 4% gap down
    assert _overnight_gap_pct(yesterday, today) == pytest.approx(-0.04)


def test_overnight_gap_zero_when_either_side_missing():
    empty_daily = pd.DataFrame(columns=["close"]).astype({"close": float})
    today = _5min_today([100.0])
    assert _overnight_gap_pct(empty_daily, today) == 0.0


def test_overnight_gap_zero_when_yesterday_close_is_zero():
    yesterday = _daily([0.0])
    today = _5min_today([100.0])
    assert _overnight_gap_pct(yesterday, today) == 0.0


# ---------------------------------------------------------------------------
# _resolve_eligibility
# ---------------------------------------------------------------------------


def test_eligibility_all_eligible_when_clean():
    now = _et(date(2026, 5, 27), time(9, 25))
    eligible, blocked, econ = _resolve_eligibility(
        now_et=now,
        earnings_payload=_fresh_earnings(),
        econ_payload=_fresh_econ(),
    )
    assert set(eligible) == {"NVDA", "TSLA", "AAPL", "AMZN", "GOOGL", "MSFT", "GLD"}
    assert blocked == []
    assert econ is None


def test_eligibility_excludes_earnings_blackouts():
    now = _et(date(2026, 5, 27), time(9, 25))
    # NVDA reports today, TSLA reported yesterday — both blocked.
    eligible, blocked, _ = _resolve_eligibility(
        now_et=now,
        earnings_payload=_fresh_earnings({
            "NVDA": [date(2026, 5, 27)],
            "TSLA": [date(2026, 5, 26)],
        }),
        econ_payload=_fresh_econ(),
    )
    assert "NVDA" not in eligible
    assert "TSLA" not in eligible
    assert ("NVDA", "earnings") in blocked
    assert ("TSLA", "earnings") in blocked


def test_eligibility_gld_skips_earnings_filter():
    now = _et(date(2026, 5, 27), time(9, 25))
    # An earnings entry for GLD is meaningless (ETF), but even if some
    # bad refresh job inserts one, the eligibility resolver must not
    # exclude GLD on that basis.
    eligible, blocked, _ = _resolve_eligibility(
        now_et=now,
        earnings_payload=_fresh_earnings({"GLD": [date(2026, 5, 27)]}),
        econ_payload=_fresh_econ(),
    )
    assert "GLD" in eligible
    assert ("GLD", "earnings") not in blocked


def test_eligibility_stale_calendar_blocks_everything():
    now = _et(date(2026, 5, 27), time(9, 25))
    # 30-day-old earnings payload → stale → all blocked.
    stale = EarningsPayload(
        refreshed_at=datetime(2026, 4, 25, tzinfo=_UTC),
        source="test", earnings={},
    )
    eligible, blocked, _ = _resolve_eligibility(
        now_et=now, earnings_payload=stale, econ_payload=_fresh_econ(),
    )
    assert eligible == []
    assert all(reason == "stale_calendar" for _, reason in blocked)
    from src.universe import UNIVERSE
    assert len(blocked) == len(UNIVERSE)


def test_eligibility_econ_event_today_surfaced():
    now = _et(date(2026, 5, 27), time(9, 25))
    cpi_at_830_et = datetime(2026, 5, 27, 12, 30, tzinfo=_UTC)  # 08:30 ET
    eligible, _, econ = _resolve_eligibility(
        now_et=now,
        earnings_payload=_fresh_earnings(),
        econ_payload=_fresh_econ([("CPI", cpi_at_830_et)]),
    )
    assert econ == "CPI"
    # Econ event surfaces the warning but doesn't block eligibility —
    # the per-scan path applies the 30-min blackout.
    from src.universe import UNIVERSE
    assert len(eligible) == len(UNIVERSE)
