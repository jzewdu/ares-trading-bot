"""Read-only Hyperliquid market data: funding, premium and marks.

No keys and no orders: this client only supplies the numbers the carry book
needs to price its short leg and settle its funding.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
HOURS_YEAR = 24 * 365
HEADERS = {"Content-Type": "application/json", "User-Agent": "ares-carry/1.0"}


class HyperliquidClient:
    def _post(self, payload: dict):
        response = requests.post(INFO_URL, json=payload, headers=HEADERS, timeout=20)
        response.raise_for_status()
        return response.json()

    def context(self) -> dict[str, dict]:
        """Current funding, premium and marks for every perp, in one request."""
        data = self._post({"type": "metaAndAssetCtxs"})
        universe, contexts = data[0]["universe"], data[1]
        out: dict[str, dict] = {}
        for meta, ctx in zip(universe, contexts):
            try:
                funding = float(ctx["funding"])
                mark = float(ctx["markPx"])
            except (KeyError, TypeError, ValueError):
                continue
            out[meta["name"]] = {
                "coin": meta["name"],
                "funding_hourly": funding,
                "funding_apr": funding * HOURS_YEAR * 100,
                "premium": float(ctx.get("premium", 0) or 0),
                "mark": mark,
                "oracle": float(ctx.get("oraclePx", mark) or mark),
                "open_interest": float(ctx.get("openInterest", 0) or 0),
                "day_volume": float(ctx.get("dayNtlVlm", 0) or 0),
            }
        return out

    def funding_history(self, coin: str, start_ms: int, end_ms: int | None = None) -> list[tuple[int, float]]:
        """Settled hourly funding rates, oldest first."""
        rows: list[tuple[int, float]] = []
        start = int(start_ms)
        end = int(end_ms or datetime.now(timezone.utc).timestamp() * 1000)
        while start < end:
            page = self._post({"type": "fundingHistory", "coin": coin, "startTime": start})
            if not page:
                break
            for row in page:
                ts = int(row["time"])
                if ts <= end:
                    rows.append((ts, float(row["fundingRate"])))
            newest = max(int(row["time"]) for row in page)
            if newest <= start or len(page) < 500:
                break
            start = newest + 1
        return rows


HL = HyperliquidClient()
