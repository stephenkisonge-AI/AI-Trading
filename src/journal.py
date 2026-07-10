"""Durable trade journal — Phase 1 of fix/paper-execution-safety.

Append-only JSONL event log plus a compact current-state snapshot.
Deployment Option B: the authoritative copy lives in a separate PRIVATE
GitHub repository (AI-Trading-State), synced via `GitJournal`. Trading
code talks only to this abstraction — never to SQLite or git directly.

Files inside the journal root, scoped PER STRAND so one strand's
entry-freeze/risk state can never gate the other (both strands share
one Alpaca paper account but are otherwise independent):
    <strand>/events.jsonl   — append-only, one JSON event per line
    <strand>/snapshot.json  — compact materialized state, rewritten on
                              each append

Event shape:
    {
      "event_id":  uuid4 hex,
      "ts":        UTC ISO-8601,
      "kind":      one of EVENT_KINDS,
      "strand":    "swing" | "day",
      "trade_id":  signal/trade identifier or null,
      "payload":   kind-specific dict (secret-scrubbed)
    }

Persistence contract (rules 12/16 of the safety spec):
    `append()` returns True only when the event is durably persisted —
    for `Journal` that means fsynced to disk, for `GitJournal` committed
    AND pushed to the private remote. Callers MUST NOT submit a broker
    order whose intent event could not be persisted. A False return from
    a GitJournal append may still leave the event in the local clone;
    that is safe (the order was never submitted) and the event is pushed
    by a later successful sync.

No secrets ever enter state files: payloads are scrubbed of keys that
look like credentials or account identifiers before writing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


EVENT_KINDS = {
    "SIGNAL",             # signal detected: conditions, regime, config hash
    "TRADE_PLANNED",      # planned entry/stop/TPs/approved risk/sizing inputs
    "ACTION_INTENT",      # about to touch the broker (persist BEFORE submit)
    "ORDER_SUBMITTED",    # broker's immediate response to a submission
    "ORDER_STATE",        # verified broker state after a follow-up query
    "POSITION_SNAPSHOT",  # broker position at a point in time
    "STATE_TRANSITION",   # trade state machine moves
    "EXIT_REALIZED",      # an exit tranche actually filled
    "TRADE_CLOSED",       # terminal: realized R, summary
    "ERROR",              # anything that went wrong
    "ENTRY_FREEZE",       # freeze/unfreeze new-entry creation
    "RECOVERY_REQUIRED",  # trade needs manual/automated reconciliation
}

# Trade states considered finished — reconciliation expects no broker
# presence for these.
TERMINAL_TRADE_STATES = {"CLOSED", "ABORTED", "REJECTED"}

# Payload keys whose values must never reach a state file. Matched as
# case-insensitive substrings of the key name.
_SECRET_KEY_PATTERN = re.compile(
    r"api_key|apikey|secret|token|webhook|password|authorization"
    r"|account_number|account_id",
    re.IGNORECASE,
)

# Anything that looks like credentials embedded in a URL (git remotes).
_URL_CRED_PATTERN = re.compile(r"://[^@/\s]+@")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scrub_secrets(value):
    """Recursively redact secret-looking keys from a payload."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if _SECRET_KEY_PATTERN.search(str(k)) else scrub_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_secrets(v) for v in value]
    return value


def redact_url_credentials(text: str) -> str:
    """Strip embedded credentials from URLs before any text is logged."""
    return _URL_CRED_PATTERN.sub("://***@", text)


class TradeView:
    """Materialized per-trade state folded from journal events."""

    def __init__(self, trade_id: str):
        self.trade_id = trade_id
        self.symbol: Optional[str] = None
        self.setup: Optional[str] = None
        self.strand: Optional[str] = None
        self.state: str = "PLANNED"
        self.plan: dict = {}
        self.signal: dict = {}
        # action_id -> {"intent": ..., "submitted": ..., "states": [...]}
        self.actions: dict[str, dict] = {}
        self.exits: list[dict] = []
        self.errors: list[dict] = []
        self.recovery_required: bool = False
        self.recovery_reason: Optional[str] = None
        self.realized_r: Optional[float] = None
        self.created_at: Optional[str] = None
        self.updated_at: Optional[str] = None

    # -- derived quantities -------------------------------------------------

    def entry_filled_qty(self) -> float:
        """Sum of verified entry fills (latest ORDER_STATE per entry action)."""
        total = 0.0
        for action in self.actions.values():
            intent = action.get("intent") or {}
            if intent.get("action") != "entry":
                continue
            last = action["states"][-1] if action["states"] else None
            if last and last.get("filled_qty"):
                total += float(last["filled_qty"])
        return total

    def realized_exit_qty(self) -> float:
        return sum(float(e.get("qty") or 0) for e in self.exits)

    def expected_position_qty(self) -> float:
        return self.entry_filled_qty() - self.realized_exit_qty()

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TRADE_STATES

    def unresolved_intents(self) -> list[str]:
        """Action IDs persisted as intent but with no broker response and
        no verified state — the ambiguous window reconciliation must flag.
        """
        return [
            action_id
            for action_id, action in self.actions.items()
            if action.get("intent") is not None
            and action.get("submitted") is None
            and not action["states"]
        ]


