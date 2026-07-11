"""Tests for src/journal.py — Phase 1 durable journal."""
import json
import subprocess

import pytest

from src.journal import (
    GitJournal,
    Journal,
    journal_from_env,
    redact_url_credentials,
    scrub_secrets,
)


# ---------------------------------------------------------------------------
# scrubbing
# ---------------------------------------------------------------------------

def test_scrub_secrets_redacts_credential_keys():
    payload = {
        "symbol": "BTC/USD",
        "api_key": "AKIAXXXX",
        "nested": {"webhook_url": "https://hooks.example/secret",
                   "qty": 0.5},
        "orders": [{"ALPACA_SECRET_KEY": "shhh", "price": 1.0}],
        "account_number": "PA3XXXX",
    }
    scrubbed = scrub_secrets(payload)
    assert scrubbed["symbol"] == "BTC/USD"
    assert scrubbed["api_key"] == "<redacted>"
    assert scrubbed["nested"]["webhook_url"] == "<redacted>"
    assert scrubbed["nested"]["qty"] == 0.5
    assert scrubbed["orders"][0]["ALPACA_SECRET_KEY"] == "<redacted>"
    assert scrubbed["orders"][0]["price"] == 1.0
    assert scrubbed["account_number"] == "<redacted>"


def test_redact_url_credentials():
    text = "fatal: could not read from 'https://x-access-token:ghp_abc123@github.com/o/r.git'"
    assert "ghp_abc123" not in redact_url_credentials(text)
    assert "://***@github.com" in redact_url_credentials(text)


# ---------------------------------------------------------------------------
# file journal basics
# ---------------------------------------------------------------------------

def test_append_and_read_roundtrip(tmp_path):
    journal = Journal(tmp_path / "j")
    assert journal.append("SIGNAL", "T1", {"symbol": "BTC/USD", "setup": "A"})
    events = journal.events()
    assert len(events) == 1
    assert events[0]["kind"] == "SIGNAL"
    assert events[0]["trade_id"] == "T1"
    assert events[0]["payload"]["symbol"] == "BTC/USD"
    assert events[0]["strand"] == "swing"


def test_append_rejects_unknown_kind(tmp_path):
    journal = Journal(tmp_path / "j")
    with pytest.raises(ValueError):
        journal.append("NOT_A_KIND", "T1", {})


def test_append_returns_false_when_persistence_fails(tmp_path):
    journal = Journal(tmp_path / "j")
    # Point the events file into a directory that does not exist —
    # persistence must fail closed, not raise.
    journal.events_path = tmp_path / "missing-dir" / "events.jsonl"
    assert journal.append("SIGNAL", "T1", {}) is False


def test_secrets_never_reach_disk(tmp_path):
    journal = Journal(tmp_path / "j")
    journal.append("ERROR", "T1", {"message": "boom", "api_key": "AKIA-LEAK"})
    raw = journal.events_path.read_text()
    assert "AKIA-LEAK" not in raw
    assert "<redacted>" in raw


def test_entry_freeze_toggles(tmp_path):
    journal = Journal(tmp_path / "j")
    assert journal.entry_freeze()["frozen"] is False
    journal.set_entry_freeze(True, "reconcile mismatch")
    assert journal.entry_freeze() == {"frozen": True, "reason": "reconcile mismatch"}
    journal.set_entry_freeze(False, "reconciled clean")
    assert journal.entry_freeze()["frozen"] is False


# ---------------------------------------------------------------------------
# trade materialization
# ---------------------------------------------------------------------------

