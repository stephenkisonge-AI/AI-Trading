"""Self-heal heartbeat — an external dead-man's switch.

On each successful day-watcher scan we ping healthchecks.io. The check is
configured (cron mode, America/New_York tz) to EXPECT a ping every 5 min
during market hours. When the watcher stops running — a GitHub runner/billing
outage, a code crash, a data blackout — the pings stop and healthchecks.io
alerts via its own Telegram/email integration.

This is the one liveness signal that survives a *total* GitHub Actions outage:
because the watchdog lives outside GitHub, it still fires when GitHub itself
refuses to start the job (the failure mode that produced the "All jobs have
failed" email this was built to replace).

Best-effort by design: a ping must NEVER affect the trading scan, so every
error is swallowed and logged to stderr. If HEALTHCHECK_PING_URL is unset the
functions no-op, so local runs and forks work without configuration.
"""
from __future__ import annotations

import os
import sys

import requests

_ENV_VAR = "HEALTHCHECK_PING_URL"
_PING_TIMEOUT_SEC = 10


def ping_heartbeat(*, fail: bool = False) -> bool:
    """Ping the healthcheck URL. Returns True on a 2xx response, False on any
    error or when the URL isn't configured. Never raises.

    fail=True hits the `/fail` endpoint so a watcher crash flips the check to
    "down" immediately, instead of waiting for the silence window to expire.
    """
    base = os.environ.get(_ENV_VAR, "").strip()
    if not base:
        # Unconfigured is not an error — local/dev runs simply skip the ping.
        return False
    url = base.rstrip("/") + "/fail" if fail else base
    try:
        resp = requests.get(url, timeout=_PING_TIMEOUT_SEC)
        if not resp.ok:
            print(
                f"[heartbeat] healthcheck returned {resp.status_code}: "
                f"{resp.text[:200]}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:
        print(f"[heartbeat] ping failed: {exc}", file=sys.stderr)
        return False
