"""Tests for src/doctor.py — failure classification and cadence checks."""
from datetime import datetime, timedelta, timezone

import src.doctor as doctor
from src.doctor import (
    Finding,
    build_report,
    check_dispatch_cadence,
    check_failed_runs,
    check_stuck_runs,
    classify_failure,
    healthy_message,
)

UTC = timezone.utc


def _run(name: str, event: str, conclusion: str, created: datetime) -> dict:
    return {"name": name, "event": event, "conclusion": conclusion,
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "id": 1}


def _watcher_ok(now: datetime, minutes_ago: int = 60) -> dict:
    """A healthy crypto-watcher dispatch tick, so tests about the other
    workflows are not tripped by the watcher cadence check."""
    return _run("Watcher", "repository_dispatch", "success",
                now - timedelta(minutes=minutes_ago))


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

    def test_job_timeout(self):
        name, _, _ = classify_failure(
            "The job has exceeded the maximum execution time of 4m0s\n"
            "The operation was canceled.")
        assert name == "job_timeout"

    def test_runner_unavailable(self):
        name, problem, fix = classify_failure(
            "The job was not acquired by Runner of type hosted even "
            "after multiple attempts")
        assert name == "runner_unavailable"
        assert "queue" in problem
        assert "githubstatus.com" in fix

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
            _watcher_ok(SATURDAY),
        ]
        assert check_dispatch_cadence(runs, SATURDAY) == []

    def test_green_schedule_runs_do_not_mask_dead_dispatch(self):
        # The 2026-07-12 incident shape: schedule runs all green,
        # dispatch silent for hours.
        runs = [
            _run("Swing Manager", "schedule", "success",
                 SATURDAY - timedelta(minutes=m))
            for m in (10, 40, 70, 100)
        ] + [_watcher_ok(SATURDAY)]
        findings = check_dispatch_cadence(runs, SATURDAY)
        assert len(findings) == 1
        assert findings[0].severity == "CRITICAL"
        assert "swing-manage-tick" in findings[0].fix

    def test_stale_dispatch_fires(self):
        runs = [
            _run("Swing Manager", "repository_dispatch", "success",
                 SATURDAY - timedelta(minutes=90)),
            _watcher_ok(SATURDAY),
        ]
        findings = check_dispatch_cadence(runs, SATURDAY)
        assert len(findings) == 1
        assert "90 min ago" in findings[0].problem

    def test_failed_dispatch_does_not_count_as_alive(self):
        runs = [
            _run("Swing Manager", "repository_dispatch", "failure",
                 SATURDAY - timedelta(minutes=5)),
            _watcher_ok(SATURDAY),
        ]
        assert len(check_dispatch_cadence(runs, SATURDAY)) == 1

    def test_day_watcher_checked_only_in_market_window(self):
        swing_ok = _run("Swing Manager", "repository_dispatch", "success",
                        FRI_MARKET - timedelta(minutes=10))
        # No day-watcher dispatches at all.
        findings = check_dispatch_cadence(
            [swing_ok, _watcher_ok(FRI_MARKET)], FRI_MARKET)
        assert len(findings) == 1
        assert "day-watcher-tick" in findings[0].fix
        # Same silence on a Saturday -> only checked Mon-Fri.
        swing_ok_sat = _run("Swing Manager", "repository_dispatch",
                            "success", SATURDAY - timedelta(minutes=10))
        assert check_dispatch_cadence(
            [swing_ok_sat, _watcher_ok(SATURDAY)], SATURDAY) == []

    def test_day_watcher_fresh_tick_in_window_is_healthy(self):
        runs = [
            _run("Swing Manager", "repository_dispatch", "success",
                 FRI_MARKET - timedelta(minutes=10)),
            _run("Day Watcher", "repository_dispatch", "success",
                 FRI_MARKET - timedelta(minutes=5)),
            _watcher_ok(FRI_MARKET),
        ]
        assert check_dispatch_cadence(runs, FRI_MARKET) == []

    def test_crypto_watcher_silent_dispatch_warns(self):
        # The Nov 2026 shape: watcher scans all green on the native
        # schedule, no crypto-watcher-tick dispatch at all.
        swing_ok = _run("Swing Manager", "repository_dispatch", "success",
                        SATURDAY - timedelta(minutes=10))
        runs = [swing_ok] + [
            _run("Watcher", "schedule", "success",
                 SATURDAY - timedelta(minutes=m)) for m in (30, 300)
        ]
        findings = check_dispatch_cadence(runs, SATURDAY)
        assert len(findings) == 1
        assert findings[0].severity == "WARN"
        assert "crypto-watcher-tick" in findings[0].fix
        assert "never in the lookback window" in findings[0].problem

    def test_crypto_watcher_tick_within_five_hours_is_healthy(self):
        swing_ok = _run("Swing Manager", "repository_dispatch", "success",
                        SATURDAY - timedelta(minutes=10))
        assert check_dispatch_cadence(
            [swing_ok, _watcher_ok(SATURDAY, minutes_ago=250)], SATURDAY) == []
        late = check_dispatch_cadence(
            [swing_ok, _watcher_ok(SATURDAY, minutes_ago=310)], SATURDAY)
        assert len(late) == 1 and "310 min ago" in late[0].problem

    def test_watcher_name_does_not_match_day_watcher(self):
        # "Day Watcher" dispatches must not satisfy the "Watcher" check.
        runs = [
            _run("Swing Manager", "repository_dispatch", "success",
                 SATURDAY - timedelta(minutes=10)),
            _run("Day Watcher", "repository_dispatch", "success",
                 SATURDAY - timedelta(minutes=5)),
        ]
        findings = check_dispatch_cadence(runs, SATURDAY)
        assert len(findings) == 1
        assert "crypto-watcher-tick" in findings[0].fix


