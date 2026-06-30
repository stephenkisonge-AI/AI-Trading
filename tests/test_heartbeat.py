"""Tests for src/heartbeat.py — the external dead-man's-switch ping.

We never hit healthchecks.io in CI; requests.get is monkeypatched. The
contract under test: correct URL is chosen (success vs /fail), unset env
no-ops, and no failure mode ever raises into the caller.
"""
import requests

from src import heartbeat


class _FakeResp:
    def __init__(self, ok=True, status_code=200, text="OK"):
        self.ok = ok
        self.status_code = status_code
        self.text = text


def test_no_url_configured_returns_false_and_does_not_call(monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_PING_URL", raising=False)

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("requests.get called despite unset URL")

    monkeypatch.setattr(requests, "get", _boom)
    assert heartbeat.ping_heartbeat() is False


def test_blank_url_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PING_URL", "   ")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())
    assert heartbeat.ping_heartbeat() is False


def _capturing_get(captured):
    def _get(url, **k):
        captured["url"] = url
        return _FakeResp()
    return _get


def test_success_pings_base_url(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc-ping.com/abc")
    captured = {}
    monkeypatch.setattr(requests, "get", _capturing_get(captured))
    assert heartbeat.ping_heartbeat() is True
    assert captured["url"] == "https://hc-ping.com/abc"


def test_fail_appends_fail_endpoint(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc-ping.com/abc/")
    captured = {}
    monkeypatch.setattr(requests, "get", _capturing_get(captured))
    assert heartbeat.ping_heartbeat(fail=True) is True
    # Trailing slash on the base must not produce a double slash.
    assert captured["url"] == "https://hc-ping.com/abc/fail"


def test_non_2xx_returns_false(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc-ping.com/abc")
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: _FakeResp(ok=False, status_code=404, text="not found"),
    )
    assert heartbeat.ping_heartbeat() is False


def test_network_error_is_swallowed(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc-ping.com/abc")

    def _raise(*a, **k):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", _raise)
    # Must return False, not propagate — a dead watchdog can't kill the scan.
    assert heartbeat.ping_heartbeat() is False
