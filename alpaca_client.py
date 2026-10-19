"""Small Alpaca REST client pinned to paper trading."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from config import SETTINGS


class AlpacaClient:
    def __init__(self) -> None:
        if not SETTINGS.paper:
            raise RuntimeError("This build is paper-only; ALPACA_PAPER must be true")
        self.base = "https://paper-api.alpaca.markets"
        self.data = "https://data.alpaca.markets"
        self.headers = {
            "APCA-API-KEY-ID": SETTINGS.api_key,
            "APCA-API-SECRET-KEY": SETTINGS.api_secret,
        }

    def _get(self, url: str, params: dict | None = None):
        response = requests.get(url, headers=self.headers, params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def _post(self, url: str, payload: dict):
        response = requests.post(url, headers=self.headers, json=payload, timeout=20)
        response.raise_for_status()
        return response.json()

    def account(self) -> dict:
        return self._get(f"{self.base}/v2/account")

    def positions(self) -> list[dict]:
        return self._get(f"{self.base}/v2/positions")

    def assets(self) -> list[dict]:
        return self._get(f"{self.base}/v2/assets", {"asset_class": "crypto", "status": "active"})

    def bars(self, symbols: list[str]) -> dict[str, list[dict]]:
        if not symbols:
            return {}
        # 36h yields <10k 15-minute bars for Alpaca's current crypto universe.
        start = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat(timespec="seconds")
        raw = self._get(
            f"{self.data}/v1beta3/crypto/us/bars",
            {"symbols": ",".join(symbols), "timeframe": "15Min", "start": start, "limit": 10000, "sort": "asc"},
        )
        return raw.get("bars", {})

    def market_buy(self, symbol: str, notional: float, client_order_id: str) -> dict:
        return self._post(f"{self.base}/v2/orders", {
            "symbol": symbol,
            "notional": f"{notional:.2f}",
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        })

    def market_sell(self, symbol: str, qty: str, client_order_id: str) -> dict:
        return self._post(f"{self.base}/v2/orders", {
            "symbol": symbol,
            "qty": qty,
            "side": "sell",
            "type": "market",
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        })
