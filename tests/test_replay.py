"""Phase 6 replay core — scan slicing, variant derivation, simulator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import src.replay as replay
from src.replay import (
    FRAME_LEN,
    Signal,
    evaluate_scan,
    find_level_cross_events,
    frames_at,
    scan_times,
    simulate_stop_limit,
    simulate_trade,
    summarize_trades,
)

UTC = timezone.utc


def _utc(*args):
    return datetime(*args, tzinfo=UTC)


def _bars(start: datetime, step: timedelta, rows: list[dict]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([start + i * step for i in range(len(rows))],
                           tz="UTC")
    return pd.DataFrame(rows, index=idx)


def _flat_rows(n: int, price: float, volume: float = 10.0) -> list[dict]:
    return [{"open": price, "high": price, "low": price, "close": price,
             "volume": volume} for _ in range(n)]


def _ohlc(o, h, l, c, v=10.0) -> dict:
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


# =========================================================================
# scan_times / frames_at
# =========================================================================

class TestScanGrid:
    def test_boundaries_are_4h_aligned(self):
        times = scan_times(_utc(2024, 1, 1, 3), _utc(2024, 1, 2, 0))
        assert times[0] == _utc(2024, 1, 1, 4)
        assert times[-1] == _utc(2024, 1, 2, 0)
        assert all(t.hour % 4 == 0 and t.minute == 0 for t in times)

    def test_start_on_boundary_included(self):
        times = scan_times(_utc(2024, 1, 1, 8), _utc(2024, 1, 1, 8))
        assert times == [_utc(2024, 1, 1, 8)]

    def test_frames_exclude_in_progress_bars(self):
        # 260 daily bars ending the day before the scan, 300 4H and 1H
        # bars ending exactly at the scan boundary.
        t = _utc(2024, 9, 1, 12)
        daily = _bars(t - timedelta(days=260), timedelta(days=1),
                      _flat_rows(260, 100.0))
        h4 = _bars(t - timedelta(hours=4 * 300), timedelta(hours=4),
                   _flat_rows(300, 100.0))
        h1 = _bars(t - timedelta(hours=300), timedelta(hours=1),
                   _flat_rows(300, 100.0))
        sliced = frames_at(daily, h4, h1, t)
        assert sliced is not None
        d, h4_s, h1_s = sliced
        # Daily: strictly before the scan's UTC day.
        assert d.index[-1] < t.replace(hour=0)
        # 4H: last bar STARTS 4h before the boundary (ends at it).
        assert h4_s.index[-1] == t - timedelta(hours=4)
        # 1H: last bar ends at the boundary.
        assert h1_s.index[-1] == t - timedelta(hours=1)
        assert len(h4_s) == FRAME_LEN and len(h1_s) == FRAME_LEN
        # Indicators attached by the slicer.
        assert "ema20" in h4_s.columns and "atr14" in h1_s.columns

    def test_insufficient_history_returns_none(self):
        t = _utc(2024, 9, 1, 12)
        daily = _bars(t - timedelta(days=100), timedelta(days=1),
                      _flat_rows(100, 100.0))
        h4 = _bars(t - timedelta(hours=4 * 300), timedelta(hours=4),
                   _flat_rows(300, 100.0))
        h1 = _bars(t - timedelta(hours=300), timedelta(hours=1),
                   _flat_rows(300, 100.0))
        assert frames_at(daily, h4, h1, t) is None


# =========================================================================
# Variant derivation (production evaluator stubbed — its own behavior is
# covered by the strategy tests; here we test the exact/window split)
# =========================================================================

def _canned_result(qualified: bool, reclaim_pass: bool,
                   others_pass: bool, hit: str | None) -> dict:
    conditions = [
        {"name": "daily_regime_bullish_or_improving", "passed": others_pass},
        {"name": "h1_green_close_reclaims_ema20", "passed": reclaim_pass},
        {"name": "stop_below_swing_low_within_atr_cap", "passed": others_pass},
    ]
    return {"qualified": qualified, "conditions": conditions,
            "telemetry": {"reclaim_window_hit": hit,
                          "reclaim_exact": reclaim_pass,
                          "pullback_close_rule": True,
                          "pullback_range_touch": False}}


class TestVariantDerivation:
    @pytest.fixture()
    def frames(self):
        t = _utc(2024, 9, 1, 12)
        daily = _bars(t - timedelta(days=260), timedelta(days=1),
                      _flat_rows(260, 100.0))
        # A clear swing low at 95 in the middle of the 4H frame.
        rows = _flat_rows(40, 100.0)
        rows[25] = _ohlc(100, 100, 95, 100)
        h4 = _bars(t - timedelta(hours=4 * 40), timedelta(hours=4), rows)
        h4 = h4.assign(atr14=2.0)  # evaluate_scan reads atr14 off the frame
        h1 = _bars(t - timedelta(hours=60), timedelta(hours=1),
                   _flat_rows(60, 100.0))
        return daily, h4, h1, t

    def test_exact_implies_window_signal_params(self, frames, monkeypatch):
        daily, h4, h1, t = frames
        monkeypatch.setattr(
            replay, "evaluate_setup_a",
            lambda *a, **k: _canned_result(True, True, True,
                                           "2024-09-01 09:00:00+00:00"))
        sig = evaluate_scan(daily, h4, h1, "BTC/USD", t)
        assert sig.exact and sig.window
        assert sig.entry == 100.0
        assert sig.stop == 95.0
        assert sig.atr == 2.0

    def test_window_only_when_reclaim_missed_between_scans(
            self, frames, monkeypatch):
        daily, h4, h1, t = frames
        monkeypatch.setattr(
            replay, "evaluate_setup_a",
            lambda *a, **k: _canned_result(False, False, True,
                                           "2024-09-01 10:00:00+00:00"))
        sig = evaluate_scan(daily, h4, h1, "BTC/USD", t)
        assert not sig.exact
        assert sig.window
        assert sig.stop == 95.0

    def test_no_window_when_other_condition_fails(self, frames, monkeypatch):
        daily, h4, h1, t = frames
        monkeypatch.setattr(
            replay, "evaluate_setup_a",
            lambda *a, **k: _canned_result(False, False, False,
                                           "2024-09-01 10:00:00+00:00"))
        sig = evaluate_scan(daily, h4, h1, "BTC/USD", t)
        assert not sig.exact and not sig.window
        assert sig.entry is None

    def test_no_window_without_reclaim_hit(self, frames, monkeypatch):
        daily, h4, h1, t = frames
        monkeypatch.setattr(
            replay, "evaluate_setup_a",
            lambda *a, **k: _canned_result(False, False, True, None))
        sig = evaluate_scan(daily, h4, h1, "BTC/USD", t)
        assert not sig.exact and not sig.window


# =========================================================================
# Trade simulator
# =========================================================================

def _sig(entry=100.0, stop=95.0, ts=None) -> Signal:
    return Signal(symbol="BTC/USD", scan_ts=ts or _utc(2024, 9, 1, 12),
                  regime="BULLISH", exact=True, window=True,
                  entry=entry, stop=stop, atr=2.0)


def _empty_daily() -> pd.DataFrame:
    # Too shallow for a regime read -> regime exits never fire.
    return _bars(_utc(2024, 8, 1), timedelta(days=1), _flat_rows(10, 100.0))


def _empty_h4() -> pd.DataFrame:
    return _bars(_utc(2024, 8, 30), timedelta(hours=4), _flat_rows(5, 100.0))


class TestSimulator:
    def test_stop_hit_fills_at_stop(self):
        sig = _sig()
        h1 = _bars(sig.scan_ts, timedelta(hours=1), [
            _ohlc(100, 101, 99, 100),
            _ohlc(100, 100, 94, 94.5),   # touches 95 stop
        ])
        t = simulate_trade(sig, "exact", _empty_daily(), _empty_h4(), h1)
        assert t.exit_reasons() == ["stop"]
        assert t.tranches[0][:2] == (1.0, 95.0)
        assert t.r_gross() == pytest.approx(-1.0)
        # Net R pays 0.25% per side on entry + exit notional.
        assert t.r_net() == pytest.approx(
            -1.0 - 0.0025 * (100.0 + 95.0) / 5.0)
        assert len(t.stop_hits) == 1
        assert t.stop_hits[0]["gap_open"] is False
        assert not t.truncated

    def test_gap_open_fills_at_open(self):
        sig = _sig()
        h1 = _bars(sig.scan_ts, timedelta(hours=1), [
            _ohlc(100, 101, 99, 100),
            _ohlc(90, 91, 89, 90),       # opens through the stop
        ])
        t = simulate_trade(sig, "exact", _empty_daily(), _empty_h4(), h1)
        assert t.exit_reasons() == ["stop_gap_open"]
        assert t.r_gross() == pytest.approx(-2.0)
        assert t.stop_hits[0]["gap_open"] is True

    def test_tp1_moves_stop_to_breakeven(self):
        sig = _sig()   # tp1 = 107.5
        h1 = _bars(sig.scan_ts, timedelta(hours=1), [
            _ohlc(100, 108, 100, 107.6),   # close through TP1
            _ohlc(107, 107, 99, 99.5),     # falls to breakeven stop
        ])
        t = simulate_trade(sig, "exact", _empty_daily(), _empty_h4(), h1)
        assert t.exit_reasons() == ["tp1", "stop"]
        fracs = [f for f, _, _, _ in t.tranches]
        prices = [p for _, p, _, _ in t.tranches]
        assert fracs == [0.5, 0.5]
        assert prices == [107.5, 100.0]    # breakeven, not 95
        assert t.r_gross() == pytest.approx(0.5 * 1.5)
        assert ("breakeven", str(h1.index[0])) in t.events

    def test_tp_needs_close_not_wick(self):
        sig = _sig()
        h1 = _bars(sig.scan_ts, timedelta(hours=1), [
            _ohlc(100, 110, 100, 105),     # wick through TP1, close below
            _ohlc(105, 105, 94, 94),       # stop
        ])
        t = simulate_trade(sig, "exact", _empty_daily(), _empty_h4(), h1)
        assert t.exit_reasons() == ["stop"]

    def test_time_stop_after_10_days_without_tp1(self):
        sig = _sig()
        n = 24 * 11
        h1 = _bars(sig.scan_ts, timedelta(hours=1), _flat_rows(n, 100.0))
        t = simulate_trade(sig, "exact", _empty_daily(), _empty_h4(), h1)
        assert t.exit_reasons() == ["time_stop"]
        assert t.exit_ts < sig.scan_ts + timedelta(days=10, hours=2)

    def test_regime_exit_on_bearish_day(self):
        sig = _sig(ts=_utc(2024, 9, 1, 12))
        # 210 declining daily closes -> BEARISH regime.
        closes = [200.0 - 0.5 * i for i in range(210)]
        rows = [_ohlc(c, c, c, c) for c in closes]
        daily = _bars(_utc(2024, 2, 4), timedelta(days=1), rows)
        h1 = _bars(sig.scan_ts, timedelta(hours=1),
                   _flat_rows(30, 100.0))   # crosses 2024-09-02 00:00
        t = simulate_trade(sig, "exact", daily, _empty_h4(), h1)
        assert t.exit_reasons() == ["regime_exit"]
        assert t.exit_ts == _utc(2024, 9, 2, 0)

    def test_open_at_data_end_is_truncated(self):
        sig = _sig()
        h1 = _bars(sig.scan_ts, timedelta(hours=1), _flat_rows(5, 100.0))
        t = simulate_trade(sig, "exact", _empty_daily(), _empty_h4(), h1)
        assert t.truncated
        assert t.exit_reasons() == ["open_at_data_end"]

    def test_full_ladder_tp1_tp2_runner(self):
        sig = _sig()   # tp1=107.5, tp2=115
        rows = [
            _ohlc(100, 108, 100, 107.6),    # TP1
            _ohlc(107.6, 116, 107, 115.5),  # TP2 -> runner
        ]
        rows += _flat_rows(10, 115.5)
        h1 = _bars(sig.scan_ts, timedelta(hours=1), rows)
        # 4H frame long enough for _h4_context (>= 21 bars up to each
        # boundary), close >= ema20 (no runner exit) with real range so
        # ATR > 0 and the trail sits safely below price.
        h4 = _bars(sig.scan_ts - timedelta(hours=4 * 40), timedelta(hours=4),
                   [_ohlc(115.5, 117.5, 113.5, 115.5) for _ in range(45)])
        t = simulate_trade(sig, "exact", _empty_daily(), h4, h1)
        reasons = t.exit_reasons()
        assert reasons[:2] == ["tp1", "tp2"]
        assert t.tranches[0][0] == 0.5 and t.tranches[1][0] == 0.25
        # Remaining 25% still open at data end.
        assert t.truncated and t.tranches[-1][0] == pytest.approx(0.25)
        # Total booked fraction is exactly the whole position.
        assert sum(f for f, _, _, _ in t.tranches) == pytest.approx(1.0)


class TestStopLimitFill:
    HOUR = _utc(2024, 9, 1, 12)

    def _m1(self, rows):
        return _bars(self.HOUR, timedelta(minutes=1), rows)

    def test_sweep_stays_inside_band_fills_near_close(self):
        # Stop 100, offset 1% -> limit 99. Low 99.5 stays above limit.
        m1 = self._m1([_ohlc(101, 101, 99.5, 99.8)])
        r = simulate_stop_limit(m1, self.HOUR, 100.0, 0.01, 30)
        assert r["filled_via_limit"]
        assert r["exit_price"] == 99.8          # clamp(close, 99, 100)
        assert r["slippage_pct"] == pytest.approx(0.002)

    def test_fill_never_better_than_stop(self):
        # Close recovered above the stop; fill is capped at the stop.
        m1 = self._m1([_ohlc(101, 102, 99.5, 101.5)])
        r = simulate_stop_limit(m1, self.HOUR, 100.0, 0.01, 30)
        assert r["exit_price"] == 100.0

    def test_pierce_and_recover_fills_at_limit(self):
        # Low breaks the band, close recovers to >= limit.
        m1 = self._m1([_ohlc(101, 101, 98.0, 99.5)])
        r = simulate_stop_limit(m1, self.HOUR, 100.0, 0.01, 30)
        assert r["filled_via_limit"]
        assert r["exit_price"] == 99.0

    def test_gap_through_then_recovery_fills_at_limit_later(self):
        m1 = self._m1([
            _ohlc(101, 101, 97.0, 97.5),   # through the whole band
            _ohlc(97.5, 98.0, 97.0, 97.8),
            _ohlc(97.8, 99.2, 97.8, 99.1), # high touches limit 99
        ])
        r = simulate_stop_limit(m1, self.HOUR, 100.0, 0.01, 30)
        assert r["filled_via_limit"]
        assert r["exit_price"] == 99.0
        assert r["exit_ts"] == m1.index[2]

    def test_no_recovery_watchdog_closes_at_market(self):
        rows = [_ohlc(101, 101, 97.0, 97.5)]
        rows += [_ohlc(97.5 - 0.01 * i, 97.6 - 0.01 * i,
                       97.3 - 0.01 * i, 97.4 - 0.01 * i, 1.0)
                 for i in range(40)]
        m1 = self._m1(rows)
        r = simulate_stop_limit(m1, self.HOUR, 100.0, 0.01, 15)
        assert not r["filled_via_limit"]
        # Market close at the open of the first bar >= trigger + 15 min.
        assert r["exit_ts"] == m1.index[15]
        assert r["exit_price"] == float(m1.iloc[15]["open"])
        assert r["slippage_pct"] > 0.02

    def test_wider_offset_converts_watchdog_to_limit_fill(self):
        rows = [_ohlc(101, 101, 97.0, 97.5)]
        rows += [_ohlc(97.5, 97.6, 97.3, 97.4, 1.0) for _ in range(40)]
        m1 = self._m1(rows)
        narrow = simulate_stop_limit(m1, self.HOUR, 100.0, 0.01, 15)
        wide = simulate_stop_limit(m1, self.HOUR, 100.0, 0.03, 15)
        assert not narrow["filled_via_limit"]
        assert wide["filled_via_limit"]        # limit 97 is inside the sweep
        assert wide["slippage_pct"] == pytest.approx(0.03)

    def test_no_trigger_returns_none(self):
        m1 = self._m1([_ohlc(101, 101, 100.5, 100.7)])
        assert simulate_stop_limit(m1, self.HOUR, 100.0, 0.01, 30) is None


class TestLevelCrossEvents:
    def test_finds_first_breach_after_clear_period(self):
        # A structural low inside the 80h lookback sets the level,
        # price holds clear of it, then one bar sweeps through.
        rows = _flat_rows(200, 100.0)
        rows[100] = _ohlc(100, 100, 90.0, 100)    # the structural low
        rows[150] = _ohlc(100, 100, 89.5, 95.0)   # the breach
        h1 = _bars(_utc(2024, 9, 1), timedelta(hours=1), rows)
        events = find_level_cross_events(h1)
        assert len(events) == 1
        assert events[0]["bar_ts"] == h1.index[150]
        assert events[0]["stop"] == 90.0

    def test_no_events_on_flat_data(self):
        h1 = _bars(_utc(2024, 9, 1), timedelta(hours=1),
                   _flat_rows(200, 100.0))
        # A perfectly flat series never has a 'clear' period above the
        # level (prior lows equal it), so no breach events.
        assert find_level_cross_events(h1) == []

    def test_dedupe_window_suppresses_clustered_breaches(self):
        rows = _flat_rows(400, 100.0)
        rows[100] = _ohlc(100, 100, 90.0, 100)
        rows[150] = _ohlc(100, 100, 89.5, 95.0)
        # Another qualifying breach 30h later (24h clear satisfied) but
        # inside the 72h dedupe window -> suppressed.
        rows[180] = _ohlc(100, 100, 89.0, 95.0)
        h1 = _bars(_utc(2024, 9, 1), timedelta(hours=1), rows)
        events = find_level_cross_events(h1, dedupe_hours=72)
        assert len(events) == 1
        # Without dedupe both breaches qualify.
        assert len(find_level_cross_events(h1, dedupe_hours=1)) == 2


class TestSummary:
    def test_empty(self):
        assert summarize_trades([]) == {"n": 0}

    def test_aggregates(self):
        sig = _sig()
        h1_loss = _bars(sig.scan_ts, timedelta(hours=1), [
            _ohlc(100, 100, 94, 94),
        ])
        h1_win = _bars(sig.scan_ts, timedelta(hours=1), [
            _ohlc(100, 108, 100, 107.6),
            _ohlc(107, 107, 99, 99.5),
        ])
        trades = [
            simulate_trade(sig, "exact", _empty_daily(), _empty_h4(), h1_loss),
            simulate_trade(sig, "exact", _empty_daily(), _empty_h4(), h1_win),
        ]
        s = summarize_trades(trades)
        assert s["n"] == 2
        assert s["win_rate"] == 0.5
        assert s["exit_mix"] == {"stop": 2, "tp1": 1}
