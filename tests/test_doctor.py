"""Tests for src/doctor.py — failure classification and cadence checks."""
from datetime import datetime, timedelta, timezone

from src.doctor import (
    Finding,
    build_report,
    check_dispatch_cadence,
    classify_failure,
)

UTC = timezone.utc


def _run(name: str, event: str, conclusion: str, created: datetime) -> dict:
    return {"name": name, "event": event, "conclusion": conclusion,
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "id": 1}


# A weekday timestamp inside the day-watcher enforcement window
# (2026-07-17 is a Friday), and one outside it (Saturday).
FRI_MARKET = datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# classify_failure
# ---------------------------------------------------------------------------

class TestClassify:
    def test_platform_outage(self):
        name, problem, fix = classify_failure(
            'Internal Server Error occurred while resolving '
            '"actions/checkout@v4".')
        assert name == "github_platform_outage"
        assert "githubstatus.com" in fix

    def test_service_unavailable_variant(self):
        name, _, _ = classify_failure(
            "Failed to resolve action download info. "
            "Error: Service Unavailable")
        assert name == "github_platform_outage"

    def test_billing_block(self):
        name, _, fix = classify_failure(
            "The job was not started because recent account payments "
            "have failed or your spending limit needs to be increased.")
        assert name == "billing_block"
        assert "Billing" in fix

    def test_python_exit(self):
        name, _, _ = classify_failure(
            "Process completed with exit code 1.")
        assert name == "python_error"

    def test_unknown_includes_excerpt(self):
        name, problem, _ = classify_failure("something totally new broke")
        assert name == "unknown"
        assert "something totally new broke" in problem

    def test_empty_annotations(self):
        name, problem, _ = classify_failure("")
        assert name == "unknown"
        assert "(no annotations)" in problem


# ---------------------------------------------------------------------------
# check_dispatch_cadence — the incident scenario: green schedule runs
# masking a dead repository_dispatch trigger
# ---------------------------------------------------------------------------

class TestCadence:
    def test_fresh_dispatches_are_healthy(self):
        runs = [
            _run("Swing Manager", "repository_dispatch", "success",
                 SATURDAY - timedelta(minutes=20)),
        ]
        assert check_dispatch_cadence(runs, SATURDAY) == []

    def test_green_schedule_runs_do_not_mask_dead_dispatch(self):
        # The 2026-07-12 incident shape: schedule runs all green,
        # dispatch silent for hours.
        runs = [
            _run("Swing Manager", "schedule", "success",
                 SATURDAY - timedelta(minutes=m))
            for m in (10, 40, 70, 100)
        ]
        findings = check_dispatch_cadence(runs, SATURDAY)
        assert len(findings) == 1
        assert findings[0].severity == "CRITICAL"
        assert "swing-manage-tick" in findings[0].fix

    def test_stale_dispatch_fires(self):
        runs = [
            _run("Swing Manager", "repository_dispatch", "success",
                 SATURDAY - timedelta(minutes=90)),
        ]
        findings = check_dispatch_cadence(runs, SATURDAY)
        assert len(findings) == 1
        assert "90 min ago" in findings[0].problem

    def test_failed_dispatch_does_not_count_as_alive(self):
        runs = [
            _run("Swing Manager", "repository_dispatch", "failure",
                 SATURDAY - timedelta(minutes=5)),
        ]
        assert len(check_dispatch_cadence(runs, SATURDAY)) == 1

    def test_day_watcher_checked_only_in_market_window(self):
        swing_ok = _run("Swing Manager", "repository_dispatch", "success",
                        FRI_MARKET - timedelta(minutes=10))
        # No day-watcher dispatches at all.
        assert check_dispatch_cadence([swing_ok], FRI_MARKET) != []
        # Same silence on a Saturday -> only checked Mon-Fri.
        swing_ok_sat = _run("Swing Manager", "repository_dispatch",
                            "success", SATURDAY - timedelta(minutes=10))
        assert check_dispatch_cadence([swing_ok_sat], SATURDAY) == []

    def test_day_watcher_fresh_tick_in_window_is_healthy(self):
        runs = [
            _run("Swing Manager", "repository_dispatch", "success",
                 FRI_MARKET - timedelta(minutes=10)),
            _run("Day Watcher", "repository_dispatch", "success",
                 FRI_MARKET - timedelta(minutes=5)),
        ]
        assert check_dispatch_cadence(runs, FRI_MARKET) == []


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

class TestReport:
    def test_healthy_returns_none(self):
        assert build_report([]) is None

    def test_criticals_sort_first_and_fix_included(self):
        report = build_report([
            Finding("WARN", "minor thing", "minor fix"),
            Finding("CRITICAL", "big thing", "big fix"),
        ])
        assert report is not None
        assert report.index("big thing") < report.index("minor thing")
        assert "FIX: big fix" in report
        assert "2 issue(s)" in report

    def test_long_report_truncated(self):
        findings = [Finding("WARN", "x" * 500, "y" * 500)
                    for _ in range(10)]
        report = build_report(findings)
        assert len(report) < 4096  # Telegram hard limit
        assert "truncated" in report
