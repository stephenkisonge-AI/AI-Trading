"""Daily watcher entry point. Runs once per day on GitHub Actions at
03:02 UTC (06:02 EAT), evaluates Setup A and Setup B on closed bars for
each symbol, and pings Telegram only when something qualifies.

Never places orders. Trade execution always goes through Claude Code +
Alpaca MCP with manual confirmation.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv

from src.data import _assert_paper_mode, get_bars, get_positions
from src.indicators import add_indicators
from src.notifier import format_setup_alert, send_alert
from src.strategy import classify_regime, evaluate_setup_a, evaluate_setup_b

SYMBOLS = ["BTC/USD", "ETH/USD"]


def _alpaca_position_symbol(symbol: str) -> str:
    """Alpaca returns crypto position symbols without the slash."""
    return symbol.replace("/", "")


def _has_open_position(symbol: str, positions) -> bool:
    target = _alpaca_position_symbol(symbol)
    return any(getattr(p, "symbol", None) == target for p in positions)


def _drop_in_progress_candle(df):
    """Drop the most recent bar — at scan time it's still forming.

    The strategy doc is explicit: evaluate on closed candles. The watcher
    runs at 03:02 UTC; the daily candle at 00:00 UTC is 3 hours into its
    24-hour window, the current 4H candle is 3 hours into a 4-hour window,
    and the current 1H candle is 2 minutes old. None of them are closed.
    """
    if len(df) > 0:
        return df.iloc[:-1].copy()
    return df


def _scan_symbol(symbol: str, positions) -> dict:
    """Pull data and run both setup evaluators for one symbol.

    Returns: {"symbol": ..., "regime": ..., "setup_a": <result>, "setup_b": <result>}
    Raises on any error — caller wraps in try/except to keep scanning others.
    """
    daily_raw = get_bars(symbol, "1Day", limit=250)
    h4_raw = get_bars(symbol, "4Hour", limit=250)
    h1_raw = get_bars(symbol, "1Hour", limit=250)

    daily = _drop_in_progress_candle(daily_raw)
    h4 = add_indicators(_drop_in_progress_candle(h4_raw))
    h1 = add_indicators(_drop_in_progress_candle(h1_raw))

    regime = classify_regime(daily)
    has_position = _has_open_position(symbol, positions)

    setup_a = evaluate_setup_a(daily, h4, h1, symbol, has_position)
    setup_b = evaluate_setup_b(daily, h4, h1, symbol, has_position)

    return {
        "symbol": symbol,
        "regime": regime,
        "has_position": has_position,
        "setup_a": setup_a,
        "setup_b": setup_b,
    }


def main() -> int:
    load_dotenv()
    _assert_paper_mode()

    run_started = datetime.now(timezone.utc)
    print(f"[watcher] started at {run_started.isoformat()}")

    try:
        positions = get_positions()
        print(f"[watcher] open positions: {len(positions)}")
    except Exception as exc:
        print(f"[watcher] FAILED to fetch positions: {exc}", file=sys.stderr)
        send_alert(
            f"⚠️ Watcher errors at {run_started.strftime('%H:%M UTC')} — "
            f"could not fetch positions: {exc}"
        )
        return 0

    errors: list[str] = []
    for symbol in SYMBOLS:
        try:
            result = _scan_symbol(symbol, positions)
            summary = (
                f"[watcher] {symbol}: regime={result['regime']} "
                f"has_position={result['has_position']} "
                f"setup_a_qualified={result['setup_a']['qualified']} "
                f"setup_b_qualified={result['setup_b']['qualified']}"
            )
            print(summary)

            for setup_result in (result["setup_a"], result["setup_b"]):
                if setup_result["qualified"]:
                    message = format_setup_alert(setup_result, result["regime"])
                    sent = send_alert(message)
                    print(
                        f"[watcher] sent alert for {symbol} Setup {setup_result['setup']}: {sent}"
                    )
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"[watcher] ERROR scanning {symbol}: {exc}\n{tb}", file=sys.stderr)
            errors.append(f"{symbol}: {exc}")

    if errors:
        send_alert(
            f"⚠️ Watcher errors at {run_started.strftime('%H:%M UTC')}\n"
            + "\n".join(f"- {e}" for e in errors)
        )

    print(f"[watcher] done at {datetime.now(timezone.utc).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
