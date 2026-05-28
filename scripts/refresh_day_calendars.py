"""Weekly refresh of the day-trade calendar state files.

OWNED BY: `.github/workflows/refresh-day-calendars.yml` (runs Sunday 06:00 UTC).
DO NOT INVOKE BY HAND — let the workflow own these JSON files so the
git diff stays clean.

Writes:
- state/earnings.json   — next 90 days of earnings for the day-trade universe
- state/econ_events.json — next 35 days of tier-1 US economic releases

Data sources:
- earnings:    Finnhub free tier (60 req/min, requires FINNHUB_API_KEY)
- econ events: hardcoded calendar in scripts/econ_calendar_data.py
               (Free APIs that cover FOMC/CPI/PCE/NFP/GDP either don't
                exist or are paid-only. The events themselves publish 6-12
                months ahead, so a checked-in schedule is more reliable
                than any scraper. Update annually.)

Environment:
- FINNHUB_API_KEY: free-tier key from https://finnhub.io/. When unset,
  earnings refresh is skipped but the timestamp still bumps so the
  watcher's 10-day staleness gate doesn't trip.
- REFRESH_DAY_CALENDARS_FORCE=stub-only: skip both fetches, just bump
  timestamps. Used for workflow smoke tests.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
EARNINGS_PATH = ROOT / "state" / "earnings.json"
ECON_EVENTS_PATH = ROOT / "state" / "econ_events.json"

UNIVERSE_STOCKS = ["NVDA", "TSLA", "AAPL", "AMZN", "GOOGL", "MSFT"]

_FINNHUB_BASE = "https://finnhub.io/api/v1"
_FINNHUB_TIMEOUT_SEC = 15
_EARNINGS_HORIZON_DAYS = 90
_ECON_HORIZON_DAYS = 35


# =========================================================================
# Earnings — Finnhub free tier
# =========================================================================


def _http_get_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": "ai-trading-refresh/1.0"})
    with urlopen(req, timeout=_FINNHUB_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_earnings_for_symbol(symbol: str, start: date, end: date,
                                api_key: str) -> list[str]:
    """Return a sorted, deduplicated list of YYYY-MM-DD earnings dates
    for `symbol` between `start` and `end` (inclusive).
    """
    url = (
        f"{_FINNHUB_BASE}/calendar/earnings"
        f"?from={start.isoformat()}&to={end.isoformat()}"
        f"&symbol={symbol}&token={api_key}"
    )
    try:
        payload = _http_get_json(url)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"[refresh] earnings fetch failed for {symbol}: {exc}",
              file=sys.stderr)
        return []

    raw = payload.get("earningsCalendar") or []
    dates: set[str] = set()
    for entry in raw:
        d = entry.get("date")
        if d:
            dates.add(d)
    return sorted(dates)


def fetch_earnings(api_key: str) -> dict[str, list[str]]:
    """Fetch next _EARNINGS_HORIZON_DAYS of earnings for each universe stock.

    Returns a dict mapping symbol → sorted list of YYYY-MM-DD strings.
    Symbols that fail to fetch are simply omitted — caller decides
    whether that's a hard failure (see main()).
    """
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=_EARNINGS_HORIZON_DAYS)
    out: dict[str, list[str]] = {}
    for symbol in UNIVERSE_STOCKS:
        dates = _fetch_earnings_for_symbol(symbol, today, end, api_key)
        out[symbol] = dates
    return out


# =========================================================================
# Econ events — hardcoded calendar
# =========================================================================


def fetch_econ_events() -> list[dict]:
    """Return next _ECON_HORIZON_DAYS of tier-1 US economic releases from
    the hardcoded calendar.

    The calendar lives in scripts/econ_calendar_data.py and contains
    FOMC, CPI, PCE, NFP, and GDP release dates for the next 12+ months.
    Update annually — these events publish their schedules well in
    advance.
    """
    # Add scripts/ to sys.path so this import works both when run as
    # `python scripts/refresh_day_calendars.py` (GH Actions invocation,
    # scripts/ is not a package) AND when imported via `from scripts
    # import refresh_day_calendars` (test invocation).
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from econ_calendar_data import ECON_CALENDAR
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=_ECON_HORIZON_DAYS)
    out: list[dict] = []
    for event in ECON_CALENDAR:
        # event['datetime_et'] is an ISO8601 string with tz offset.
        ts = datetime.fromisoformat(event["datetime_et"])
        d = ts.date()
        if today <= d <= horizon:
            out.append({"name": event["name"], "datetime_et": event["datetime_et"]})
    out.sort(key=lambda e: e["datetime_et"])
    return out


# =========================================================================
# Wiring
# =========================================================================


def write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[refresh] wrote {path}")


def _read_existing(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if os.environ.get("REFRESH_DAY_CALENDARS_FORCE") == "stub-only":
        # Touch the files with a fresh refreshed_at so the workflow can
        # be smoke-tested without hitting Finnhub. Existing content
        # (manual seed) is preserved.
        for path in (EARNINGS_PATH, ECON_EVENTS_PATH):
            data = _read_existing(path)
            data["refreshed_at"] = now_iso
            data["source"] = "stub-only"
            write_payload(path, data)
        return 0

    # --- Econ events (hardcoded, can't fail) ---
    econ_events = fetch_econ_events()
    write_payload(ECON_EVENTS_PATH, {
        "_doc": _read_existing(ECON_EVENTS_PATH).get("_doc", ""),
        "refreshed_at": now_iso,
        "source": "hardcoded_calendar",
        "events": econ_events,
    })

    # --- Earnings (Finnhub, soft-degrade when key missing) ---
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not api_key:
        # Bump timestamp on existing earnings so the 10-day stale-data
        # gate doesn't trip, but flag the missing key clearly.
        existing = _read_existing(EARNINGS_PATH)
        existing["refreshed_at"] = now_iso
        existing["source"] = "no_finnhub_key_keeping_existing"
        write_payload(EARNINGS_PATH, existing)
        print("[refresh] WARNING: FINNHUB_API_KEY unset — kept existing "
              "earnings file, bumped timestamp only.", file=sys.stderr)
        return 0

    earnings = fetch_earnings(api_key)
    # Sanity check — if every symbol returned empty (Finnhub outage,
    # bad key, etc.), DON'T overwrite the seed. Telegram alert will
    # fire via the workflow's failure path if we exit non-zero.
    if all(len(dates) == 0 for dates in earnings.values()):
        print("[refresh] ERROR: every earnings fetch returned empty — "
              "preserving existing file.", file=sys.stderr)
        return 1

    write_payload(EARNINGS_PATH, {
        "_doc": _read_existing(EARNINGS_PATH).get("_doc", ""),
        "refreshed_at": now_iso,
        "source": "finnhub_free",
        "earnings": earnings,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
