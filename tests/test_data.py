"""Tests for src/data.py env validation and input handling. No live Alpaca calls."""
import pytest

from src.data import (
    _assert_paper_mode,
    _require_env,
    get_bars,
    get_client,
)


def test_paper_mode_passes_when_set_to_True(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "True")
    _assert_paper_mode()  # must not raise


@pytest.mark.parametrize("bad_value", ["true", "TRUE", "1", "yes", "", "False"])
def test_paper_mode_rejects_anything_other_than_True(monkeypatch, bad_value):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", bad_value)
    with pytest.raises(RuntimeError, match="ALPACA_PAPER_TRADE"):
        _assert_paper_mode()


def test_paper_mode_rejects_when_unset(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER_TRADE", raising=False)
    with pytest.raises(RuntimeError, match="ALPACA_PAPER_TRADE"):
        _assert_paper_mode()


def test_require_env_raises_when_missing(monkeypatch):
    monkeypatch.delenv("SOME_NONEXISTENT_VAR", raising=False)
    with pytest.raises(RuntimeError, match="SOME_NONEXISTENT_VAR"):
        _require_env("SOME_NONEXISTENT_VAR")


def test_require_env_raises_when_empty(monkeypatch):
    monkeypatch.setenv("SOME_EMPTY_VAR", "")
    with pytest.raises(RuntimeError, match="SOME_EMPTY_VAR"):
        _require_env("SOME_EMPTY_VAR")


def test_get_client_raises_when_not_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "False")
    monkeypatch.setenv("ALPACA_API_KEY", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    with pytest.raises(RuntimeError, match="ALPACA_PAPER_TRADE"):
        get_client()


def test_get_bars_rejects_unknown_timeframe():
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        get_bars("BTC/USD", "30Min")
