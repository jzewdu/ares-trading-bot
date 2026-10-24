"""One-off: liquidate the retired carry book and open a fresh gear-shift ledger.

Run it once, before the gear-shift service starts, with the live .env loaded:

    set -a; . ~/ares_trading_bot/.env; set +a
    ARES_STATE_PATH=/var/lib/ares/ares_state.sqlite3 .venv/bin/python scripts/retire_carry.py

It pauses entries, sells every crypto position on the Alpaca paper account,
closes the old delta-neutral rows, and writes the post-liquidation equity as
the new baseline. It refuses to run twice in one sitting only insofar as a
second run finds no positions; closing rows is idempotent.
"""
from __future__ import annotations

import pathlib
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from alpaca_client import AlpacaClient
from state import connect, event, initialize, set_value

MAX_WAIT = 30


def pair(symbol: str) -> str:
    text = str(symbol)
    return text if "/" in text else f"{text[:-3]}/{text[-3:]}"


def main() -> int:
    client = AlpacaClient()
    initialize()
    set_value("paused", "1")   # stop the old loop from opening anything new
    account = client.account()
    print(f"before: equity {account.get('equity')} cash {account.get('cash')}")
    all_positions = [p for p in client.positions() if float(p.get("qty") or 0) > 0]
    positions = [p for p in all_positions if float(p.get("market_value") or 0) >= 1.0]
    for p in all_positions:
        if p not in positions:
            print(f"  skipping dust: {p['symbol']} qty {p['qty']} "
                  f"(${p.get('market_value')})")
    print(f"{len(positions)} position(s) to liquidate")
    for pos in positions:
        symbol = pair(pos["symbol"])
        qty = float(pos["qty"])
        fill = {}
        # Send the broker's own quantity string: formatting it to 8 decimals
        # can round up past the balance and Alpaca rejects that with a 403.
        sell_qty = str(pos.get("qty") or "").strip() or AlpacaClient.qty_string(qty)
        for attempt in range(3):
            try:
                order = client.market_sell(symbol, sell_qty,
                                           f"ares-retire-{int(time.time() * 1000)}-{attempt}")
                fill = client.wait_fill(order["id"], MAX_WAIT)
                break
            except requests.RequestException as exc:
                body = getattr(getattr(exc, "response", None), "text", "")
                print(f"  attempt {attempt + 1} for {symbol} failed: {exc} {body[:200]}")
                time.sleep(2)
        print(f"  sold {qty:.8g} {symbol}: {fill.get('status')} "
              f"@ {fill.get('filled_avg_price')}")
        if fill.get("status") != "filled":
            print(f"  WARNING: {symbol} did not fill; it will be reported as a stray position")
    account = client.account()
    equity = float(account.get("equity", 0) or 0)
    cash = float(account.get("cash", 0) or 0)
    with connect() as db:
        db.execute("UPDATE dn_positions SET status='closed', closed_ts=?, "
                   "exit_reason='strategy retired — gear-shift book replaced the carry desk' "
                   "WHERE status='open'",
                   (datetime.now(timezone.utc).isoformat(),))
    set_value("gear_start_equity", f"{equity:.8f}")
    set_value("gear_started", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    set_value("gear_baseline_date", datetime.now(timezone.utc).date().isoformat())
    set_value("gear_baseline_equity", f"{equity:.8f}")
    set_value("paused", "0")
    event("info", f"Carry book retired; gear-shift baseline set to ${equity:,.2f}")
    print(f"after: equity {equity:,.2f} cash {cash:,.2f}")
    print(f"gear-shift baseline written: ${equity:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
