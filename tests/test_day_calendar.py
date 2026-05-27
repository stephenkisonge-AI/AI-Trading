"""Tests for src/day_calendar.py — loader + predicates for the day-trade
strategy's external state files (state/earnings.json, state/econ_events.json).
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.day_calendar import (
    EarningsPayload,
    EconEventsPayload,
    is_earnings_blackout,
    is_econ_blackout,
    is_stale,
    load_earnings,
    load_econ_events,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Load — happy path against the committed seed files
# ---------------------------------------------------------------------------


def test_load_earnings_against_seed_file():
    payload = load_earnings()
    assert isinstance(payload, EarningsPayload)
    # Universe is six stocks; ETFs (GLD) are intentionally absent.
    assert set(payload.earnings) == {"NVDA", "TSLA", "AAPL", "AMZN", "GOOGL", "MSFT"}
    for symbol, dates in payload.earnings.items():
        assert all(isinstance(d, date) for d in dates), f"{symbol} has non-date entries"


def test_load_econ_events_against_seed_file():
    payload = load_econ_events()
    assert isinstance(payload, EconEventsPayload)
    assert len(payload.events) > 0
    names = {name for name, _ in payload.events}
    # Seed must contain at least one of each tier-1 release type we care about.
    assert names & {"FOMC", "CPI", "PCE", "NFP", "GDP"}
    for _, ts in payload.events:
        assert ts.tzinfo is not None, "event timestamps must be tz-aware"


def test_load_earnings_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_earnings(tmp_path / "does_not_exist.json")


# ---------------------------------------------------------------------------
# is_stale — fail-closed posture
# ---------------------------------------------------------------------------


def _earnings_payload(refreshed_at: datetime) -> EarningsPayload:
    return EarningsPayload(refreshed_at=refreshed_at, source="test", earnings={})


def _econ_payload(refreshed_at: datetime) -> EconEventsPayload:
    return EconEventsPayload(refreshed_at=refreshed_at, source="test", events=[])


def test_is_stale_returns_false_when_fresh():
    now = datetime(2026, 5, 27, tzinfo=UTC)
    payload = _earnings_payload(now - timedelta(days=2))
    assert is_stale(payload, now=now) is False


def test_is_stale_returns_true_just_past_cutoff():
    now = datetime(2026, 5, 27, tzinfo=UTC)
    payload = _earnings_payload(now - timedelta(days=10, seconds=1))
    assert is_stale(payload, now=now) is True


def test_is_stale_boundary_exactly_at_cutoff_is_fresh():
    # The condition is strict `>` — exactly 10 days old must NOT trip
    # fail-closed, so the rule reads as "older than 10 days".
    now = datetime(2026, 5, 27, tzinfo=UTC)
    payload = _earnings_payload(now - timedelta(days=10))
    assert is_stale(payload, now=now) is False


def test_is_stale_works_for_econ_payload_too():
    now = datetime(2026, 5, 27, tzinfo=UTC)
    fresh = _econ_payload(now - timedelta(days=1))
    stale = _econ_payload(now - timedelta(days=14))
    assert is_stale(fresh, now=now) is False
    assert is_stale(stale, now=now) is True


# ---------------------------------------------------------------------------
# is_earnings_blackout — earnings day OR day after
# ---------------------------------------------------------------------------


def _payload_with(symbol: str, dates: list[date]) -> EarningsPayload:
    return EarningsPayload(
        refreshed_at=datetime(2026, 5, 27, tzinfo=UTC),
        source="test",
        earnings={symbol: dates},
    )


def test_earnings_blackout_on_report_day():
    p = _payload_with("NVDA", [date(2026, 8, 27)])
    assert is_earnings_blackout("NVDA", date(2026, 8, 27), p) is True


def test_earnings_blackout_day_after_report():
    p = _payload_with("NVDA", [date(2026, 8, 27)])
    assert is_earnings_blackout("NVDA", date(2026, 8, 28), p) is True


def test_earnings_blackout_two_days_after_is_clear():
    p = _payload_with("NVDA", [date(2026, 8, 27)])
    assert is_earnings_blackout("NVDA", date(2026, 8, 29), p) is False


def test_earnings_blackout_day_before_is_clear():
    # "Don't trade on earnings day or the day AFTER" — explicitly NOT the day
    # before. Pre-earnings positioning is allowed; post-earnings flush is not.
    p = _payload_with("NVDA", [date(2026, 8, 27)])
    assert is_earnings_blackout("NVDA", date(2026, 8, 26), p) is False


def test_earnings_blackout_symbol_not_in_payload_is_clear():
    # GLD is in the universe but never has earnings — must not error and
    # must not block.
    p = _payload_with("NVDA", [date(2026, 8, 27)])
    assert is_earnings_blackout("GLD", date(2026, 8, 27), p) is False


# ---------------------------------------------------------------------------
# is_econ_blackout — ±30 min window
# ---------------------------------------------------------------------------


def _econ_with(events: list[tuple[str, datetime]]) -> EconEventsPayload:
    return EconEventsPayload(
        refreshed_at=datetime(2026, 5, 27, tzinfo=UTC),
        source="test",
        events=events,
    )


def test_econ_blackout_at_event_time():
    event_ts = datetime(2026, 6, 10, 12, 30, tzinfo=UTC)  # 08:30 ET
    p = _econ_with([("CPI", event_ts)])
    in_blackout, name = is_econ_blackout(event_ts, p)
    assert in_blackout is True
    assert name == "CPI"


def test_econ_blackout_29_min_before():
    event_ts = datetime(2026, 6, 10, 12, 30, tzinfo=UTC)
    p = _econ_with([("CPI", event_ts)])
    in_blackout, name = is_econ_blackout(event_ts - timedelta(minutes=29), p)
    assert in_blackout is True
    assert name == "CPI"


def test_econ_blackout_31_min_before_is_clear():
    event_ts = datetime(2026, 6, 10, 12, 30, tzinfo=UTC)
    p = _econ_with([("CPI", event_ts)])
    in_blackout, name = is_econ_blackout(event_ts - timedelta(minutes=31), p)
    assert in_blackout is False
    assert name is None


def test_econ_blackout_29_min_after():
    event_ts = datetime(2026, 6, 10, 12, 30, tzinfo=UTC)
    p = _econ_with([("CPI", event_ts)])
    in_blackout, name = is_econ_blackout(event_ts + timedelta(minutes=29), p)
    assert in_blackout is True
    assert name == "CPI"


def test_econ_blackout_31_min_after_is_clear():
    event_ts = datetime(2026, 6, 10, 12, 30, tzinfo=UTC)
    p = _econ_with([("CPI", event_ts)])
    in_blackout, name = is_econ_blackout(event_ts + timedelta(minutes=31), p)
    assert in_blackout is False
    assert name is None


def test_econ_blackout_no_events_in_window():
    far_event = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)
    p = _econ_with([("FOMC", far_event)])
    in_blackout, name = is_econ_blackout(
        datetime(2026, 6, 10, 14, 0, tzinfo=UTC), p
    )
    assert in_blackout is False
    assert name is None


def test_econ_blackout_picks_first_matching_event():
    # If two events sit inside the same 30-min halo, we just need ANY hit.
    e1 = datetime(2026, 6, 10, 12, 30, tzinfo=UTC)
    e2 = datetime(2026, 6, 10, 12, 35, tzinfo=UTC)
    p = _econ_with([("CPI", e1), ("PCE", e2)])
    in_blackout, name = is_econ_blackout(e1, p)
    assert in_blackout is True
    assert name in {"CPI", "PCE"}
