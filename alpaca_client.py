"""Small Alpaca REST client pinned to paper trading."""
from __future__ import annotations

import re
import time
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

    @staticmethod
    def _age_seconds(stamp: str) -> float:
        text = re.sub(r"\.(\d{6})\d+", r".\1", str(stamp).replace("Z", "+00:00"))
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            return 1e9
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())

    def latest_market(self, symbols: list[str]) -> dict[str, dict]:
        """Latest price per pair with its age and quoted spread.

        Alpaca's thinner crypto pairs can go hours without a trade; treating that
        print as the current price invents a basis that is not tradable. The
        spread is reported separately because a market order pays it, and on a
        thin pair it can cost more than months of funding.
        """
        if not symbols:
            return {}
        trades: dict[str, dict] = {}
        quotes: dict[str, dict] = {}

        def absorb_trades(payload: dict) -> None:
            for symbol, trade in (payload.get("trades") or {}).items():
                try:
                    price = float(trade["p"])
                except (KeyError, TypeError, ValueError):
                    continue
                if price > 0:
                    trades[symbol] = {"price": price, "age_s": self._age_seconds(trade.get("t", "")),
                                      "source": "trade"}

        def absorb_quotes(payload: dict) -> None:
            for symbol, quote in (payload.get("quotes") or {}).items():
                try:
                    bid, ask = float(quote["bp"]), float(quote["ap"])
                except (KeyError, TypeError, ValueError):
                    continue
                mid = (bid + ask) / 2
                if mid > 0:
                    quotes[symbol] = {"bid": bid, "ask": ask, "mid": mid,
                                      "spread": (ask - bid) / mid,
                                      "age_s": self._age_seconds(quote.get("t", ""))}

        for offset in range(0, len(symbols), 5):
            batch = symbols[offset:offset + 5]
            for path, absorb in (("trades", absorb_trades), ("quotes", absorb_quotes)):
                try:
                    absorb(self._get(f"{self.data}/v1beta3/crypto/us/latest/{path}",
                                     {"symbols": ",".join(batch)}))
                except requests.RequestException:
                    continue
        for symbol in [s for s in symbols if s not in trades or s not in quotes]:
            for path, absorb in (("trades", absorb_trades), ("quotes", absorb_quotes)):
                try:
                    absorb(self._get(f"{self.data}/v1beta3/crypto/us/latest/{path}", {"symbols": symbol}))
                except requests.RequestException:
                    continue
        out: dict[str, dict] = {}
        for symbol in symbols:
            trade, quote = trades.get(symbol), quotes.get(symbol)
            row = dict(trade) if trade else ({"price": quote["mid"], "age_s": quote["age_s"],
                                              "source": "quote"} if quote else None)
            if not row:
                continue
            row["spread"] = quote["spread"] if quote else None
            row["bid"] = quote["bid"] if quote else None
            row["ask"] = quote["ask"] if quote else None
            out[symbol] = row
        return out

    def bars(self, symbols: list[str], days: int = 90,
             timeframe: str = "1Hour") -> dict[str, list[dict]]:
        """Bars back to `days`, following page tokens to the end.

        The desk runs a 200-bar trend line, so it needs far more than one
        1000-bar page. Small symbol batches keep each response inside the API's
        limits and stop one quiet pair from truncating the batch.
        """
        if not symbols:
            return {}
        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        collected: dict[str, list[dict]] = {}
        for offset in range(0, len(symbols), 4):
            batch = symbols[offset:offset + 4]
            token: str | None = None
            seen: set[str] = set()
            while True:
                params = {"symbols": ",".join(batch), "timeframe": timeframe,
                          "start": start, "limit": 1000, "sort": "asc"}
                if token:
                    params["page_token"] = token
                raw = self._get(f"{self.data}/v1beta3/crypto/us/bars", params)
                for symbol, rows in (raw.get("bars") or {}).items():
                    collected.setdefault(symbol, []).extend(rows)
                token = raw.get("next_page_token")
                if not token or token in seen:
                    break
                seen.add(token)
        out: dict[str, list[dict]] = {}
        for symbol, rows in collected.items():
            by_time = {str(row.get("t")): row for row in rows}
            out[symbol] = [by_time[key] for key in sorted(by_time)]
        return out

    def order(self, order_id: str) -> dict:
        return self._get(f"{self.base}/v2/orders/{order_id}")

    def wait_fill(self, order_id: str, timeout: float = 30.0) -> dict:
        """Poll a crypto order until it reaches a terminal state.

        The ledger is only updated from fill data, so a rejected or cancelled
        order must be visible to the caller rather than assumed filled.
        """
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.order(order_id)
            if last.get("status") in {"filled", "canceled", "rejected", "expired", "replaced"}:
                return last
            time.sleep(0.75)
        return last

    def portal_value(self) -> float:
        """Broker equity in USD, used to audit the book against Alpaca."""
        return float(self.account().get("equity", 0) or 0)

    def market_buy(self, symbol: str, notional: float, client_order_id: str) -> dict:
        return self._post(f"{self.base}/v2/orders", {
            "symbol": symbol,
            "notional": f"{notional:.2f}",
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        })

    @staticmethod
    def qty_string(qty: float) -> str:
        """Alpaca rejects exponent notation and trailing zeros on crypto qty.

        Truncate rather than round: formatting 1633.614775495 to 8 decimals
        rounds up to ...7755, which is more than the account holds and Alpaca
        answers with a 403 'insufficient balance'. Truncating always sells a
        quantity the account can actually cover.
        """
        truncated = int(max(0.0, qty) * 100_000_000) / 100_000_000
        return f"{truncated:.8f}".rstrip("0").rstrip(".")

    def market_sell(self, symbol: str, qty: str, client_order_id: str) -> dict:
        return self._post(f"{self.base}/v2/orders", {
            "symbol": symbol,
            "qty": qty,
            "side": "sell",
            "type": "market",
            "time_in_force": "gtc",
            "client_order_id": client_order_id,
        })
