"""Weekly refresh of the day-trade calendar state files.

OWNED BY: `.github/workflows/refresh-day-calendars.yml` (runs Sunday 06:00 UTC).
DO NOT INVOKE BY HAND — let the workflow own these JSON files so the
git diff stays clean.

Writes:
- state/earnings.json   — next ~90 days of earnings for the day-trade universe
- state/econ_events.json — next ~35 days of tier-1 US economic releases

Currently a stub. The Finnhub free-tier integration lands in a later
phase. Run with the env var `REFRESH_DAY_CALENDARS_FORCE=stub-only` to
exercise the file-write path during workflow setup without making any
external calls.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EARNINGS_PATH = ROOT / "state" / "earnings.json"
ECON_EVENTS_PATH = ROOT / "state" / "econ_events.json"

UNIVERSE_STOCKS = ["NVDA", "TSLA", "AAPL", "AMZN", "GOOGL", "MSFT"]


def fetch_earnings() -> dict[str, list[str]]:
    """Fetch next 90 days of earnings for each stock from Finnhub free tier.

    Returns a dict mapping symbol → sorted list of YYYY-MM-DD strings.
    """
    raise NotImplementedError(
        "Finnhub integration lands in a later phase. "
        "Set FINNHUB_API_KEY and implement the GET /calendar/earnings call."
    )


def fetch_econ_events() -> list[dict]:
    """Fetch next 35 days of FOMC, CPI, PCE, NFP, and GDP release schedules.

    Returns a list of {'name': str, 'datetime_et': ISO8601} dicts.
    """
    raise NotImplementedError(
        "Econ-event source not chosen yet. Candidate: BLS API + Fed schedule "
        "page + BEA release calendar, merged into a single feed."
    )


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
        # Touch the files with a fresh refreshed_at so D0 wiring can be
        # exercised end-to-end without hitting any external API. Pre-existing
        # content (manual seed) is preserved.
        for path in (EARNINGS_PATH, ECON_EVENTS_PATH):
            data = _read_existing(path)
            data["refreshed_at"] = now_iso
            data["source"] = "stub-only"
            write_payload(path, data)
        return 0

    try:
        earnings = fetch_earnings()
        econ_events = fetch_econ_events()
    except NotImplementedError as exc:
        print(f"[refresh] FAILED: {exc}", file=sys.stderr)
        return 1

    write_payload(EARNINGS_PATH, {
        "_doc": _read_existing(EARNINGS_PATH).get("_doc", ""),
        "refreshed_at": now_iso,
        "source": "finnhub_free",
        "earnings": earnings,
    })
    write_payload(ECON_EVENTS_PATH, {
        "_doc": _read_existing(ECON_EVENTS_PATH).get("_doc", ""),
        "refreshed_at": now_iso,
        "source": "bls_fed_bea",
        "events": econ_events,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
