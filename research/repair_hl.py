"""Refill any coin whose funding history was truncated by rate limiting."""
import json
import sys
import time
import urllib.request

with open("hl_funding.json") as fh:
    data = json.load(fh)

now = int(time.time() * 1000)
floor = now - 730 * 86400 * 1000


def info(payload):
    body = json.dumps(payload).encode()
    for attempt in range(8):
        try:
            req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info", data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as exc:
            print(f"    retry {attempt}: {exc}", file=sys.stderr)
            time.sleep(2.0 + attempt * 2)
    return None


for coin, rows in list(data.items()):
    if len(rows) >= 17000:
        print(f"{coin}: ok ({len(rows)})", file=sys.stderr)
        continue
    print(f"{coin}: refetching (had {len(rows)})", file=sys.stderr)
    seen = {int(r[0]): (float(r[1]), float(r[2])) for r in rows}
    start, empty = floor, 0
    while start < now and empty < 3:
        page = info({"type": "fundingHistory", "coin": coin, "startTime": start})
        if page is None:
            empty += 1
            continue
        if not page:
            break
        for r in page:
            seen[int(r["time"])] = (float(r["fundingRate"]), float(r.get("premium", 0) or 0))
        newest = max(int(r["time"]) for r in page)
        if newest <= start:
            break
        start = newest + 1
        time.sleep(0.5)
    data[coin] = [[t, seen[t][0], seen[t][1]] for t in sorted(seen)]
    span = (data[coin][-1][0] - data[coin][0][0]) / 86400000
    print(f"{coin}: {len(data[coin])} rows, {span:.0f} days", file=sys.stderr)

with open("hl_funding.json", "w") as fh:
    json.dump(data, fh)