def _seed_full_trade(journal, trade_id="T1", symbol="BTC/USD"):
    """Signal → plan → entry intent/submit/fill → stop intent/submit/live."""
    journal.record_signal(trade_id, symbol=symbol, setup="A",
                          signal_bar_ts="2026-07-10T12:00:00+00:00")
    journal.record_trade_planned(
        trade_id, symbol=symbol, setup="A",
        planned_entry=100000.0, structural_stop=95000.0,
        stop_limit_price=94525.0, tp1=107500.0, tp2=115000.0,
        approved_risk_usd=25.0,
    )
    journal.record_action_intent(
        trade_id, f"{trade_id}-entry-0", "entry",
        client_order_id="swing-BTCUSD-A-20260710T120000Z-entry-0",
        requested_qty=0.005,
    )
    journal.record_order_submitted(
        trade_id, f"{trade_id}-entry-0",
        broker_order_id="bo-entry-1", status="accepted",
    )
    journal.record_order_state(
        trade_id, f"{trade_id}-entry-0",
        broker_order_id="bo-entry-1", status="filled",
        filled_qty=0.005, avg_fill_price=100100.0,
    )
    journal.record_action_intent(
        trade_id, f"{trade_id}-stop-0", "stop",
        client_order_id="swing-BTCUSD-A-20260710T120000Z-stop-0",
        requested_qty=0.005, stop_price=95000.0, limit_price=94525.0,
    )
    journal.record_order_submitted(
        trade_id, f"{trade_id}-stop-0",
        broker_order_id="bo-stop-1", status="accepted",
    )
    journal.record_order_state(
        trade_id, f"{trade_id}-stop-0",
        broker_order_id="bo-stop-1", status="new", filled_qty=0.0,
    )
    journal.record_state_transition(trade_id, "PROTECTED", "stop verified")


def test_trade_materialization(tmp_path):
    journal = Journal(tmp_path / "j")
    _seed_full_trade(journal)
    trades = journal.trades()
    assert set(trades) == {"T1"}
    view = trades["T1"]
    assert view.symbol == "BTC/USD"
    assert view.setup == "A"
    assert view.state == "PROTECTED"
    assert not view.is_terminal()
    assert view.entry_filled_qty() == pytest.approx(0.005)
    assert view.expected_position_qty() == pytest.approx(0.005)
    assert view.unresolved_intents() == []


def test_exit_reduces_expected_position(tmp_path):
    journal = Journal(tmp_path / "j")
    _seed_full_trade(journal)
    journal.record_exit("T1", qty=0.0025, price=107500.0, reason="TP1")
    view = journal.trades()["T1"]
    assert view.expected_position_qty() == pytest.approx(0.0025)


def test_trade_closed_is_terminal(tmp_path):
    journal = Journal(tmp_path / "j")
    _seed_full_trade(journal)
    journal.record_exit("T1", qty=0.005, price=95000.0, reason="STOP")
    journal.record_trade_closed("T1", realized_r=-1.0, reason="stopped out")
    view = journal.trades()["T1"]
    assert view.is_terminal()
    assert view.realized_r == -1.0
    assert journal.open_trades() == {}


def test_unresolved_intent_detected(tmp_path):
    journal = Journal(tmp_path / "j")
    journal.record_action_intent("T2", "T2-entry-0", "entry",
                                 client_order_id="swing-ETHUSD-A-x-entry-0",
                                 requested_qty=0.1)
    view = journal.trades()["T2"]
    assert view.unresolved_intents() == ["T2-entry-0"]


def test_recovery_required_flag(tmp_path):
    journal = Journal(tmp_path / "j")
    _seed_full_trade(journal)
    journal.record_recovery_required("T1", "cancel state unknown")
    view = journal.trades()["T1"]
    assert view.recovery_required is True
    assert view.state == "RECOVERY_REQUIRED"


def test_find_action_by_client_order_id(tmp_path):
    journal = Journal(tmp_path / "j")
    _seed_full_trade(journal)
    hit = journal.find_action_by_client_order_id(
        "swing-BTCUSD-A-20260710T120000Z-entry-0")
    assert hit is not None
    trade_id, action_id, action = hit
    assert trade_id == "T1"
    assert action_id == "T1-entry-0"
    assert action["submitted"]["broker_order_id"] == "bo-entry-1"
    assert journal.find_action_by_client_order_id("nope") is None
    assert journal.find_action_by_client_order_id("") is None