class Journal:
    """File-backed journal (append-only JSONL + snapshot)."""

    def __init__(self, root: Path | str, strand: str = "swing"):
        self.root = Path(root)
        self.strand = strand
        # Per-strand subtree: freeze events, trades, and snapshots for
        # one strand are invisible to the other's journal instance.
        strand_dir = self.root / strand
        strand_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = strand_dir / "events.jsonl"
        self.snapshot_path = strand_dir / "snapshot.json"

    # -- core ----------------------------------------------------------------

    def append(self, kind: str, trade_id: Optional[str] = None,
               payload: Optional[dict] = None) -> bool:
        """Durably persist one event. True only on confirmed persistence."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown journal event kind: {kind}")
        event = {
            "event_id": uuid.uuid4().hex,
            "ts": utcnow_iso(),
            "kind": kind,
            "strand": self.strand,
            "trade_id": trade_id,
            "payload": scrub_secrets(payload or {}),
        }
        try:
            line = json.dumps(event, separators=(",", ":"), default=str)
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._write_snapshot()
            return True
        except Exception as exc:
            print(f"[journal] append FAILED ({kind}): {exc}", file=sys.stderr)
            return False

    def events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        out = []
        with open(self.events_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        # Union merges of concurrent appends can interleave lines, so file
        # order is not authoritative — timestamp order (event_id tiebreak,
        # deterministic) is. ISO-8601 UTC sorts lexicographically.
        out.sort(key=lambda e: (e.get("ts", ""), e.get("event_id", "")))
        return out

    # -- emitters ------------------------------------------------------------

    def record_signal(self, trade_id: str, **payload) -> bool:
        return self.append("SIGNAL", trade_id, payload)

    def record_trade_planned(self, trade_id: str, **payload) -> bool:
        return self.append("TRADE_PLANNED", trade_id, payload)

    def record_action_intent(self, trade_id: str, action_id: str,
                             action: str, **payload) -> bool:
        """Persist BEFORE submitting the broker request. If this returns
        False the caller must not submit."""
        payload.update({"action_id": action_id, "action": action})
        return self.append("ACTION_INTENT", trade_id, payload)

    def record_order_submitted(self, trade_id: str, action_id: str,
                               **payload) -> bool:
        payload["action_id"] = action_id
        return self.append("ORDER_SUBMITTED", trade_id, payload)

    def record_order_state(self, trade_id: str, action_id: str,
                           **payload) -> bool:
        payload["action_id"] = action_id
        return self.append("ORDER_STATE", trade_id, payload)

    def record_position_snapshot(self, trade_id: Optional[str],
                                 **payload) -> bool:
        return self.append("POSITION_SNAPSHOT", trade_id, payload)

    def record_state_transition(self, trade_id: str, to_state: str,
                                reason: str = "") -> bool:
        return self.append("STATE_TRANSITION", trade_id,
                           {"to_state": to_state, "reason": reason})

    def record_exit(self, trade_id: str, **payload) -> bool:
        return self.append("EXIT_REALIZED", trade_id, payload)

    def record_trade_closed(self, trade_id: str, **payload) -> bool:
        ok = self.append("TRADE_CLOSED", trade_id, payload)
        if ok:
            ok = self.record_state_transition(trade_id, "CLOSED",
                                              payload.get("reason", ""))
        return ok

    def record_error(self, trade_id: Optional[str], message: str,
                     **payload) -> bool:
        payload["message"] = message
        return self.append("ERROR", trade_id, payload)

    def set_entry_freeze(self, frozen: bool, reason: str) -> bool:
        return self.append("ENTRY_FREEZE", None,
                           {"frozen": frozen, "reason": reason})

    def record_recovery_required(self, trade_id: str, reason: str) -> bool:
        ok = self.append("RECOVERY_REQUIRED", trade_id, {"reason": reason})
        if ok:
            ok = self.record_state_transition(trade_id, "RECOVERY_REQUIRED",
                                              reason)
        return ok

    # -- queries -------------------------------------------------------------

    def entry_freeze(self) -> dict:
        """Latest freeze state: {"frozen": bool, "reason": str}."""
        state = {"frozen": False, "reason": ""}
        for event in self.events():
            if event["kind"] == "ENTRY_FREEZE":
                state = {
                    "frozen": bool(event["payload"].get("frozen")),
                    "reason": event["payload"].get("reason", ""),
                }
        return state

    def trades(self) -> dict[str, TradeView]:
        """Fold all events into per-trade views, chronological order."""
        views: dict[str, TradeView] = {}
        for event in self.events():
            trade_id = event.get("trade_id")
            if not trade_id:
                continue
            view = views.setdefault(trade_id, TradeView(trade_id))
            if view.created_at is None:
                view.created_at = event["ts"]
            view.updated_at = event["ts"]
            kind = event["kind"]
            payload = event.get("payload") or {}

            if kind == "SIGNAL":
                view.signal = payload
                view.symbol = payload.get("symbol", view.symbol)
                view.setup = payload.get("setup", view.setup)
                view.strand = event.get("strand", view.strand)
            elif kind == "TRADE_PLANNED":
                view.plan = payload
                view.symbol = payload.get("symbol", view.symbol)
                view.setup = payload.get("setup", view.setup)
            elif kind == "ACTION_INTENT":
                action = view.actions.setdefault(
                    payload.get("action_id", ""),
                    {"intent": None, "submitted": None, "states": []})
                action["intent"] = payload
            elif kind == "ORDER_SUBMITTED":
                action = view.actions.setdefault(
                    payload.get("action_id", ""),
                    {"intent": None, "submitted": None, "states": []})
                action["submitted"] = payload
            elif kind == "ORDER_STATE":
                action = view.actions.setdefault(
                    payload.get("action_id", ""),
                    {"intent": None, "submitted": None, "states": []})
                action["states"].append(payload)
            elif kind == "STATE_TRANSITION":
                view.state = payload.get("to_state", view.state)
            elif kind == "EXIT_REALIZED":
                view.exits.append(payload)
            elif kind == "TRADE_CLOSED":
                view.realized_r = payload.get("realized_r", view.realized_r)
            elif kind == "ERROR":
                view.errors.append(payload)
            elif kind == "RECOVERY_REQUIRED":
                view.recovery_required = True
                view.recovery_reason = payload.get("reason")
        return views

    def open_trades(self) -> dict[str, TradeView]:
        return {tid: v for tid, v in self.trades().items()
                if not v.is_terminal()}

    def find_action_by_client_order_id(self, client_order_id: str):
        """(trade_id, action_id, action dict) or None."""
        if not client_order_id:
            return None
        for trade_id, view in self.trades().items():
            for action_id, action in view.actions.items():
                intent = action.get("intent") or {}
                if intent.get("client_order_id") == client_order_id:
                    return trade_id, action_id, action
        return None

    def known_client_order_ids(self) -> set[str]:
        ids: set[str] = set()
        for view in self.trades().values():
            for action in view.actions.values():
                intent = action.get("intent") or {}
                coid = intent.get("client_order_id")
                if coid:
                    ids.add(coid)
        return ids

    # -- snapshot ------------------------------------------------------------

    def _write_snapshot(self) -> None:
        views = self.trades()
        open_trades = {
            tid: {
                "symbol": v.symbol,
                "setup": v.setup,
                "state": v.state,
                "expected_position_qty": v.expected_position_qty(),
                "recovery_required": v.recovery_required,
                "updated_at": v.updated_at,
            }
            for tid, v in views.items() if not v.is_terminal()
        }
        snapshot = {
            "updated_at": utcnow_iso(),
            "strand": self.strand,
            "entry_freeze": self.entry_freeze(),
            "open_trades": open_trades,
            "total_events": len(self.events()),
            "total_trades": len(views),
        }
        tmp = self.snapshot_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2, default=str)
        tmp.replace(self.snapshot_path)


class GitJournal(Journal):
    """Journal whose root is a clone of the private state repository.

    `append()` is durable only once the commit is PUSHED to the remote.
    `sync()` must be called at the start of every management/entry pass;
    a failed sync means broker actions must not proceed (fail closed).
    """

    def __init__(self, root: Path | str, strand: str = "swing",
                 push_retries: int = 3):
        super().__init__(root, strand)
        self.push_retries = push_retries
        if not (self.root / ".git").exists():
            raise ValueError(
                f"GitJournal root {self.root} is not a git clone; "
                f"clone the private state repo there first"
            )
        self._ensure_merge_config()

    # -- git plumbing ---------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, timeout=120,
        )

    def _git_ok(self, *args: str) -> bool:
        proc = self._git(*args)
        if proc.returncode != 0:
            detail = redact_url_credentials(
                (proc.stderr or proc.stdout or "").strip()[:500])
            print(f"[journal] git {args[0]} failed: {detail}", file=sys.stderr)
            return False
        return True

    def _ensure_merge_config(self) -> None:
        """Concurrent appends must merge, not conflict.

        events.jsonl is append-only → union merge keeps both writers'
        lines (readers order by timestamp, not file position).
        snapshot.json is regenerable → keep the checked-out side on
        conflict; it is rewritten from merged events afterwards.
        """
        attributes = self.root / ".gitattributes"
        wanted = "events.jsonl merge=union\nsnapshot.json merge=ours\n"
        if not attributes.exists() or attributes.read_text() != wanted:
            attributes.write_text(wanted)
        self._git("config", "merge.ours.driver", "true")

    def _rebase_pull(self) -> bool:
        """pull --rebase; on failure leave a clean tree (abort), not a
        half-rebased repo. False = fail closed."""
        if self._git_ok("pull", "--rebase", "--quiet"):
            return True
        self._git("rebase", "--abort")
        return False

    def _commit_local(self, message: str) -> bool:
        if not self._git_ok("add", "-A"):
            return False
        commit = self._git("commit", "-m", message)
        if commit.returncode != 0 and "nothing to commit" not in (
                commit.stdout + commit.stderr):
            print(f"[journal] git commit failed: "
                  f"{redact_url_credentials((commit.stderr or '')[:500])}",
                  file=sys.stderr)
            return False
        return True

    def sync(self) -> bool:
        """Bring the clone up to date with the remote. Called at the
        start of every management/entry pass. False = fail closed."""
        # Leftovers from a crashed run must be committed first or the
        # rebase refuses to start.
        if not self._commit_local("journal: local recovery commit"):
            return False
        return self._rebase_pull()

    def append(self, kind: str, trade_id: Optional[str] = None,
               payload: Optional[dict] = None) -> bool:
        if not super().append(kind, trade_id, payload):
            return False
        return self._commit_and_push(kind, trade_id)

    def _commit_and_push(self, kind: str, trade_id: Optional[str]) -> bool:
        if not self._commit_local(f"journal: {kind} {trade_id or ''}".strip()):
            return False
        for _attempt in range(self.push_retries):
            if self._git_ok("push", "--quiet"):
                return True
            # Someone else pushed first — replay our append-only commits
            # on top of theirs and retry. Bounded; fail closed after that.
            if not self._rebase_pull():
                return False
            # The union merge may have combined event streams; the
            # snapshot must be regenerated from the merged events.
            self._write_snapshot()
            if not self._commit_local("journal: snapshot refresh after merge"):
                return False
        print(f"[journal] push failed after {self.push_retries} attempts — "
              f"treating event as NOT persisted (fail closed)",
              file=sys.stderr)
        return False


def journal_from_env(strand: str = "swing") -> Optional[Journal]:
    """Build the journal from environment configuration.

    STATE_REPO_DIR — path to a clone of the private state repo (GitJournal).
    JOURNAL_DIR    — plain file journal (local testing only).
    Neither set    — returns None; callers must treat that as
                     "journal unavailable" and freeze new entries.

    Both strands share the same state repo clone; the strand argument
    selects the per-strand subtree so their freeze/risk state stays
    independent.
    """
    state_repo_dir = os.environ.get("STATE_REPO_DIR")
    if state_repo_dir:
        return GitJournal(state_repo_dir, strand=strand)
    journal_dir = os.environ.get("JOURNAL_DIR")
    if journal_dir:
        return Journal(journal_dir, strand=strand)
    return None
