"""Hardcoded calendar of tier-1 US economic release datetimes.

UPDATED: 2026-05-28 (covers through Mar 2027).
SOURCES:
- FOMC: federalreserve.gov/monetarypolicy/fomccalendars.htm (8 meetings/yr,
  decision at 14:00 ET on the second day of each two-day meeting).
- CPI:  bls.gov/schedule/news_release/cpi.htm (8:30 ET, ~10th-15th).
- PCE:  bea.gov/news/schedule (8:30 ET, last business day of month).
- NFP:  bls.gov/schedule/news_release/empsit.htm (8:30 ET, first Friday).
- GDP:  bea.gov/news/schedule (8:30 ET, end of month for the prior quarter).

The day-trade watcher treats each release as a ±30 min blackout window.
Refresh this file annually around mid-December when the next year's
schedules are fully published. The refresh workflow's sanity-check fires
a Telegram warning if the calendar runs short of upcoming events.
"""
from __future__ import annotations

# Each entry: {"name": str, "datetime_et": ISO8601-with-offset}
# Order matters only for human readability — code re-sorts after filter.
ECON_CALENDAR: list[dict] = [
    # ===== 2026 =====
    # FOMC (8 scheduled meetings; decision at 14:00 ET on day 2)
    {"name": "FOMC", "datetime_et": "2026-01-28T14:00:00-05:00"},
    {"name": "FOMC", "datetime_et": "2026-03-18T14:00:00-04:00"},
    {"name": "FOMC", "datetime_et": "2026-04-29T14:00:00-04:00"},
    {"name": "FOMC", "datetime_et": "2026-06-17T14:00:00-04:00"},
    {"name": "FOMC", "datetime_et": "2026-07-29T14:00:00-04:00"},
    {"name": "FOMC", "datetime_et": "2026-09-16T14:00:00-04:00"},
    {"name": "FOMC", "datetime_et": "2026-10-28T14:00:00-04:00"},
    {"name": "FOMC", "datetime_et": "2026-12-16T14:00:00-05:00"},

    # CPI (released ~mid-month at 8:30 ET)
    {"name": "CPI", "datetime_et": "2026-01-14T08:30:00-05:00"},
    {"name": "CPI", "datetime_et": "2026-02-11T08:30:00-05:00"},
    {"name": "CPI", "datetime_et": "2026-03-12T08:30:00-04:00"},
    {"name": "CPI", "datetime_et": "2026-04-09T08:30:00-04:00"},
    {"name": "CPI", "datetime_et": "2026-05-13T08:30:00-04:00"},
    {"name": "CPI", "datetime_et": "2026-06-10T08:30:00-04:00"},
    {"name": "CPI", "datetime_et": "2026-07-15T08:30:00-04:00"},
    {"name": "CPI", "datetime_et": "2026-08-13T08:30:00-04:00"},
    {"name": "CPI", "datetime_et": "2026-09-10T08:30:00-04:00"},
    {"name": "CPI", "datetime_et": "2026-10-14T08:30:00-04:00"},
    {"name": "CPI", "datetime_et": "2026-11-12T08:30:00-05:00"},
    {"name": "CPI", "datetime_et": "2026-12-10T08:30:00-05:00"},

    # PCE (released last business day of month at 8:30 ET)
    {"name": "PCE", "datetime_et": "2026-01-30T08:30:00-05:00"},
    {"name": "PCE", "datetime_et": "2026-02-27T08:30:00-05:00"},
    {"name": "PCE", "datetime_et": "2026-03-27T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2026-04-30T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2026-05-29T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2026-06-26T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2026-07-31T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2026-08-28T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2026-09-25T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2026-10-30T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2026-11-25T08:30:00-05:00"},
    {"name": "PCE", "datetime_et": "2026-12-23T08:30:00-05:00"},

    # NFP — Employment Situation (first Friday at 8:30 ET)
    {"name": "NFP", "datetime_et": "2026-01-02T08:30:00-05:00"},
    {"name": "NFP", "datetime_et": "2026-02-06T08:30:00-05:00"},
    {"name": "NFP", "datetime_et": "2026-03-06T08:30:00-05:00"},
    {"name": "NFP", "datetime_et": "2026-04-03T08:30:00-04:00"},
    {"name": "NFP", "datetime_et": "2026-05-01T08:30:00-04:00"},
    {"name": "NFP", "datetime_et": "2026-06-05T08:30:00-04:00"},
    {"name": "NFP", "datetime_et": "2026-07-02T08:30:00-04:00"},
    {"name": "NFP", "datetime_et": "2026-08-07T08:30:00-04:00"},
    {"name": "NFP", "datetime_et": "2026-09-04T08:30:00-04:00"},
    {"name": "NFP", "datetime_et": "2026-10-02T08:30:00-04:00"},
    {"name": "NFP", "datetime_et": "2026-11-06T08:30:00-05:00"},
    {"name": "NFP", "datetime_et": "2026-12-04T08:30:00-05:00"},

    # GDP (advance estimate ~end of first month after quarter close)
    {"name": "GDP", "datetime_et": "2026-01-29T08:30:00-05:00"},   # Q4 2025 advance
    {"name": "GDP", "datetime_et": "2026-04-29T08:30:00-04:00"},   # Q1 2026 advance
    {"name": "GDP", "datetime_et": "2026-07-30T08:30:00-04:00"},   # Q2 2026 advance
    {"name": "GDP", "datetime_et": "2026-10-29T08:30:00-04:00"},   # Q3 2026 advance

    # ===== 2027 Q1 (extends horizon past year boundary) =====
    {"name": "FOMC", "datetime_et": "2027-01-27T14:00:00-05:00"},
    {"name": "FOMC", "datetime_et": "2027-03-17T14:00:00-04:00"},
    {"name": "CPI", "datetime_et": "2027-01-13T08:30:00-05:00"},
    {"name": "CPI", "datetime_et": "2027-02-10T08:30:00-05:00"},
    {"name": "CPI", "datetime_et": "2027-03-10T08:30:00-04:00"},
    {"name": "PCE", "datetime_et": "2027-01-29T08:30:00-05:00"},
    {"name": "PCE", "datetime_et": "2027-02-26T08:30:00-05:00"},
    {"name": "PCE", "datetime_et": "2027-03-26T08:30:00-04:00"},
    {"name": "NFP", "datetime_et": "2027-01-08T08:30:00-05:00"},
    {"name": "NFP", "datetime_et": "2027-02-05T08:30:00-05:00"},
    {"name": "NFP", "datetime_et": "2027-03-05T08:30:00-05:00"},
    {"name": "GDP", "datetime_et": "2027-01-28T08:30:00-05:00"},   # Q4 2026 advance
]
