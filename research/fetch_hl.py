"""Two years of hourly perpetual funding from Hyperliquid's public archive.

Hyperliquid is the deepest perp venue that serves history without keys, and its
fundingHistory rows carry the perp premium over the oracle index, which is the
basis a spot-hedged position is exposed to.
"""
import json
import sys
import time
import urllib.request

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX",
         "BCH", "LTC", "DOT", "kBONK"]
DAYS = 730


def info(payload):
    body = json.dumps(payload).encode()
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info", data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as exc:
            print(f"    retry {attempt}: {exc}", file=sys.stderr)
            time.sleep(1.0 + attempt)
    return []


def history(coin):
    start = int(time.time() * 1000) - DAYS * 86400 * 1000
    seen = {}
    for _ in range(60):
        rows = info({"type": "fundingHistory", "coin": coin, "startTime": start})
        if not rows:
            break
        for r in rows:
            seen[int(r["time"])] = (float(r["fundingRate"]), float(r.get("premium", 0) or 0))
        newest = max(int(r["time"]) for r in rows)
        if len(rows) < 500:
            break
        start = newest + 1
        time.sleep(0.15)
    return [[ts, seen[ts][0], seen[ts][1]] for ts in sorted(seen)]


def main():
    data = {}
    for coin in COINS:
        print(f"fetching {coin} ...", file=sys.stderr)
        rows = history(coin)
        data[coin] = rows
        if rows:
            first = time.strftime("%Y-%m-%d", time.gmtime(rows[0][0] / 1000))
            last = time.strftime("%Y-%m-%d", time.gmtime(rows[-1][0] / 1000))
        else:
            first = last = "none"
        print(f"  {len(rows)} rows  {first} -> {last}", file=sys.stderr)
    with open("hl_funding.json", "w") as fh:
        json.dump(data, fh)
    print("written hl_funding.json", file=sys.stderr)


if __name__ == "__main__":
    main()
