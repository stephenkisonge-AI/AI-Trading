"""Tests for scripts/refresh_day_calendars.py — Finnhub earnings parse,
hardcoded econ calendar filtering, and the soft-degrade paths.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


# scripts/ isn't on the package path by default — give the test runner a hint.
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import refresh_day_calendars as refresh
from scripts.econ_calendar_data import ECON_CALENDAR


# ---------------------------------------------------------------------------
# Hardcoded econ calendar
# ---------------------------------------------------------------------------


def test_econ_calendar_entries_are_well_formed():
    """Every entry must have a recognized release name and a parseable
    ISO8601 datetime_et with timezone offset (DST-aware)."""
    valid_names = {"FOMC", "CPI", "PCE", "NFP", "GDP"}
    for entry in ECON_CALENDAR:
        assert entry["name"] in valid_names, f"unknown release: {entry}"
        ts = datetime.fromisoformat(entry["datetime_et"])
        assert ts.tzinfo is not None, f"timezone missing: {entry}"


def test_econ_calendar_covers_at_least_12_months():
    """Calendar must extend at least 12 months from today, else the
    annual refresh is overdue."""
    today = datetime.now(timezone.utc).date()
    last = max(
        datetime.fromisoformat(e["datetime_et"]).date()
        for e in ECON_CALENDAR
    )
    assert (last - today).days >= 180, (
        f"econ calendar only extends to {last} — refresh overdue"
    )


def test_econ_calendar_has_8_fomc_meetings_per_year():
    """FOMC schedules 8 meetings per year. Sanity-check 2026 coverage."""
    fomc_2026 = [
        e for e in ECON_CALENDAR
        if e["name"] == "FOMC"
        and datetime.fromisoformat(e["datetime_et"]).year == 2026
    ]
    assert len(fomc_2026) == 8


def test_fetch_econ_events_filters_to_35_day_horizon():
    """fetch_econ_events should return only events in the next 35 days."""
    events = refresh.fetch_econ_events()
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=35)
    for e in events:
        d = datetime.fromisoformat(e["datetime_et"]).date()
        assert today <= d <= horizon, f"out-of-window event: {e}"


def test_fetch_econ_events_sorted_chronologically():
    events = refresh.fetch_econ_events()
    for i in range(1, len(events)):
        prev = datetime.fromisoformat(events[i - 1]["datetime_et"])
        curr = datetime.fromisoformat(events[i]["datetime_et"])
        assert prev <= curr


# ---------------------------------------------------------------------------
# Finnhub earnings — patched HTTP
# ---------------------------------------------------------------------------


def _finnhub_response(dates_by_symbol: dict[str, list[str]]):
    """Build a fake Finnhub /calendar/earnings payload for one symbol."""
    def _impl(url: str) -> dict:
        # Parse symbol out of the URL.
        for sym, dates in dates_by_symbol.items():
            if f"symbol={sym}" in url:
                return {"earningsCalendar": [{"date": d, "symbol": sym} for d in dates]}
        return {"earningsCalendar": []}
    return _impl


def test_fetch_earnings_parses_finnhub_response():
    fake = _finnhub_response({
        "NVDA": ["2026-08-27"],
        "AAPL": ["2026-08-06"],
    })
    with patch.object(refresh, "_http_get_json", side_effect=fake):
        out = refresh.fetch_earnings(api_key="fake")
    assert out["NVDA"] == ["2026-08-27"]
    assert out["AAPL"] == ["2026-08-06"]
    # Symbols with no upcoming earnings still appear with empty list.
    assert out["TSLA"] == []


def test_fetch_earnings_dedupes_and_sorts():
    fake = _finnhub_response({
        "NVDA": ["2026-11-19", "2026-08-27", "2026-08-27"],  # dup
    })
    with patch.object(refresh, "_http_get_json", side_effect=fake):
        out = refresh.fetch_earnings(api_key="fake")
    assert out["NVDA"] == ["2026-08-27", "2026-11-19"]


def test_fetch_earnings_swallows_per_symbol_errors():
    def _impl(url: str) -> dict:
        if "NVDA" in url:
            raise TimeoutError("finnhub timeout")
        return {"earningsCalendar": [{"date": "2026-08-06", "symbol": "AAPL"}]}
    with patch.object(refresh, "_http_get_json", side_effect=_impl):
        out = refresh.fetch_earnings(api_key="fake")
    # NVDA fetch failed → empty list, no crash.
    assert out["NVDA"] == []
    # AAPL still got through.
    assert out["AAPL"] == ["2026-08-06"]


# ---------------------------------------------------------------------------
# main() soft-degrade paths
# ---------------------------------------------------------------------------


def test_main_stub_only_preserves_content_bumps_timestamp(tmp_path, monkeypatch):
    earnings = tmp_path / "earnings.json"
    econ = tmp_path / "econ.json"
    earnings.write_text(json.dumps({
        "_doc": "doc", "refreshed_at": "2020-01-01T00:00:00Z",
        "source": "manual_seed", "earnings": {"NVDA": ["2026-08-27"]},
    }))
    econ.write_text(json.dumps({
        "_doc": "doc", "refreshed_at": "2020-01-01T00:00:00Z",
        "source": "manual_seed", "events": [],
    }))
    monkeypatch.setattr(refresh, "EARNINGS_PATH", earnings)
    monkeypatch.setattr(refresh, "ECON_EVENTS_PATH", econ)
    monkeypatch.setenv("REFRESH_DAY_CALENDARS_FORCE", "stub-only")

    rc = refresh.main()
    assert rc == 0

    after = json.loads(earnings.read_text())
    assert after["source"] == "stub-only"
    assert after["earnings"] == {"NVDA": ["2026-08-27"]}  # preserved
    assert after["refreshed_at"] != "2020-01-01T00:00:00Z"  # bumped


def test_main_missing_finnhub_key_soft_degrades(tmp_path, monkeypatch):
    """Without FINNHUB_API_KEY the script must still produce a refreshed
    earnings.json (with old content preserved) so the watcher's 10-day
    stale-data gate doesn't trip just because the key isn't set yet.
    """
    earnings = tmp_path / "earnings.json"
    econ = tmp_path / "econ.json"
    earnings.write_text(json.dumps({
        "_doc": "doc", "refreshed_at": "2020-01-01T00:00:00Z",
        "source": "manual_seed", "earnings": {"NVDA": ["2026-08-27"]},
    }))
    econ.write_text(json.dumps({
        "_doc": "doc", "refreshed_at": "2020-01-01T00:00:00Z",
        "source": "manual_seed", "events": [],
    }))
    monkeypatch.setattr(refresh, "EARNINGS_PATH", earnings)
    monkeypatch.setattr(refresh, "ECON_EVENTS_PATH", econ)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("REFRESH_DAY_CALENDARS_FORCE", raising=False)

    rc = refresh.main()
    assert rc == 0
    after = json.loads(earnings.read_text())
    assert after["source"] == "no_finnhub_key_keeping_existing"
    assert after["earnings"] == {"NVDA": ["2026-08-27"]}  # preserved


def test_main_all_empty_earnings_fetch_fails_hard(tmp_path, monkeypatch):
    """If every symbol returns empty (Finnhub outage, bad key) we must
    NOT overwrite the seed — return non-zero so the workflow's failure
    path fires the Telegram alert.
    """
    earnings = tmp_path / "earnings.json"
    econ = tmp_path / "econ.json"
    earnings.write_text(json.dumps({
        "_doc": "doc", "refreshed_at": "2020-01-01T00:00:00Z",
        "source": "manual_seed", "earnings": {"NVDA": ["2026-08-27"]},
    }))
    econ.write_text(json.dumps({
        "_doc": "doc", "refreshed_at": "2020-01-01T00:00:00Z",
        "source": "manual_seed", "events": [],
    }))
    monkeypatch.setattr(refresh, "EARNINGS_PATH", earnings)
    monkeypatch.setattr(refresh, "ECON_EVENTS_PATH", econ)
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    monkeypatch.delenv("REFRESH_DAY_CALENDARS_FORCE", raising=False)

    def _empty(url: str) -> dict:
        return {"earningsCalendar": []}
    with patch.object(refresh, "_http_get_json", side_effect=_empty):
        rc = refresh.main()
    assert rc == 1
    # Seed preserved.
    after = json.loads(earnings.read_text())
    assert after["source"] == "manual_seed"


def test_main_happy_path_writes_finnhub_payload(tmp_path, monkeypatch):
    earnings = tmp_path / "earnings.json"
    econ = tmp_path / "econ.json"
    earnings.write_text(json.dumps({
        "_doc": "doc", "refreshed_at": "2020-01-01T00:00:00Z",
        "source": "manual_seed", "earnings": {"NVDA": []},
    }))
    econ.write_text(json.dumps({
        "_doc": "doc", "refreshed_at": "2020-01-01T00:00:00Z",
        "source": "manual_seed", "events": [],
    }))
    monkeypatch.setattr(refresh, "EARNINGS_PATH", earnings)
    monkeypatch.setattr(refresh, "ECON_EVENTS_PATH", econ)
    monkeypatch.setenv("FINNHUB_API_KEY", "fake")
    monkeypatch.delenv("REFRESH_DAY_CALENDARS_FORCE", raising=False)

    fake = _finnhub_response({
        "NVDA": ["2026-08-27"],
        "AAPL": ["2026-08-06"],
    })
    with patch.object(refresh, "_http_get_json", side_effect=fake):
        rc = refresh.main()
    assert rc == 0
    after = json.loads(earnings.read_text())
    assert after["source"] == "finnhub_free"
    assert after["earnings"]["NVDA"] == ["2026-08-27"]
    # Econ events file refreshed too.
    after_econ = json.loads(econ.read_text())
    assert after_econ["source"] == "hardcoded_calendar"
