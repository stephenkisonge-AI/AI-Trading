"""Read-only loader for the day-trade strategy's external calendar files.

`state/earnings.json` and `state/econ_events.json` are owned by the
`refresh-day-calendars.yml` GitHub workflow. The watcher only reads them.

Loader contract:
- Returns parsed payloads as plain dicts/lists. Pure I/O — no decision logic.
- All time comparisons must use the helpers below, which apply the
  no-trade windows defined in `Day_Trading_Strategy.md`.
- `is_stale(...)` returns True when the file is older than the doc's
  10-day fail-closed cutoff. The watcher must refuse new entries when
  either file is stale.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
EARNINGS_PATH = _ROOT / "state" / "earnings.json"
ECON_EVENTS_PATH = _ROOT / "state" / "econ_events.json"

# Per Day_Trading_Strategy.md:
# - State files older than this trigger fail-closed.
STALENESS_DAYS = 10
# - Watcher refuses entries within ±this many minutes of a tier-1 release.
ECON_BLACKOUT_MINUTES = 30


@dataclass(frozen=True)
class EarningsPayload:
    refreshed_at: datetime
    source: str
    earnings: dict[str, list[date]]


@dataclass(frozen=True)
class EconEventsPayload:
    refreshed_at: datetime
    source: str
    events: list[tuple[str, datetime]]  # (name, datetime with tz)


def _parse_iso_utc(s: str) -> datetime:
    """Parse an ISO-8601 timestamp into a UTC-aware datetime."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_earnings(path: Path = EARNINGS_PATH) -> EarningsPayload:
    """Load earnings.json. Raises FileNotFoundError or json.JSONDecodeError
    on broken input — caller decides whether that's fail-closed.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    refreshed_at = _parse_iso_utc(raw["refreshed_at"])
    earnings: dict[str, list[date]] = {}
    for symbol, dates in (raw.get("earnings") or {}).items():
        earnings[symbol] = [date.fromisoformat(d) for d in dates]
    return EarningsPayload(
        refreshed_at=refreshed_at,
        source=str(raw.get("source", "unknown")),
        earnings=earnings,
    )


def load_econ_events(path: Path = ECON_EVENTS_PATH) -> EconEventsPayload:
    raw = json.loads(path.read_text(encoding="utf-8"))
    refreshed_at = _parse_iso_utc(raw["refreshed_at"])
    events: list[tuple[str, datetime]] = []
    for entry in raw.get("events") or []:
        name = str(entry["name"])
        ts = _parse_iso_utc(entry["datetime_et"])
        events.append((name, ts))
    return EconEventsPayload(
        refreshed_at=refreshed_at,
        source=str(raw.get("source", "unknown")),
        events=events,
    )


def is_stale(payload: EarningsPayload | EconEventsPayload,
             now: datetime | None = None,
             max_age_days: int = STALENESS_DAYS) -> bool:
    """True when `payload.refreshed_at` is older than `max_age_days` from now.

    The watcher must check both payloads and refuse new entries if either
    returns True. See Day_Trading_Strategy.md §"Risk caps" / no-trade
    conditions.
    """
    now = now or datetime.now(timezone.utc)
    return (now - payload.refreshed_at) > timedelta(days=max_age_days)


def is_earnings_blackout(symbol: str, day: date,
                         payload: EarningsPayload) -> bool:
    """True if `symbol` reports earnings on `day` OR `day - 1` (the
    "earnings day or day after" rule).

    The watcher treats this as a per-ticker disqualifier — other tickers
    in the universe remain eligible.
    """
    dates = payload.earnings.get(symbol)
    if not dates:
        return False
    yesterday = day - timedelta(days=1)
    return any(d == day or d == yesterday for d in dates)


def is_econ_blackout(now: datetime,
                     payload: EconEventsPayload,
                     window_minutes: int = ECON_BLACKOUT_MINUTES) -> tuple[bool, str | None]:
    """True if `now` is within ±`window_minutes` of any tier-1 release.

    Returns (in_blackout, event_name) so the watcher can log which event
    caused the skip. The window is symmetric — releases create a no-trade
    halo that opens 30 min before scheduled time and closes 30 min after.
    """
    now = now.astimezone(timezone.utc)
    window = timedelta(minutes=window_minutes)
    for name, event_ts in payload.events:
        if abs(now - event_ts) <= window:
            return True, name
    return False, None
