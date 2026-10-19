"""Paper-only strategy loop with persistent pause and daily loss stop."""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone

from alpaca_client import AlpacaClient
from config import SETTINGS
from state import event, get_value, owned_symbols, record_trade, set_value
from strategy import signal_for


class Engine:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status = "Waiting for paper credentials"
        self.last_cycle: str | None = None
        self.last_error: str | None = None
        self.scanned = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ares-paper-engine", daemon=True)
        self._thread.start()

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status, "last_cycle": self.last_cycle, "last_error": self.last_error, "scanned": self.scanned,
                    "paused": get_value("paused", "1") != "0", "paper": SETTINGS.paper}

    def _set_status(self, value: str, error: str | None = None, scanned: int | None = None) -> None:
        with self._lock:
            self.status = value
            self.last_error = error
            if scanned is not None:
                self.scanned = scanned

    def _run(self) -> None:
        if not SETTINGS.configured:
            self._set_status("Setup required: add fresh paper API keys and dashboard credentials")
            return
        try:
            client = AlpacaClient()
        except Exception as exc:
            self._set_status("Paper-only configuration error", str(exc))
            return

        self._set_status("Paused — resume from the dashboard to start paper trading")
        while not self._stop.is_set():
            try:
                self._cycle(client)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                event("error", message)
                self._set_status("Paused after API or data error; inspect logs before resuming", message)
                set_value("paused", "1")
            self._stop.wait(max(60, SETTINGS.poll_seconds))

    def _cycle(self, client: AlpacaClient) -> None:
        account = client.account()
        equity = float(account.get("equity", 0) or 0)
        if equity <= 0:
            raise RuntimeError("Account equity is unavailable; refusing to trade")
        today = datetime.now(timezone.utc).date().isoformat()
        baseline_date = get_value("baseline_date")
        if baseline_date != today:
            set_value("baseline_date", today)
            set_value("baseline_equity", str(equity))
        baseline = float(get_value("baseline_equity", str(equity)) or equity)
        if baseline > 0 and equity <= baseline * (1 - SETTINGS.max_daily_loss_pct):
            set_value("paused", "1")
            event("warning", f"Daily loss limit reached: equity {equity:.2f}, baseline {baseline:.2f}; paused")

        assets = client.assets()
        symbols = [a.get("symbol", "") for a in assets
                   if a.get("tradable") and str(a.get("symbol", "")).endswith(("/USD", "/USDC"))]
        symbols = sorted(set(symbols))[:max(1, SETTINGS.max_pairs)]
        bars = client.bars(symbols)
        positions = {p["symbol"]: p for p in client.positions()}
        owned = owned_symbols()
        active_owned = owned.intersection(positions)
        exposure = sum(float(positions[s].get("market_value", 0) or 0) for s in positions)
        self.last_cycle = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._set_status("Scanning paper market", scanned=len(symbols))

        # Existing positions from this bot are the only positions it may exit.
        for symbol in active_owned:
            pos = positions[symbol]
            entry = float(pos.get("avg_entry_price", 0) or 0)
            price = float(pos.get("current_price", 0) or 0)
            qty = str(pos.get("qty", ""))
            if entry > 0 and price > 0 and price <= entry * 0.96 and qty:
                order_id = self._order_id("sell", symbol, self.last_cycle or "")
                result = client.market_sell(symbol, qty, order_id)
                record_trade(order_id, symbol, "sell", qty, None, "4% protective exit")
                event("trade", f"Paper sell submitted: {symbol}; order {result.get('id', 'accepted')}")

        # Pause blocks new entries, while protective exits continue to be checked.
        if get_value("paused", "1") != "0":
            self._set_status("Paused — no new entries; protective exits remain active", scanned=len(symbols))
            return

        # Do not open additional positions while a broker position is being exited.
        positions = {p["symbol"]: p for p in client.positions()}
        for symbol in symbols:
            if len(positions) >= SETTINGS.max_positions:
                break
            if symbol in positions or symbol not in bars or not signal_for(bars[symbol]):
                continue
            close_price = float(bars[symbol][-2]["c"]) if len(bars[symbol]) >= 2 else 0
            if close_price <= 0:
                continue
            account_cap = equity * SETTINGS.max_position_pct
            remaining_cap = max(0.0, equity * SETTINGS.max_total_exposure_pct - exposure)
            notional = min(account_cap, remaining_cap)
            if notional < 5:
                break
            bar_time = str(bars[symbol][-2]["t"])
            order_id = self._order_id("buy", symbol, bar_time)
            result = client.market_buy(symbol, notional, order_id)
            record_trade(order_id, symbol, "buy", None, notional, "15m breakout + trend/volume filter")
            event("trade", f"Paper buy submitted: {symbol} for ${notional:.2f}; order {result.get('id', 'accepted')}")
            exposure += notional
            positions[symbol] = {"symbol": symbol}

        is_running = get_value("paused", "1") == "0"
        self._set_status("Paper trading active" if is_running else "Paused", scanned=len(symbols))

    @staticmethod
    def _order_id(side: str, symbol: str, stamp: str) -> str:
        digest = hashlib.sha256(f"{side}|{symbol}|{stamp}".encode()).hexdigest()[:20]
        return f"ares-{side[0]}-{digest}"


ENGINE = Engine()
