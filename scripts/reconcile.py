"""Read-only journal-vs-broker reconciliation CLI — Phase 1.

Usage:
    python scripts/reconcile.py [--journal-dir PATH] [--days N] [--sync]

Exit codes:
    0 — reconciliation complete: journal and Alpaca paper account agree
    1 — mismatches or unknown states exist (report printed)
    2 — could not run (journal unavailable, broker unreachable, sync failed)

This command NEVER submits, cancels, or replaces an order. It performs
GET-only broker calls. Any future repair functionality must be a
separate, explicit paper-repair command — it does not belong here.

Journal selection (first match wins):
    --journal-dir PATH   explicit path (GitJournal if PATH/.git exists)
    STATE_REPO_DIR       clone of the private state repo (GitJournal)
    JOURNAL_DIR          plain file journal (local testing)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from src.data import get_client
from src.journal import GitJournal, Journal, journal_from_env
from src.reconciliation import reconcile


def _load_journal(journal_dir: str | None):
    if journal_dir:
        root = Path(journal_dir)
        if (root / ".git").exists():
            return GitJournal(root)
        return Journal(root)
    return journal_from_env()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal-dir", default=None,
                        help="journal root (default: STATE_REPO_DIR / JOURNAL_DIR env)")
    parser.add_argument("--days", type=int, default=14,
                        help="how many days of recent broker orders to inspect")
    parser.add_argument("--sync", action="store_true",
                        help="git-pull the state repo before reconciling (GitJournal only)")
    parser.add_argument("--clear-freeze", action="store_true",
                        help="if (and only if) reconciliation is fully clean, "
                             "lift the journal entry freeze. Journal-only "
                             "write; still never touches broker orders.")
    args = parser.parse_args(argv)

    load_dotenv()

    journal = _load_journal(args.journal_dir)
    if journal is None:
        print("reconcile: no journal configured "
              "(set STATE_REPO_DIR or JOURNAL_DIR, or pass --journal-dir)",
              file=sys.stderr)
        return 2

    if args.sync and isinstance(journal, GitJournal):
        if not journal.sync():
            print("reconcile: state-repo sync FAILED — refusing to "
                  "reconcile against stale journal (fail closed)",
                  file=sys.stderr)
            return 2

    try:
        client = get_client()
        positions = client.get_all_positions()
        open_orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, limit=500,
        ))
        recent_orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=datetime.now(timezone.utc) - timedelta(days=args.days),
            limit=500,
        ))
    except Exception as exc:
        print(f"reconcile: broker fetch failed: {exc}", file=sys.stderr)
        return 2

    report = reconcile(journal, positions, open_orders, recent_orders)
    print(report.render())
    if report.ok and args.clear_freeze and journal.entry_freeze()["frozen"]:
        if journal.set_entry_freeze(False, "reconciliation passed clean"):
            print("entry freeze LIFTED (reconciliation clean).")
        else:
            print("reconciliation clean but freeze-lift persistence failed — "
                  "freeze remains.", file=sys.stderr)
            return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
