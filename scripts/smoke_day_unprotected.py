"""Live smoke test (Alpaca PAPER) for the day-trade close path.

Unit tests prove the logic against fakes; this proves it against the
real broker: real OCO legs and their statuses, real cancel / share-hold
release timing, real close_position behaviour.

It trades a symbol OUTSIDE the day universe (default F, 2 shares, about
$25) so the live day-watcher neither counts nor manages the test
position. Needs the regular session to be open. Always cleans up.

Checks, in order:
  1. A position resting behind two OCO brackets (the production shape)
     is recognised as protected and left alone by manage_position.
  2. A close that is rejected once is retried within the same call and
     the position ends flat.
  3. The MU 2026-07-15 sequence: every close attempt fails after the
     exits were cancelled -> the error says NO STOP and the position
     really is naked at the broker -> the next manage_position pass
     flattens it.

Usage:  python scripts/smoke_day_unprotected.py [--symbol F] [--qty 2]
Exit code 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class _CloseFails:
    """Real client, except close_position raises the first `times` calls."""

    def __init__(self, client, times: int):
        self._client, self._left = client, times

    def __getattr__(self, name):
        return getattr(self._client, name)

    def close_position(self, symbol):
        if self._left > 0:
            self._left -= 1
            raise RuntimeError("smoke test: injected close_position rejection")
        return self._client.close_position(symbol)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbol", default="F")
    ap.add_argument("--qty", type=int, default=2)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = ap.parse_args(argv)
    _load_env(args.env_file)

    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest, MarketOrderRequest, StopLossRequest,
        TakeProfitRequest,
    )

    from src import day_trader as dt
    from src.data import get_client          # refuses anything but paper
    from src.universe import UNIVERSE

    symbol, qty = args.symbol.upper(), args.qty
    if symbol in UNIVERSE:
        print(f"REFUSED: {symbol} is in the day universe — the live watcher "
              "would count and manage the test position.")
        return 2
    if qty < 2:
        print("REFUSED: need qty >= 2 to build two OCO brackets.")
        return 2

    client = get_client()
    clock = client.get_clock()
    if not clock.is_open:
        print(f"REFUSED: market is closed (next open {clock.next_open}).")
        return 2
    now_et = datetime.now(timezone.utc).astimezone(dt.ET)
    if now_et.time() >= dt._time(15, 30):
        print("REFUSED: too close to the 3:55 PM hard close.")
        return 2

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        results.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        return bool(ok)

    def open_orders():
        return dt._open_orders_for(client, symbol)

    def wait_flat(timeout=30) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if dt._position_still_open(client, symbol) is False and not open_orders():
                return True
            time.sleep(1)
        return False

    def open_protected():
        """Market-buy `qty`, then rest two OCO brackets far from the
        market — the same request shape place_entry_bundle submits."""
        entry = client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY))
        filled = dt._wait_for_fill(client, entry.id)
        px = float(filled.filled_avg_price)
        stop_px, tp_px = round(px * 0.90, 2), round(px * 1.10, 2)
        half = qty // 2
        for leg_qty in (half, qty - half):
            client.submit_order(LimitOrderRequest(
                symbol=symbol, qty=leg_qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY, limit_price=tp_px,
                order_class=OrderClass.OCO,
                take_profit=TakeProfitRequest(limit_price=tp_px),
                stop_loss=StopLossRequest(stop_price=stop_px)))
        time.sleep(2)
        print(f"  opened {qty} {symbol} @ {px:.2f}, stops {stop_px}, targets {tp_px}")
        return client.get_open_position(symbol)

    def resting_legs():
        """Every resting order for the symbol, stop legs included.

        Alpaca's open-orders query (what open_orders() uses) returns
        only the limit leg of an OCO pair; the stop leg sits "held" and
        shows up only in the all-status listing — which is where
        _find_resting_stop looks. Live-verified 2026-11-16: two OCO
        brackets = 2 open orders but 4 resting legs.
        """
        since = datetime.now(timezone.utc) - timedelta(days=1)
        return [o for o in dt._list_orders_since(client, symbol, since)
                if str(o.status).lower().split(".")[-1]
                in dt._PROTECTIVE_STOP_STATUSES]

    def statuses():
        return sorted(f"{str(o.side).split('.')[-1].lower()} "
                      f"{str(o.order_type).split('.')[-1].lower()} "
                      f"{str(o.status).split('.')[-1].lower()}"
                      for o in resting_legs())

    try:
        if dt._position_still_open(client, symbol) is not False or open_orders():
            print(f"REFUSED: {symbol} already has a position or open orders.")
            return 2

        print("1. Protected position is recognised and left alone")
        pos = open_protected()
        now_utc = datetime.now(timezone.utc)
        print(f"  open orders: {statuses()}")
        check("stop detected on real OCO legs",
              dt._has_protective_stop(client, symbol, now_utc, "long"))
        res = dt.manage_position(client=client, position=pos,
                                 now_et=datetime.now(timezone.utc).astimezone(dt.ET),
                                 spy_5min_with_indicators=None)
        check("manage_position takes no action", res is None, f"returned {res}")
        check("position and all 4 exit legs still resting",
              dt._position_still_open(client, symbol) is True
              and len(resting_legs()) == 4, f"resting: {statuses()}")

        print("2. A rejected close is retried within the same call")
        res = dt._close_position_market(
            _CloseFails(client, 1), symbol, pos, reason="smoke: retry",
            now_utc=datetime.now(timezone.utc))
        check("close succeeded on the second attempt",
              res["error"] is None and res["close_attempts"] == 2,
              f"attempts={res['close_attempts']} error={res['error']}")
        check("exits were cancelled first", len(res["cancelled_orders"]) >= 2,
              f"{len(res['cancelled_orders'])} cancelled")
        check("position is flat, nothing resting", wait_flat())

        print("3. MU sequence: close fails after the cancel, next pass flattens")
        pos = open_protected()
        fake_355 = datetime.now(timezone.utc).astimezone(dt.ET).replace(
            hour=15, minute=56, second=0, microsecond=0)
        res = dt.manage_position(
            client=_CloseFails(client, 99), position=pos, now_et=fake_355,
            spy_5min_with_indicators=None)
        check("failed close reports NO STOP",
              bool(res) and "NO STOP" in str(res.get("error"))
              and res.get("close_attempts") == dt._CLOSE_MAX_ATTEMPTS,
              f"error={res and res.get('error')}")
        check("broker agrees: position open with no resting orders",
              dt._position_still_open(client, symbol) is True and not open_orders())
        check("no protective stop is detected",
              not dt._has_protective_stop(client, symbol,
                                          datetime.now(timezone.utc), "long"))
        res = dt.manage_position(
            client=client, position=client.get_open_position(symbol),
            now_et=datetime.now(timezone.utc).astimezone(dt.ET),
            spy_5min_with_indicators=None)
        check("next pass flattens the unprotected position",
              bool(res) and res.get("action") == "unprotected_flatten"
              and res.get("error") is None, f"returned {res}")
        check("position is flat, nothing resting", wait_flat())
    except Exception as exc:
        check("no unexpected exception", False, f"{type(exc).__name__}: {exc}")
    finally:
        # Leave nothing behind, whatever happened above.
        try:
            for o in open_orders():
                client.cancel_order_by_id(o.id)
            time.sleep(2)
            if dt._position_still_open(client, symbol) is not False:
                client.close_position(symbol)
            clean = wait_flat()
        except Exception as exc:
            clean = False
            print(f"  cleanup error: {exc}")
        check("cleanup: no position, no open orders", clean)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\nSMOKE TEST {'PASSED' if not failed else 'FAILED'}: "
          f"{len(results) - len(failed)}/{len(results)} checks passed")
    for name in failed:
        print(f"  failed: {name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
