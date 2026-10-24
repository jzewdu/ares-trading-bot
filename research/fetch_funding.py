"""Fetch real funding-rate and basis history from OKX's public API (no keys needed).

Delta-neutral carry needs three series per asset: perpetual funding payments,
spot price, and perpetual price (for basis). OKX pays funding every 8h.
"""
import json
import sys
import time
import urllib.request

BASE = "https://www.okx.com"
SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]
DAYS = 730


def get(path, params):
    url = BASE + path + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.loads(r.read().decode())
            if payload.get("code") == "0":
                return payload["data"]
            print(f"  api code {payload.get('code')} {payload.get('msg')}", file=sys.stderr)
        except Exception as exc:
            print(f"  retry {attempt}: {exc}", file=sys.stderr)
        time.sleep(1.5)
    return []


def funding(asset):
    inst = f"{asset}-USDT-SWAP"
    out, cursor = [], int(time.time() * 1000)
    floor = cursor - DAYS * 86400 * 1000
    while True:
        rows = get("/api/v5/public/funding-rate-history",
                   {"instId": inst, "limit": 100, "after": str(cursor)})
        if not rows:
            break
        out.extend(rows)
        cursor = int(rows[-1]["fundingTime"])
        if cursor <= floor:
            break
        time.sleep(0.12)
    # de-duplicate and sort ascending
    seen = {}
    for r in out:
        ts = int(r["fundingTime"])
        if ts >= floor:
            seen[ts] = float(r["fundingRate"])
    return [[ts, seen[ts]] for ts in sorted(seen)]


def daily(inst):
    out, cursor = [], int(time.time() * 1000)
    floor = cursor - DAYS * 86400 * 1000
    while True:
        rows = get("/api/v5/market/history-candles",
                   {"instId": inst, "bar": "1D", "limit": 100, "after": str(cursor)})
        if not rows:
            break
        out.extend(rows)
        cursor = int(rows[-1][0])
        if cursor <= floor:
            break
        time.sleep(0.12)
    seen = {}
    for r in out:
        seen[int(r[0])] = float(r[4])  # close
    return [[ts, seen[ts]] for ts in sorted(seen)]


def main():
    data = {}
    for asset in SYMBOLS:
        print(f"fetching {asset} ...", file=sys.stderr)
        data[asset] = {
            "funding": funding(asset),
            "spot": daily(f"{asset}-USDT"),
            "perp": daily(f"{asset}-USDT-SWAP"),
        }
        print(f"  funding {len(data[asset]['funding'])}, "
              f"spot {len(data[asset]['spot'])}, perp {len(data[asset]['perp'])}", file=sys.stderr)
    with open("funding_history.json", "w") as fh:
        json.dump(data, fh)
    print("written funding_history.json", file=sys.stderr)


if __name__ == "__main__":
    main()
