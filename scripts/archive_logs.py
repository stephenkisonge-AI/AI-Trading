"""Archive GitHub Actions run logs before GitHub deletes them.

GitHub keeps a run's logs for 90 days and then returns HTTP 410. The
day-trade strand has no journal — a run's stdout is the only record of
WHY a management action did or did not happen — so once the logs
expire, a bad trade can no longer be explained. (Found out the hard way
on 2026-11-15: the MU loss of 2026-07-15 had to be reconstructed from
order history and price data because the run logs were gone.)

This script copies the logs of every completed workflow run into plain
text files, one per workflow per UTC day:

    <out>/<workflow>/<YYYY>/<YYYY-MM-DD>.log

Each run is one block, oldest first:

    ===== run <id> | <created_at> | <workflow name> | <event> | <conclusion> | <sha7> =====
    ...condensed job log...
    ===== end run <id> =====

"Condensed" means the steps that never explain a trade (checkout,
setup-python, pip install, post-job cleanup, runner preamble) are
replaced by a one-line marker — unless they contain an error, in which
case they are kept in full. Everything a workflow's own commands print
is kept verbatim.

Idempotent: runs already present in a day file are skipped, so the
lookback window can overlap freely. Stdlib only — the archive workflow
runs it without installing dependencies.

Usage:
    GH_TOKEN=... python scripts/archive_logs.py --out <logs checkout> --days 7
    GH_TOKEN=... python scripts/archive_logs.py --out <dir> --since 2026-08-18 --until 2026-08-24
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "stephenkisonge-AI/AI-Trading"
_HTTP_TIMEOUT_SEC = 60
# Stop downloading when the token's hourly quota gets this low. The run
# still writes what it has; the next run continues from there.
DEFAULT_MIN_REMAINING = 60

_HEADER_RE = re.compile(r"^===== run (\d+) \| ([^|]+?) \|")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ")
_STEP_START = "##[group]Run "
_POST_JOB = "Post job cleanup."
# Steps whose output never explains a trading decision.
_NOISE_STEP_RE = re.compile(r"^(actions/checkout@|actions/setup-python@|pip install\b)")
_ERROR_MARK = "##[error]"


# --- condensing a job log -------------------------------------------------

def _message(line: str) -> str:
    """The log line without its leading timestamp."""
    return _TS_RE.sub("", line, count=1)


def condense(job_log: str) -> str:
    """Drop the boilerplate steps from one job's log.

    A job log is the runner preamble followed by one segment per step,
    each starting with a `##[group]Run <command>` line, followed by the
    post-job cleanup. Boilerplate segments collapse to a marker line;
    any segment containing `##[error]` is kept whole, so a run that
    failed in `pip install` still shows why.
    """
    lines = [_ANSI_RE.sub("", ln) for ln in
             job_log.lstrip("﻿").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()

    segments: list[tuple[str, list[str]]] = [("preamble", [])]
    for ln in lines:
        msg = _message(ln)
        if msg.startswith(_STEP_START):
            segments.append((msg[len(_STEP_START):], []))
        elif msg == _POST_JOB and segments[-1][0] != "post-job cleanup":
            segments.append(("post-job cleanup", []))
        segments[-1][1].append(ln)

    out: list[str] = []
    for name, seg in segments:
        if not seg:
            continue
        boilerplate = (name in ("preamble", "post-job cleanup")
                       or _NOISE_STEP_RE.match(name))
        if boilerplate and not any(_ERROR_MARK in ln for ln in seg):
            out.append(f"[omitted {len(seg)} lines: {name}]")
        else:
            out.extend(seg)
    return "\n".join(out)


def job_logs_from_zip(data: bytes) -> list[tuple[str, str]]:
    """[(job name, raw log text)] from a run-logs zip, in job order.

    The archive always holds one `<n>_<job>.txt` per job at the top
    level; per-step files in sub-folders are only sometimes present, so
    they are ignored.
    """
    jobs: list[tuple[int, str, str]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            m = re.match(r"^(\d+)_(.+)\.txt$", name)
            if "/" in name or not m:
                continue
            text = zf.read(info).decode("utf-8", errors="replace")
            jobs.append((int(m.group(1)), m.group(2), text))
    jobs.sort()
    return [(name, text) for _, name, text in jobs]


# --- day files ------------------------------------------------------------

def workflow_slug(run: dict) -> str:
    path = run.get("path") or ""
    stem = Path(path).stem if path else (run.get("name") or "unknown")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-").lower() or "unknown"


def day_file(out_dir: Path, run: dict) -> Path:
    day = run["created_at"][:10]
    return out_dir / workflow_slug(run) / day[:4] / f"{day}.log"


def render_block(run: dict, body: str) -> str:
    header = " | ".join([
        f"===== run {run['id']}",
        run["created_at"],
        str(run.get("name") or "?"),
        str(run.get("event") or "?"),
        str(run.get("conclusion") or "?"),
        (run.get("head_sha") or "?")[:7] + " =====",
    ])
    return f"{header}\n{body.rstrip()}\n===== end run {run['id']} =====\n"


def parse_blocks(text: str) -> dict[int, tuple[str, str]]:
    """{run id: (created_at, block text)} for an existing day file."""
    blocks: dict[int, tuple[str, str]] = {}
    current: list[str] = []
    key = None
    for ln in text.splitlines(keepends=True):
        m = _HEADER_RE.match(ln)
        if m:
            if key is not None:
                blocks[key[0]] = (key[1], "".join(current))
            key = (int(m.group(1)), m.group(2).strip())
            current = []
        if key is not None:
            current.append(ln)
    if key is not None:
        blocks[key[0]] = (key[1], "".join(current))
    return blocks


def merge_into_day_file(path: Path, new_blocks: dict[int, tuple[str, str]]) -> int:
    """Add blocks to a day file, keeping it sorted oldest-first. Existing
    blocks win, so an archived log is never rewritten. Returns the number
    of blocks added."""
    existing = parse_blocks(path.read_text(encoding="utf-8")) if path.exists() else {}
    added = 0
    for run_id, block in new_blocks.items():
        if run_id not in existing:
            existing[run_id] = block
            added += 1
    if not added:
        return 0
    ordered = sorted(existing.items(), key=lambda kv: (kv[1][0], kv[0]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".log.tmp")
    tmp.write_text("".join(block for _, (_, block) in ordered),
                   encoding="utf-8", newline="\n")
    tmp.replace(path)
    return added


def archived_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    ids = set()
    with path.open(encoding="utf-8") as fh:
        for ln in fh:
            m = _HEADER_RE.match(ln)
            if m:
                ids.add(int(m.group(1)))
    return ids


# --- GitHub API -----------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHub:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self.remaining: int | None = None
        self._lock = threading.Lock()
        self._plain = urllib.request.build_opener()
        self._no_redirect = urllib.request.build_opener(_NoRedirect)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "run-log-archiver"}

    def _note_quota(self, headers) -> None:
        value = headers.get("X-RateLimit-Remaining")
        if value is not None and value.isdigit():
            with self._lock:
                self.remaining = int(value)

    def list_runs(self, day: date) -> list[dict]:
        """Completed runs created on `day` (UTC). One day per query keeps
        every query far below the API's 1000-result ceiling."""
        runs: list[dict] = []
        page = 1
        while True:
            url = (f"{GITHUB_API}/repos/{self.repo}/actions/runs"
                   f"?created={day}..{day}&status=completed"
                   f"&per_page=100&page={page}")
            req = urllib.request.Request(url, headers=self._headers())
            with self._plain.open(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                self._note_quota(resp.headers)
                batch = json.load(resp).get("workflow_runs", [])
            runs.extend(batch)
            if len(batch) < 100:
                return runs
            page += 1

    def download_logs(self, run_id: int) -> tuple[str, bytes | int]:
        """("ok", zip bytes) | ("gone", http status) | ("error", status).

        The logs endpoint answers with a redirect to short-lived blob
        storage. That hop must NOT carry the GitHub token (the blob host
        rejects foreign Authorization headers), so it is followed by
        hand.
        """
        url = f"{GITHUB_API}/repos/{self.repo}/actions/runs/{run_id}/logs"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._no_redirect.open(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                self._note_quota(resp.headers)
                return "ok", resp.read()
        except urllib.error.HTTPError as exc:
            self._note_quota(exc.headers)
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location")
                if not location:
                    return "error", exc.code
                try:
                    blob_req = urllib.request.Request(
                        location, headers={"User-Agent": "run-log-archiver"})
                    with self._plain.open(blob_req, timeout=_HTTP_TIMEOUT_SEC) as blob:
                        return "ok", blob.read()
                except urllib.error.HTTPError as blob_exc:
                    return "error", blob_exc.code
                except (urllib.error.URLError, OSError):
                    return "error", 0
            # 410 = retention expired, 404 = the run produced no logs.
            # Both are permanent: record the fact so it is not retried.
            if exc.code in (404, 410):
                return "gone", exc.code
            return "error", exc.code
        except (urllib.error.URLError, OSError):
            return "error", 0


# --- driver ---------------------------------------------------------------

def build_block(run: dict, status: str, payload) -> tuple[str, str] | None:
    if status == "gone":
        body = f"[log unavailable: HTTP {payload} when archived]"
    elif status == "ok":
        try:
            jobs = job_logs_from_zip(payload)
        except zipfile.BadZipFile:
            return None
        if not jobs:
            body = "[log archive contained no job logs]"
        elif len(jobs) == 1:
            body = condense(jobs[0][1])
        else:
            body = "\n".join(f"--- job: {name} ---\n{condense(text)}"
                             for name, text in jobs)
    else:
        return None
    return run["created_at"], render_block(run, body)


def archive_day(gh: GitHub, out_dir: Path, day: date, workers: int,
                min_remaining: int) -> dict:
    stats = {"day": str(day), "runs": 0, "added": 0, "failed": 0,
             "skipped_quota": 0}
    runs = gh.list_runs(day)
    stats["runs"] = len(runs)
    have: dict[Path, set[int]] = {}
    todo = []
    for run in runs:
        path = day_file(out_dir, run)
        if path not in have:
            have[path] = archived_ids(path)
        if run["id"] not in have[path]:
            todo.append(run)
    todo.sort(key=lambda r: r["created_at"])

    def fetch(run):
        if gh.remaining is not None and gh.remaining < min_remaining:
            return run, "quota", None
        status, payload = gh.download_logs(run["id"])
        return run, status, payload

    new: dict[Path, dict[int, tuple[str, str]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for run, status, payload in pool.map(fetch, todo):
            if status == "quota":
                stats["skipped_quota"] += 1
                continue
            block = build_block(run, status, payload)
            if block is None:
                stats["failed"] += 1
                continue
            new.setdefault(day_file(out_dir, run), {})[run["id"]] = block
    for path, blocks in new.items():
        stats["added"] += merge_into_day_file(path, blocks)
    return stats


def _parse_day(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path,
                    help="root of the logs checkout to write into")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY")
                    or DEFAULT_REPO)
    ap.add_argument("--days", type=int, default=7,
                    help="archive runs from the last N UTC days (default 7)")
    ap.add_argument("--since", type=_parse_day, help="first UTC day, YYYY-MM-DD")
    ap.add_argument("--until", type=_parse_day, help="last UTC day, YYYY-MM-DD")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-remaining", type=int, default=DEFAULT_MIN_REMAINING)
    args = ap.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[archive] GH_TOKEN / GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    if not args.out.is_dir():
        print(f"[archive] --out {args.out} is not a directory", file=sys.stderr)
        return 2

    today = datetime.now(timezone.utc).date()
    until = args.until or today
    since = args.since or (until - timedelta(days=max(args.days, 1) - 1))
    gh = GitHub(token, args.repo)

    totals = {"runs": 0, "added": 0, "failed": 0, "skipped_quota": 0}
    day = since
    while day <= until:           # oldest first: those expire first
        try:
            stats = archive_day(gh, args.out, day, args.workers,
                                args.min_remaining)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"[archive] {day}: listing runs failed: {exc}", file=sys.stderr)
            return 1
        for key in totals:
            totals[key] += stats[key]
        print(f"[archive] {day}: runs={stats['runs']} added={stats['added']} "
              f"failed={stats['failed']} quota_skipped={stats['skipped_quota']} "
              f"api_remaining={gh.remaining}")
        if stats["skipped_quota"]:
            print("[archive] API quota nearly spent — stopping here; the "
                  "next run continues from this day.")
            break
        day += timedelta(days=1)

    print(f"[archive] done: runs_seen={totals['runs']} added={totals['added']} "
          f"failed={totals['failed']} quota_skipped={totals['skipped_quota']}")
    # Downloads that failed are retried by the next run. Only a run where
    # nothing at all could be fetched is an error worth a red workflow.
    if totals["failed"] and not totals["added"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
