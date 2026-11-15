"""Tests for scripts/archive_logs.py — the pure parts (no network)."""
from __future__ import annotations

import io
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import archive_logs as al

TS = "2026-11-15T20:36:0{}.0000000Z "


def _job_log(pip_error: bool = False) -> str:
    lines = [
        "﻿" + TS.format(0) + "Current runner version: '2.338.2'",
        TS.format(0) + "##[group]Runner Image",
        TS.format(0) + "##[endgroup]",
        TS.format(1) + "##[group]Run actions/checkout@v4",
        TS.format(1) + "##[endgroup]",
        TS.format(1) + "##[group]Fetching the repository",
        TS.format(1) + "##[endgroup]",
        TS.format(2) + "##[group]Run pip install -r requirements.txt",
        TS.format(2) + "##[endgroup]",
        TS.format(2) + "Collecting alpaca-py",
    ]
    if pip_error:
        lines.append(TS.format(2) + "##[error]Process completed with exit code 1.")
    lines += [
        TS.format(3) + "##[group]Run python -m src.day_watcher",
        TS.format(3) + "##[endgroup]",
        TS.format(4) + "\x1b[36;1m[day_watcher] phase=intraday_scan\x1b[0m",
        TS.format(4) + "[day_trader] close_position MU attempt 1/3 failed: boom",
        TS.format(5) + "Post job cleanup.",
        TS.format(5) + "[command]/usr/bin/git version",
        TS.format(5) + "Post job cleanup.",
        TS.format(5) + "Cleaning up orphan processes",
    ]
    return "\r\n".join(lines) + "\r\n"


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _run(run_id=1, created="2026-11-15T20:35:42Z", path=".github/workflows/day-watcher.yml",
         name="Day Watcher"):
    return {"id": run_id, "created_at": created, "path": path, "name": name,
            "event": "repository_dispatch", "conclusion": "success",
            "head_sha": "fdf2e26abcdef"}


def test_condense_keeps_the_workflow_output_and_drops_boilerplate():
    out = al.condense(_job_log())
    assert "[day_watcher] phase=intraday_scan" in out
    assert "close_position MU attempt 1/3 failed: boom" in out
    assert "Collecting alpaca-py" not in out
    assert "Fetching the repository" not in out
    assert "Cleaning up orphan processes" not in out
    assert "[omitted 3 lines: preamble]" in out
    assert "[omitted 4 lines: actions/checkout@v4]" in out
    assert "[omitted 3 lines: pip install -r requirements.txt]" in out
    assert "[omitted 4 lines: post-job cleanup]" in out


def test_condense_strips_ansi_colour_and_bom():
    out = al.condense(_job_log())
    assert "\x1b" not in out and "﻿" not in out


def test_condense_keeps_a_boilerplate_step_that_failed():
    out = al.condense(_job_log(pip_error=True))
    assert "Collecting alpaca-py" in out
    assert "##[error]Process completed with exit code 1." in out
    assert "omitted 3 lines: pip install" not in out


def test_job_logs_from_zip_uses_top_level_job_files_in_order():
    data = _zip({
        "1_report.txt": "second job",
        "0_scan.txt": "first job",
        "scan/system.txt": "ignored",
        "scan/8_Run watcher.txt": "ignored step file",
    })
    assert al.job_logs_from_zip(data) == [("scan", "first job"),
                                          ("report", "second job")]


def test_day_file_is_per_workflow_per_utc_day():
    path = al.day_file(Path("root"), _run())
    assert path == Path("root") / "day-watcher" / "2026" / "2026-11-15.log"


def test_block_round_trips_and_merge_is_sorted_and_idempotent(tmp_path):
    late = _run(run_id=20, created="2026-11-15T15:00:00Z")
    early = _run(run_id=10, created="2026-11-15T14:00:00Z")
    path = al.day_file(tmp_path, late)

    block_late = al.build_block(late, "ok", _zip({"0_scan.txt": _job_log()}))
    assert al.merge_into_day_file(path, {20: block_late}) == 1
    block_early = al.build_block(early, "gone", 410)
    assert al.merge_into_day_file(path, {10: block_early}) == 1

    text = path.read_text(encoding="utf-8")
    assert text.index("===== run 10 |") < text.index("===== run 20 |")
    assert "[log unavailable: HTTP 410 when archived]" in text
    assert al.archived_ids(path) == {10, 20}
    assert set(al.parse_blocks(text)) == {10, 20}

    # Re-adding an archived run changes nothing — the first copy wins.
    replacement = (late["created_at"], al.render_block(late, "REWRITTEN"))
    assert al.merge_into_day_file(path, {20: replacement}) == 0
    assert path.read_text(encoding="utf-8") == text


def test_multi_job_runs_are_labelled_per_job():
    _, block = al.build_block(
        _run(), "ok", _zip({"0_a.txt": _job_log(), "1_b.txt": _job_log()}))
    assert "--- job: a ---" in block and "--- job: b ---" in block


def test_unreadable_download_is_not_archived():
    assert al.build_block(_run(), "ok", b"not a zip") is None
    assert al.build_block(_run(), "error", 502) is None


class _FakeGitHub:
    def __init__(self, runs, remaining=5000, fail=()):
        self._runs, self.remaining, self._fail = runs, remaining, set(fail)
        self.downloads: list[int] = []

    def list_runs(self, day):
        return list(self._runs)

    def download_logs(self, run_id):
        self.downloads.append(run_id)
        if run_id in self._fail:
            return "error", 502
        return "ok", _zip({"0_scan.txt": _job_log()})


def test_archive_day_skips_what_is_already_archived(tmp_path):
    runs = [_run(run_id=1), _run(run_id=2, created="2026-11-15T20:40:00Z")]
    gh = _FakeGitHub(runs)
    first = al.archive_day(gh, tmp_path, date(2026, 11, 15), 2, 60)
    assert (first["added"], first["failed"]) == (2, 0)
    gh.downloads.clear()
    second = al.archive_day(gh, tmp_path, date(2026, 11, 15), 2, 60)
    assert second["added"] == 0 and gh.downloads == []


def test_archive_day_retries_failed_downloads_next_time(tmp_path):
    runs = [_run(run_id=1), _run(run_id=2, created="2026-11-15T20:40:00Z")]
    stats = al.archive_day(_FakeGitHub(runs, fail=[2]), tmp_path,
                           date(2026, 11, 15), 2, 60)
    assert (stats["added"], stats["failed"]) == (1, 1)
    stats = al.archive_day(_FakeGitHub(runs), tmp_path, date(2026, 11, 15), 2, 60)
    assert stats["added"] == 1


def test_archive_day_stops_downloading_when_quota_is_low(tmp_path):
    gh = _FakeGitHub([_run(run_id=1)], remaining=10)
    stats = al.archive_day(gh, tmp_path, date(2026, 11, 15), 2, 60)
    assert stats["skipped_quota"] == 1 and stats["added"] == 0
    assert gh.downloads == []