def test_snapshot_written_and_compact(tmp_path):
    journal = Journal(tmp_path / "j")
    _seed_full_trade(journal)
    snapshot = json.loads(journal.snapshot_path.read_text())
    assert snapshot["entry_freeze"]["frozen"] is False
    assert "T1" in snapshot["open_trades"]
    assert snapshot["open_trades"]["T1"]["state"] == "PROTECTED"
    assert snapshot["open_trades"]["T1"]["expected_position_qty"] == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# git-backed journal (Option B durability)
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def _make_state_remote(tmp_path):
    """Bare remote + seeded initial commit, like the private state repo."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main",
                    str(remote)], capture_output=True, text=True, check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)],
                   capture_output=True, text=True, check=True)
    _git(seed, "config", "user.email", "test@test")
    _git(seed, "config", "user.name", "test")
    (seed / "README.md").write_text("state\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", "-u", "origin", "HEAD")
    return remote


def _clone(remote, dest):
    subprocess.run(["git", "clone", str(remote), str(dest)],
                   capture_output=True, text=True, check=True)
    _git(dest, "config", "user.email", "test@test")
    _git(dest, "config", "user.name", "test")
    return dest


def test_git_journal_requires_a_clone(tmp_path):
    with pytest.raises(ValueError):
        GitJournal(tmp_path / "not-a-repo")


def test_git_journal_append_pushes_to_remote(tmp_path):
    remote = _make_state_remote(tmp_path)
    journal = GitJournal(_clone(remote, tmp_path / "c1"))
    assert journal.append("SIGNAL", "T1", {"symbol": "BTC/USD"}) is True

    # A fresh clone must see the event — persistence means PUSHED.
    reader = GitJournal(_clone(remote, tmp_path / "c2"))
    events = reader.events()
    assert len(events) == 1
    assert events[0]["trade_id"] == "T1"


def test_git_journal_sync_pulls_remote_events(tmp_path):
    remote = _make_state_remote(tmp_path)
    writer = GitJournal(_clone(remote, tmp_path / "c1"))
    reader = GitJournal(_clone(remote, tmp_path / "c2"))
    writer.append("SIGNAL", "T1", {"symbol": "BTC/USD"})
    assert reader.events() == []
    assert reader.sync() is True
    assert len(reader.events()) == 1


def test_git_journal_concurrent_appends_both_survive(tmp_path):
    # Two writers race: the loser must rebase (union-merging the
    # append-only event file) and still persist. No event may be lost.
    remote = _make_state_remote(tmp_path)
    j1 = GitJournal(_clone(remote, tmp_path / "c1"))
    j2 = GitJournal(_clone(remote, tmp_path / "c2"))
    assert j1.append("SIGNAL", "T1", {"symbol": "BTC/USD"}) is True
    assert j2.append("SIGNAL", "T2", {"symbol": "ETH/USD"}) is True

    reader = GitJournal(_clone(remote, tmp_path / "c3"))
    trade_ids = {e["trade_id"] for e in reader.events()}
    assert trade_ids == {"T1", "T2"}


def test_git_journal_push_failure_is_not_persisted(tmp_path):
    remote = _make_state_remote(tmp_path)
    journal = GitJournal(_clone(remote, tmp_path / "c1"), push_retries=2)
    # Break the remote — every push must now fail, and append must
    # report the event as NOT persisted (fail closed).
    _git(journal.root, "remote", "set-url", "origin",
         str(tmp_path / "gone.git"))
    assert journal.append("SIGNAL", "T1", {"symbol": "BTC/USD"}) is False


def test_git_journal_sync_failure_fails_closed(tmp_path):
    remote = _make_state_remote(tmp_path)
    journal = GitJournal(_clone(remote, tmp_path / "c1"))
    _git(journal.root, "remote", "set-url", "origin",
         str(tmp_path / "gone.git"))
    assert journal.sync() is False


# ---------------------------------------------------------------------------
# env factory
# ---------------------------------------------------------------------------

def test_journal_from_env_prefers_state_repo(tmp_path, monkeypatch):
    remote = _make_state_remote(tmp_path)
    clone = _clone(remote, tmp_path / "c1")
    monkeypatch.setenv("STATE_REPO_DIR", str(clone))
    monkeypatch.delenv("JOURNAL_DIR", raising=False)
    assert isinstance(journal_from_env(), GitJournal)


def test_journal_from_env_file_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("STATE_REPO_DIR", raising=False)
    monkeypatch.setenv("JOURNAL_DIR", str(tmp_path / "j"))
    journal = journal_from_env()
    assert isinstance(journal, Journal)
    assert not isinstance(journal, GitJournal)


def test_journal_from_env_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("STATE_REPO_DIR", raising=False)
    monkeypatch.delenv("JOURNAL_DIR", raising=False)
    assert journal_from_env() is None
