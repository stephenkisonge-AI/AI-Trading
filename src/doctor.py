"""Self-diagnosing health monitor for the GH Actions trading pipelines.

Runs every 6 hours (doctor.yml) plus on demand. Two families of
checks, both born from the 2026-07-11..13 incident where every visible
run was green while the system was quietly degraded:

1. PRIMARY-TRIGGER CADENCE — swing-manager's and day-watcher's primary
   trigger is an external cron (cron-job.org) POSTing
   repository_dispatch events; each workflow also has a native backup
   schedule. When the external cron dies (expired PAT → 403 →
   cron-job.org auto-disables the job), runs stay green on the backup
   at ~1/3 cadence and nothing in-repo ever fails. The doctor watches
   for silence of the *dispatch* trigger specifically.

2. FAILED-RUN CLASSIFICATION — failures that happen before our code
   starts (GitHub platform outage resolving actions, billing blocks)
   can never reach the in-code Telegram error alerts. The doctor reads
   failed runs' annotations, matches them against known signatures,
   and sends the exact problem plus the fix.

Read-only: GitHub API GETs plus one Telegram message via
src.notifier.send_alert. Touches no trading state, so it does NOT
join the alpaca-paper-trading-state concurrency group.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from src.notifier import send_alert

GITHUB_API = "https://api.github.com"
_HTTP_TIMEOUT_SEC = 30

# Cadence thresholds. Swing-manager dispatch ticks arrive every ~20
# min; 75 min without one (while the backup schedule keeps running)
# means the external cron is dead, not merely late. Day-watcher ticks
# are 5-minutely but only during US market hours; 30 min of silence
# inside the enforcement window is a dead trigger.
SWING_DISPATCH_MAX_AGE = timedelta(minutes=75)
DAY_DISPATCH_MAX_AGE = timedelta(minutes=30)

# Day-watcher enforcement window: Mon–Fri 15:00–19:30 UTC. Regular US
# cash hours are 13:30–20:00 UTC in summer and 14:30–21:00 in winter;
# this window sits inside BOTH so the check never fires on a session
# edge regardless of DST. (US market holidays will still false-positive
# a few times a year — the alert text says how to recognize that.)
_DAY_WINDOW_START = (15, 0)
_DAY_WINDOW_END = (19, 30)

_CRON_FIX = (
    "Fix: log in at console.cron-job.org and open the job whose body is "
    '{{"event_type": "{event_type}"}}. If cron-job.org disabled it '
    "(grey toggle) it has been failing repeatedly — usually 403 from an "
    "expired/revoked GitHub PAT. Update the Authorization header to "
    "'Bearer <token>' with a valid no-expiration classic PAT (repo "
    "scope), re-enable the job, and use Test run (expect HTTP 204). "
    "Full steps: memory note 'cronjob-org-dispatch-failure-mode' / "
    "RESTORE_PROMPT.md step 8."
)


@dataclass
class Finding:
    severity: str        # "CRITICAL" | "WARN"
    problem: str
    fix: str


# --- failed-run classification -------------------------------------------
# (name, signature regex, problem, fix). First match wins; matched
# against the failed run's annotation messages.
_SIGNATURES = [
    (
        "github_platform_outage",
        re.compile(r"Internal Server Error occurred while resolving"
                   r"|Failed to resolve action download info", re.I),
        "GitHub Actions platform outage — the runner could not download "
        "actions (checkout/setup-python) before our code ever ran.",
        "Nothing to change in the repo. Re-run the job if its work "
        "mattered (gh run rerun <run-id>) and check "
        "https://www.githubstatus.com if it keeps happening.",
    ),
    (
        "billing_block",
        re.compile(r"payments have failed|spending limit", re.I),
        "GitHub refused to start the job: account payment failed or the "
        "Actions spending limit is exhausted (same as the 2026-06-25 "
        "burst).",
        "github.com → Settings → Billing and plans: fix the payment "
        "method or raise the spending limit. Runs resume on their own "
        "afterwards.",
    ),
    (
        "job_timeout",
        re.compile(r"exceeded the maximum execution time", re.I),
        "A job hit its timeout-minutes limit — likely a hung API call "
        "or a stuck management pass.",
        "Read the log tail: gh run view <run-id> --log-failed. If an "
        "Alpaca call hung, the next scheduled run usually recovers; "
        "repeated timeouts in the same step are a code problem.",
    ),
    (
        "python_error",
        re.compile(r"Process completed with exit code \d+", re.I),
        "Our code exited non-zero (Python exception or explicit "
        "failure exit).",
        "gh run view <run-id> --log-failed shows the traceback; "
        "reproduce locally with the same entrypoint and fix forward. "
        "If Telegram alerts were silent, diagnose per memory note "
        "'Diagnose missed Telegram alerts via GH Actions logs first'.",
    ),
]

_UNKNOWN_FIX = ("Unrecognized failure — inspect with "
                "gh run view <run-id> --log-failed.")


def classify_failure(annotation_text: str) -> tuple[str, str, str]:
    """Match a failed run's annotation text against known signatures.
    Returns (signature_name, problem, fix)."""
    for name, rx, problem, fix in _SIGNATURES:
        if rx.search(annotation_text):
            return name, problem, fix
    excerpt = " ".join(annotation_text.split())[:200] or "(no annotations)"
    return "unknown", f"Unclassified failure: {excerpt}", _UNKNOWN_FIX


# --- cadence checks -------------------------------------------------------

def _latest_dispatch_success(runs: list[dict], workflow_name: str):
    """Most recent created_at of a successful repository_dispatch run of
    `workflow_name`, or None."""
    times = [
        datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        for r in runs
        if r.get("name") == workflow_name
        and r.get("event") == "repository_dispatch"
        and r.get("conclusion") == "success"
    ]
    return max(times) if times else None


def _in_day_watch_window(now: datetime) -> bool:
    if now.weekday() >= 5:          # Sat/Sun
        return False
    t = (now.hour, now.minute)
    return _DAY_WINDOW_START <= t <= _DAY_WINDOW_END


def check_dispatch_cadence(runs: list[dict], now: datetime) -> list[Finding]:
    """Detect a dead external-cron trigger while backup schedules keep
    the run list green."""
    findings: list[Finding] = []

    last = _latest_dispatch_success(runs, "Swing Manager")
    if last is None or now - last > SWING_DISPATCH_MAX_AGE:
        age = "never in the lookback window" if last is None else (
            f"{int((now - last).total_seconds() // 60)} min ago")
        findings.append(Finding(
            "CRITICAL",
            "Swing Manager's PRIMARY trigger is silent: last successful "
            f"swing-manage-tick dispatch was {age} (expected every ~20 "
            "min). The workflow is limping on the native backup schedule "
            "(~30–70 min), so crypto stop-watchdog/TP latency is "
            "degraded — runs still LOOK green.",
            _CRON_FIX.format(event_type="swing-manage-tick"),
        ))

    if _in_day_watch_window(now):
        last = _latest_dispatch_success(runs, "Day Watcher")
        if last is None or now - last > DAY_DISPATCH_MAX_AGE:
            age = "never in the lookback window" if last is None else (
                f"{int((now - last).total_seconds() // 60)} min ago")
            findings.append(Finding(
                "CRITICAL",
                "Day Watcher's PRIMARY trigger is silent during market "
                f"hours: last successful day-watcher-tick dispatch was "
                f"{age} (expected every 5 min Mon–Fri). If today is a US "
                "market holiday, ignore this one.",
                _CRON_FIX.format(event_type="day-watcher-tick"),
            ))

    return findings


# --- GitHub API access ----------------------------------------------------

def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}


def fetch_runs(token: str, repo: str, since: datetime,
               max_pages: int = 3) -> list[dict]:
    runs: list[dict] = []
    url = (f"{GITHUB_API}/repos/{repo}/actions/runs"
           f"?created=>={since.strftime('%Y-%m-%dT%H:%M:%SZ')}&per_page=100")
    for _ in range(max_pages):
        resp = requests.get(url, headers=_gh_headers(token),
                            timeout=_HTTP_TIMEOUT_SEC)
        resp.raise_for_status()
        runs.extend(resp.json().get("workflow_runs", []))
        url = resp.links.get("next", {}).get("url")
        if not url:
            break
    return runs


def fetch_annotation_text(token: str, repo: str, run_id: int) -> str:
    """Concatenate annotation messages of a run's failed jobs. A GH
    Actions job id doubles as a check-run id, which is what the
    annotations endpoint wants."""
    jobs_resp = requests.get(
        f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/jobs",
        headers=_gh_headers(token), timeout=_HTTP_TIMEOUT_SEC)
    jobs_resp.raise_for_status()
    texts: list[str] = []
    for job in jobs_resp.json().get("jobs", []):
        if job.get("conclusion") not in ("failure", "startup_failure",
                                         "timed_out"):
            continue
        ann_resp = requests.get(
            f"{GITHUB_API}/repos/{repo}/check-runs/{job['id']}/annotations",
            headers=_gh_headers(token), timeout=_HTTP_TIMEOUT_SEC)
        if ann_resp.ok:
            texts.extend(a.get("message", "") for a in ann_resp.json())
    return "\n".join(t for t in texts if t)


def check_failed_runs(token: str, repo: str,
                      runs: list[dict]) -> list[Finding]:
    """Classify every failed run in the window; identical diagnoses are
    grouped into one finding listing the affected run ids."""
    failed = [r for r in runs
              if r.get("conclusion") in ("failure", "startup_failure",
                                         "timed_out")]
    grouped: dict[str, dict] = {}
    for run in failed:
        text = fetch_annotation_text(token, repo, run["id"])
        name, problem, fix = classify_failure(text)
        g = grouped.setdefault(name, {"problem": problem, "fix": fix,
                                      "runs": []})
        g["runs"].append(f"{run.get('name')}#{run['id']}")
    return [
        Finding("WARN",
                f"{len(g['runs'])} failed run(s) — {g['problem']} "
                f"[{', '.join(g['runs'][:5])}]",
                g["fix"])
        for g in grouped.values()
    ]


# --- report ---------------------------------------------------------------

def healthy_message(n_runs: int, lookback_minutes: int) -> str:
    """Short all-clear note — sent on every healthy pass so the doctor's
    own liveness is visible (a silent doctor is indistinguishable from a
    dead one)."""
    return (f"🩺 doctor ✓ all healthy — {n_runs} runs checked in the last "
            f"{lookback_minutes // 60}h{lookback_minutes % 60:02d}m: "
            f"dispatch ticks on schedule, no failed runs.")


def build_report(findings: list[Finding]) -> str | None:
    """One Telegram-ready message, CRITICALs first; None when healthy."""
    if not findings:
        return None
    order = {"CRITICAL": 0, "WARN": 1}
    findings = sorted(findings, key=lambda f: order.get(f.severity, 9))
    lines = [f"🩺 DOCTOR — {len(findings)} issue(s) found"]
    for i, f in enumerate(findings, 1):
        lines.append(f"\n{i}. [{f.severity}] {f.problem}")
        lines.append(f"   FIX: {f.fix}")
    report = "\n".join(lines)
    return report[:3800] + "\n…(truncated)" if len(report) > 3800 else report


def main(argv=None) -> int:
    # The report carries emoji; a cp1252 console (local dry-run on
    # Windows) must degrade, not crash — same lesson as funnel_report.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lookback-minutes", type=int, default=400,
                        help="how far back to scan runs (default covers "
                             "the 6h doctor schedule with margin)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the report instead of sending it")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY",
                          "stephenkisonge-AI/AI-Trading")
    if not token:
        print("[doctor] GITHUB_TOKEN not set", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    runs = fetch_runs(token, repo,
                      now - timedelta(minutes=args.lookback_minutes))
    print(f"[doctor] {len(runs)} runs in the last "
          f"{args.lookback_minutes} min")

    findings = check_dispatch_cadence(runs, now)
    findings += check_failed_runs(token, repo, runs)
    report = build_report(findings)
    if report is None:
        report = healthy_message(len(runs), args.lookback_minutes)

    print(report)
    if args.dry_run:
        print("[doctor] dry-run — not sent")
        return 0
    sent = send_alert(report)
    print(f"[doctor] telegram_sent={'OK' if sent else 'FAILED'}")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