# ---------------------------------------------------------------------------
# check_failed_runs — cancelled runs: timeouts, runner starvation, and
# runs replaced in the concurrency queue before they started
# ---------------------------------------------------------------------------

def _run_id(name: str, conclusion: str, run_id: int) -> dict:
    run = _run(name, "repository_dispatch", conclusion, SATURDAY)
    run["id"] = run_id
    return run


class TestFailedAndCancelledRuns:
    def _patch(self, monkeypatch, jobs_by_run: dict, text_by_job: dict):
        """jobs_by_run: run id -> list of job dicts. text_by_job: job id
        -> annotation text."""
        monkeypatch.setattr(
            doctor, "fetch_jobs",
            lambda token, repo, run_id: jobs_by_run.get(run_id, []))
        monkeypatch.setattr(
            doctor, "fetch_annotation_text",
            lambda token, repo, jobs: "\n".join(
                text_by_job.get(j["id"], "") for j in jobs))

    def test_successful_runs_are_not_inspected(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("no API call expected for a green run")
        monkeypatch.setattr(doctor, "fetch_jobs", boom)
        assert check_failed_runs(
            "t", "r", [_run_id("Day Watcher", "success", 1)]) == []

    def test_timeout_is_reported_although_the_run_says_cancelled(
            self, monkeypatch):
        # GitHub ends the RUN of a timed-out job as "cancelled" (seen on
        # Day Watcher 2026-10-30), which the doctor used to ignore.
        self._patch(
            monkeypatch,
            {7: [{"id": 70, "conclusion": "cancelled"}]},
            {70: "The job has exceeded the maximum execution time of 4m0s"})
        findings = check_failed_runs(
            "t", "r", [_run_id("Day Watcher", "cancelled", 7)])
        assert len(findings) == 1
        assert "timeout-minutes" in findings[0].problem
        assert "Day Watcher#7" in findings[0].problem

    def test_runner_starvation_is_classified(self, monkeypatch):
        # Run conclusion "failure", job conclusion "cancelled".
        self._patch(
            monkeypatch,
            {8: [{"id": 80, "conclusion": "cancelled"}]},
            {80: "The job was not acquired by Runner of type hosted "
                 "even after multiple attempts"})
        findings = check_failed_runs(
            "t", "r", [_run_id("Day Watcher", "failure", 8)])
        assert len(findings) == 1
        assert "could not supply a runner" in findings[0].problem

    def test_runs_replaced_in_the_queue_are_grouped_by_workflow(
            self, monkeypatch):
        # Cancelled with no jobs at all = replaced while waiting.
        self._patch(monkeypatch, {}, {})
        runs = ([_run_id("Day Watcher", "cancelled", i) for i in (1, 2, 3)]
                + [_run_id("Watcher", "cancelled", 4)])
        findings = check_failed_runs("t", "r", runs)
        assert len(findings) == 1
        f = findings[0]
        assert f.problem.startswith("4 run(s) were cancelled before they "
                                    "started")
        assert "Day Watcher ×3, Watcher ×1" in f.problem
        assert "gh workflow run watcher.yml" in f.fix

    def test_cancel_without_a_known_reason_is_not_called_unclassified(
            self, monkeypatch):
        self._patch(
            monkeypatch,
            {9: [{"id": 90, "conclusion": "cancelled"}]},
            {90: "The operation was canceled."})
        findings = check_failed_runs(
            "t", "r", [_run_id("Doctor", "cancelled", 9)])
        assert len(findings) == 1
        assert "manual cancel" in findings[0].problem
        assert "ignore" in findings[0].fix

    def test_failure_and_replacement_yield_separate_findings(
            self, monkeypatch):
        self._patch(
            monkeypatch,
            {5: [{"id": 50, "conclusion": "failure"}]},
            {50: "Process completed with exit code 1."})
        findings = check_failed_runs("t", "r", [
            _run_id("Swing Manager", "failure", 5),
            _run_id("Swing Manager", "cancelled", 6),
        ])
        assert len(findings) == 2
        assert "exited non-zero" in findings[0].problem
        assert "Swing Manager ×1" in findings[1].problem


class TestAnnotationFetch:
    def test_annotations_of_cancelled_jobs_are_read(self, monkeypatch):
        asked = []

        class _Resp:
            ok = True

            def json(self):
                return [{"message": "The job has exceeded the maximum "
                                    "execution time of 4m0s"}]

        def fake_get(url, headers=None, timeout=None):
            asked.append(url)
            return _Resp()

        monkeypatch.setattr(doctor.requests, "get", fake_get)
        text = doctor.fetch_annotation_text("t", "o/r", [
            {"id": 1, "conclusion": "success"},
            {"id": 2, "conclusion": "cancelled"},
        ])
        assert "maximum execution time" in text
        assert len(asked) == 1
        assert asked[0].endswith("/check-runs/2/annotations")


# ---------------------------------------------------------------------------
# check_stuck_runs — runs GitHub accepted but never started
# ---------------------------------------------------------------------------

def _pending(name: str, status: str, minutes_ago: int, run_id: int) -> dict:
    created = SATURDAY - timedelta(minutes=minutes_ago)
    return {"name": name, "event": "repository_dispatch", "status": status,
            "conclusion": None, "id": run_id,
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")}


class TestStuckRuns:
    def test_recently_queued_run_is_not_stuck(self):
        runs = [_pending("Day Watcher", "queued", 5, 1),
                _pending("Swing Manager", "pending", 29, 2)]
        assert check_stuck_runs(runs, SATURDAY) == []

    def test_in_progress_and_completed_runs_are_ignored(self):
        done = _run("Day Watcher", "repository_dispatch", "success",
                    SATURDAY - timedelta(minutes=90))
        done["status"] = "completed"
        runs = [_pending("Archive logs", "in_progress", 40, 1), done]
        assert check_stuck_runs(runs, SATURDAY) == []

    def test_long_queued_runs_are_reported_with_ids(self):
        # The 2026-11-11 shape: consecutive Day Watcher ticks that
        # stayed "queued" forever, plus a Swing Manager one.
        runs = [_pending("Day Watcher", "queued", m, 100 + m)
                for m in (95, 90, 85)]
        runs.append(_pending("Swing Manager", "queued", 75, 7))
        findings = check_stuck_runs(runs, SATURDAY)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "WARN"
        assert f.problem.startswith("4 run(s) never started")
        assert "Day Watcher ×3, Swing Manager ×1" in f.problem
        assert "oldest 2026-07-18 14:25 UTC" in f.problem
        assert "195 190 185 7" in f.fix


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

class TestReport:
    def test_healthy_returns_none(self):
        assert build_report([]) is None

    def test_healthy_message_content(self):
        msg = healthy_message(14, 400)
        assert "all healthy" in msg
        assert "14 runs" in msg
        assert "6h40m" in msg
        assert "no failed, cancelled or stuck runs" in msg
        assert "equity" not in msg.lower()   # absent when fetch failed

    def test_healthy_message_includes_equity_when_available(self):
        msg = healthy_message(14, 400, equity=99469.2)
        assert "Paper equity: $99,469.20." in msg

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
