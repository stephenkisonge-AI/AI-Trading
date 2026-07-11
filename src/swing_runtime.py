"""Shared runtime wiring for the crypto swing strand (Phase 4).

Builds the live dependencies that `swing_exits.manage_swing_trades`
and the entry path inject: journal, quote provider, per-symbol regime,
and runner context. Used by both entrypoints:

    src/watcher.py        — 4-hourly scan (entries + management)
    src/swing_manager.py  — management-only pass (15-30 min cadence,
                            Addendum E; never places entries)

Fail-closed rule: if the journal cannot be built or synced, management
still runs where possible but ANY entry path must treat the strand as
frozen. `build_synced_journal` returns (journal, error) so callers can
distinguish "ready" from "unavailable".
"""
from __future__ import annotations

import sys
from typing import Optional

import pandas as pd

from src.data import get_bars, get_latest_quote
from src.indicators import add_indicators
from src.journal import GitJournal, Journal, journal_from_env
from src.strategy import classify_regime


def build_synced_journal() -> tuple[Optional[Journal], Optional[str]]:
    """(journal, error). error is a human-readable reason when the
    journal is unavailable or its state-repo sync failed — callers must
    fail closed on entries in that case."""
    try:
        journal = journal_from_env("swing")
    except Exception as exc:
        return None, f"journal init failed: {exc}"
    if journal is None:
        return None, "no journal configured (STATE_REPO_DIR/JOURNAL_DIR unset)"
    if isinstance(journal, GitJournal) and not journal.sync():
        return None, "state-repo sync failed"
    return journal, None


def regime_for(symbol: str) -> Optional[str]:
    """Per-symbol daily regime on closed bars; None when unknowable
    (management treats unknown as 'hold, keep the stop')."""
    try:
        daily = get_bars(symbol, "1Day", limit=250)
        closed = daily.iloc[:-1] if len(daily) > 0 else daily
        regime = classify_regime(closed)
        return regime if regime in ("BULLISH", "BEARISH", "NEUTRAL") else None
    except Exception as exc:
        print(f"[swing] regime check failed for {symbol}: {exc}",
              file=sys.stderr)
        return None


def runner_ctx_for(symbol: str, view) -> Optional[dict]:
    """4H context for runner management: last closed close/ema20/atr14
    and the high-water mark since the TP2 fill. None when unknowable."""
    try:
        h4_raw = get_bars(symbol, "4Hour", limit=250)
    except Exception as exc:
        print(f"[swing] 4H bars failed for {symbol}: {exc}", file=sys.stderr)
        return None
    if len(h4_raw) < 30:
        return None
    h4 = add_indicators(h4_raw.iloc[:-1].copy())  # closed bars only
    last = h4.iloc[-1]
    close = float(last["close"])
    ema20 = float(last["ema20"]) if pd.notna(last["ema20"]) else None
    atr14 = float(last["atr14"]) if pd.notna(last["atr14"]) else None

    tp2_exits = [e for e in view.exits if str(e.get("reason", "")).upper() == "TP2"]
    hwm = None
    if tp2_exits:
        # HWM = max 4H high since the TP2 exit was recorded.
        since = None
        try:
            since = pd.Timestamp(tp2_exits[-1].get("_ts")).tz_convert("UTC")
        except (TypeError, ValueError):
            try:
                since = pd.Timestamp(tp2_exits[-1].get("_ts"), tz="UTC")
            except (TypeError, ValueError):
                since = None
        try:
            bars_since = h4[h4.index >= since] if since is not None else h4
            if not bars_since.empty:
                hwm = float(bars_since["high"].max())
        except TypeError:
            hwm = None
    return {"close": close, "ema20": ema20, "atr14": atr14, "hwm": hwm}


def quote_for(symbol: str):
    return get_latest_quote(symbol)
