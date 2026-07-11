"""Management-only pass for the crypto swing strand (Addendum E).

Runs every 15-30 minutes via repository_dispatch (same external-cron
pattern as day-watcher.yml), separate from the 4-hour entry scan. At
GH Actions cadence the gap watchdog and TP triggers are only as fast
as this workflow — a 4-hour management gap materially weakens both,
which is why this exists.

THIS ENTRYPOINT NEVER PLACES ENTRIES. It only protects, reduces, or
closes existing positions: stop monitoring, gap watchdog, TP
transitions, trail raises, time stops, regime exits, and crash-window
recovery. It runs even while entries are frozen.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from src.data import _assert_paper_mode, get_client
from src.execution import load_exec_config
from src.heartbeat import ping_heartbeat
from src.notifier import send_alert
from src.swing_exits import manage_swing_trades
from src.swing_runtime import (
    build_synced_journal,
    quote_for,
    regime_for,
    runner_ctx_for,
)
from src.trader import auto_execute_enabled


def main() -> int:
    load_dotenv()
    _assert_paper_mode()
    started = datetime.now(timezone.utc)
    print(f"[swing-manager] started at {started.isoformat()}")

    if not auto_execute_enabled():
        # Nothing to manage in alerts-only mode — the journal has no
        # authority over manually-placed positions.
        print("[swing-manager] auto-execute disabled — nothing to manage")
        return 0

    journal, journal_error = build_synced_journal()
    if journal is None:
        # Without the journal we cannot know what we own or whether a
        # transition is safe. Alert loudly; do not guess.
        print(f"[swing-manager] journal unavailable: {journal_error}",
              file=sys.stderr)
        send_alert(f"⚠️ swing-manager could not load the state journal "
                   f"({journal_error}) — positions were NOT managed this "
                   f"pass.")
        return 1

    try:
        config = load_exec_config()
    except ValueError as exc:
        print(f"[swing-manager] exec config invalid: {exc}", file=sys.stderr)
        send_alert(f"⚠️ swing-manager exec config invalid ({exc}) — "
                   f"positions were NOT managed this pass.")
        return 1

    open_before = len(journal.open_trades())
    actions = manage_swing_trades(
        get_client(), journal, config=config, alert_fn=send_alert,
        get_quote_fn=quote_for, regime_fn=regime_for,
        runner_ctx_fn=runner_ctx_for)
    for action in actions:
        print(f"[swing-manager] action: {action}")

    freeze = journal.entry_freeze()
    print(f"[swing-manager] open_trades={open_before} actions={len(actions)} "
          f"entry_freeze={'ON' if freeze['frozen'] else 'OFF'}")
    ping_heartbeat()
    print(f"[swing-manager] done at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
