"""Gear-shift trading loop: read the regime, pick a gear, trade it, learn.

Each cycle the engine rebuilds 4-hour bars, classifies every coin as trending,
ranging or no-trade, and only then looks for setups. A trend coin is bought on
a fresh 20-bar breakout; a range coin is bought at its lower band and sold at
the middle. Positions are sized from the distance to the stop, so every trade
risks the same small slice of equity before the learner adjusts it.

Every exit files the result into `learning.py`, which decays old evidence and
uses it to shrink, block or lean into the same kind of trade next time.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import requests

from alpaca_client import AlpacaClient
from config import SETTINGS
from gears import signal_for
from learning import advice, summary as learn_summary, update as learn_update
from regime import RANGE, STAND_ASIDE, TREND, aggregate, classify
from state import (close_trade, closed_trades, event, get_value, open_trade, open_trades,
                   raise_stop, recent_close, recent_decisions, recent_events, recent_marks,
                   record_decision, record_mark, set_value, update_high)

MAX_ORDER_WAIT = 30
FEE_RATE = 0.0025        # Alpaca crypto taker, taken in coin on buys and in cash on sells
MAX_PRINT_AGE = 300      # seconds before a quote is too stale to trade on
DATA_ERRORS_BEFORE_PAUSE = 6


def _epoch(stamp) -> int:
    text = str(stamp)
    if text.isdigit():
        return int(text)
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(float(text))


def _pair(symbol: str) -> str:
    """Alpaca reports crypto positions as BTCUSD; the book uses BTC/USD."""
    text = str(symbol)
    if "/" in text or len(text) < 4:
        return text
    return f"{text[:-3]}/{text[-3:]}"


def _hours_since(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 3600)
    except ValueError:
        return 0.0


def _score(item) -> float:
    row = item[1] if isinstance(item, tuple) else item
    sig = row.get("signal")
    return float(sig.get("score", 0)) if sig else 0.0


class Engine:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status = "Waiting for paper credentials"
        self.last_cycle: str | None = None
        self.last_error: str | None = None
        self.ctx: dict[str, dict] = {}
        self.prices: dict[str, dict] = {}
        self.equity: float = 0.0
        self.cash: float = 0.0
        self.exposure: float = 0.0
        self.realized: float = 0.0
        self.unrealized: float = 0.0
        self.fees_total: float = 0.0
        self.start_equity: float = 0.0
        self.broker_equity: float = 0.0
        self.broker_error: str | None = None
        self.broker_positions: int = 0
        self.gear_counts: dict[str, int] = {TREND: 0, RANGE: 0, STAND_ASIDE: 0}
        self.headline_gear: str = STAND_ASIDE
        self._data_errors = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ares-gear-engine", daemon=True)
        self._thread.start()

    def _set_status(self, value: str, error: str | None = None) -> None:
        with self._lock:
            self.status = value
            self.last_error = error

    def _run(self) -> None:
        if not SETTINGS.configured:
            self._set_status("Setup required: add fresh paper API keys and dashboard credentials")
            return
        try:
            client = AlpacaClient()
        except Exception as exc:
            self._set_status("Paper-only configuration error", str(exc))
            return
        self._init_account(client)
        while not self._stop.is_set():
            try:
                self._cycle(client)
                self._data_errors = 0
            except Exception as exc:
                self._data_errors += 1
                message = f"{type(exc).__name__}: {exc}"
                event("error", message)
                self._set_status("Data error — holding positions; inspect logs", message)
                if self._data_errors >= DATA_ERRORS_BEFORE_PAUSE and get_value("paused", "0") == "0":
                    set_value("paused", "1")
                    event("warning", "Six failed cycles in a row; entries paused until the feed recovers")
            self._stop.wait(max(60, SETTINGS.poll_seconds))

    def _init_account(self, client: AlpacaClient) -> None:
        if get_value("gear_start_equity"):
            return
        try:
            account = client.account()
            equity = float(account.get("equity", 0) or 0)
            cash = float(account.get("cash", 0) or 0)
        except (requests.RequestException, ValueError) as exc:
            self._set_status("Could not read the paper account", str(exc))
            return
        if SETTINGS.start_equity > 0:
            equity = SETTINGS.start_equity
        set_value("gear_start_equity", f"{equity:.8f}")
        set_value("gear_started", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if not get_value("paused"):
            set_value("paused", "0")
        event("info", f"Gear-shift book opened on ${equity:,.2f} of paper equity "
                      f"(${cash:,.2f} cash)")

    # ------------------------------------------------------------------ data
    def _market(self, client: AlpacaClient) -> dict[str, dict]:
        symbols = [SETTINGS.spot_symbol(c) for c in SETTINGS.universe]
        try:
            quotes = client.latest_market(symbols)
        except requests.RequestException as exc:
            self.broker_error = str(exc)
            quotes = {}
        out: dict[str, dict] = {}
        for coin in SETTINGS.universe:
            quote = quotes.get(SETTINGS.spot_symbol(coin), {})
            price = float(quote.get("price", 0) or 0)
            if price <= 0:
                continue
            out[coin] = {
                "price": price,
                "spread": quote.get("spread"),
                "age": quote.get("age_s"),
                "source": quote.get("source", "trade"),
            }
        return out

    def _analyse(self, bars: dict[str, list[dict]]) -> None:
        ctx: dict[str, dict] = {}
        for coin in SETTINGS.universe:
            symbol = SETTINGS.spot_symbol(coin)
            hourly = []
            for bar in bars.get(symbol, []):
                try:
                    hourly.append({
                        "ts": _epoch(bar["t"]), "o": float(bar["o"]), "h": float(bar["h"]),
                        "l": float(bar["l"]), "c": float(bar["c"]),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            rows = aggregate(hourly, 4)
            regimes = classify(rows, SETTINGS)
            if not regimes:
                ctx[coin] = {
                    "coin": coin, "symbol": symbol, "gear": STAND_ASIDE, "direction": "flat",
                    "stable": 0, "reason": "Waiting for enough 4h history", "signal": None,
                    "adx": 0.0, "er": 0.0, "atr": 0.0, "atr_pct": 0.0, "rsi": 0.0,
                    "close": 0.0, "bb_low": 0.0, "bb_mid": 0.0, "bb_up": 0.0,
                    "bb_width_pct": 0.0, "don_high": 0.0, "don_low": 0.0,
                    "trend_spread_pct": 0.0, "ts": 0,
                }
                continue
            last = regimes[-1]
            row = last.as_dict()
            row["coin"] = coin
            row["symbol"] = symbol
            row["signal"] = signal_for(last, SETTINGS)
            ctx[coin] = row
        self.ctx = ctx
        counts = {TREND: 0, RANGE: 0, STAND_ASIDE: 0}
        for row in ctx.values():
            counts[row["gear"]] = counts.get(row["gear"], 0) + 1
        self.gear_counts = counts
        tradable = counts[TREND] + counts[RANGE]
        if tradable and counts[TREND] >= counts[RANGE]:
            self.headline_gear = TREND
        elif counts[RANGE]:
            self.headline_gear = RANGE
        else:
            self.headline_gear = STAND_ASIDE

    # ------------------------------------------------------------------ accounting
    def _mark(self, client: AlpacaClient) -> float:
        try:
            account = client.account()
            self.equity = float(account.get("equity", 0) or 0)
            self.cash = float(account.get("cash", 0) or 0)
            self.broker_equity = self.equity
            self.broker_error = None
        except (requests.RequestException, ValueError) as exc:
            self.broker_error = str(exc)
            positions_value = 0.0
            for pos in open_trades():
                quote = self.prices.get(pos["coin"], {})
                positions_value += pos["qty"] * float(quote.get("price", pos["entry"]))
            self.equity = self.cash + positions_value
        positions = open_trades()
        self.exposure = 0.0
        self.unrealized = 0.0
        for pos in positions:
            price = float(self.prices.get(pos["coin"], {}).get("price", pos["entry"]))
            self.exposure += pos["qty"] * price
            self.unrealized += pos["qty"] * (price - pos["entry"])
        closed = closed_trades(1000)
        self.realized = sum(float(t.get("pnl") or 0) for t in closed)
        self.fees_total = sum(float(t.get("entry_fee") or 0) + float(t.get("exit_fee") or 0)
                              for t in closed)
        if not self.start_equity:
            self.start_equity = float(get_value("gear_start_equity", "0") or 0)

        today = datetime.now(timezone.utc).date().isoformat()
        if get_value("gear_baseline_date") != today:
            set_value("gear_baseline_date", today)
            set_value("gear_baseline_equity", f"{self.equity:.8f}")
        baseline = float(get_value("gear_baseline_equity", str(self.equity)) or self.equity)
        if baseline > 0 and self.equity <= baseline * (1 - SETTINGS.max_daily_loss_pct):
            if get_value("paused", "0") != "1":
                set_value("paused", "1")
                event("warning", f"Daily loss limit: equity ${self.equity:,.2f} vs "
                                 f"${baseline:,.2f} at UTC open; new entries paused")
        # Reconcile the book against what Alpaca actually holds so a missing or
        # stray position can never be invisible.
        held: dict[str, float] = {}
        try:
            # Positions worth less than a dollar are rounding dust from a
            # previous exit, not something the desk can sell or should flag.
            held = {_pair(p["symbol"]): float(p.get("qty") or 0)
                    for p in client.positions()
                    if float(p.get("market_value") or 0) >= 1.0}
            self.broker_positions = len(held)
        except requests.RequestException:
            self.broker_positions = 0
        else:
            for pos in positions:
                qty = held.get(pos["symbol"])
                if qty is None or abs(qty - float(pos["qty"])) > float(pos["qty"]) * 0.005:
                    if not get_value(f"gear_mismatch_{pos['id']}"):
                        set_value(f"gear_mismatch_{pos['id']}", "1")
                        event("warning", f"{pos['coin']}: ledger holds {pos['qty']:.8g}, "
                                         f"Alpaca holds {qty if qty is not None else 0:.8g}")
            tracked = {pos["symbol"] for pos in positions}
            stray = sorted(s for s, q in held.items() if q > 0 and s not in tracked)
            if stray and not get_value("gear_stray_flagged"):
                set_value("gear_stray_flagged", "1")
                event("warning", f"Alpaca holds crypto outside the book: {', '.join(stray)}")
        return self.equity

    # ------------------------------------------------------------------ exits
    def _manage(self, client: AlpacaClient) -> None:
        for pos in open_trades():
            coin = pos["coin"]
            quote = self.prices.get(coin, {})
            price = float(quote.get("price", 0) or 0)
            row = self.ctx.get(coin, {})
            if price <= 0 or not row:
                continue
            update_high(pos["id"], price)
            high = max(float(pos.get("high_price") or pos["entry"]), price)
            held_days = _hours_since(pos["opened_ts"]) / 24
            reason = None
            if price <= pos["stop"]:
                reason = f"stop at {pos['stop']:.6g}"
            elif price >= pos["target"]:
                reason = f"target at {pos['target']:.6g}"
            elif pos["gear"] == TREND:
                if row["gear"] == STAND_ASIDE and row["direction"] == "down" and row.get("stable", 0) >= 2:
                    reason = "trend reversed — leaving before the stop"
                elif held_days >= SETTINGS.trend_max_days:
                    reason = f"{SETTINGS.trend_max_days:.0f}-day time stop"
                else:
                    trail = high - SETTINGS.trail_atr * float(row["atr"])
                    if trail > pos["stop"]:
                        raise_stop(pos["id"], trail)
                        record_decision(pos["symbol"], "TRAIL",
                                        f"Stop raised to {trail:.6g} ({SETTINGS.trail_atr:.0f} ATR "
                                        f"below the high {high:.6g})", price)
            else:
                if row["gear"] != RANGE and row.get("stable", 0) >= 2:
                    reason = "range broke — the trend no longer suits this gear"
                elif held_days >= SETTINGS.range_max_days:
                    reason = f"{SETTINGS.range_max_days:.0f}-day time stop"
            if reason:
                self._sell(client, pos, price, reason)

    def _sell(self, client: AlpacaClient, pos: dict, price: float, reason: str) -> bool:
        symbol = pos["symbol"]
        try:
            held = {_pair(p["symbol"]): p for p in client.positions()}
        except requests.RequestException:
            held = {}
        # Sell the broker's own quantity string. Rounding 1633.614775495 to 8
        # decimals rounds *up* past the balance, and Alpaca rejects the order
        # with a 403; the raw string is always covered by the position.
        held_pos = held.get(symbol) or {}
        qty = float(held_pos.get("qty") or 0) or float(pos["qty"])
        if qty <= 0:
            event("error", f"{pos['coin']}: no position to sell; trade left open")
            return False
        sell_qty = str(held_pos.get("qty") or "").strip()
        if not sell_qty or "e" in sell_qty.lower():
            sell_qty = AlpacaClient.qty_string(qty)
        try:
            before = float(client.account().get("cash", 0) or 0)
            submitted = client.market_sell(symbol, sell_qty,
                                           f"ares-x-{pos['coin'].lower()}-{int(time.time() * 1000)}")
            order = client.wait_fill(submitted["id"], MAX_ORDER_WAIT)
            after = float(client.account().get("cash", 0) or 0)
        except (requests.RequestException, KeyError) as exc:
            event("error", f"{pos['coin']}: exit order failed — {exc}; still holding")
            return False
        filled = float(order.get("filled_qty") or 0)
        fill = float(order.get("filled_avg_price") or 0)
        if order.get("status") != "filled" or filled <= 0 or fill <= 0:
            event("error", f"{pos['coin']}: exit {order.get('status', 'unknown')} — still holding")
            return False
        proceeds = max(0.0, after - before)
        fee = filled * fill - proceeds
        if not 0 <= fee <= filled * fill * 0.02:
            fee = filled * fill * FEE_RATE
        pnl = proceeds - float(pos["cost"])
        risk = float(pos["risk_usd"]) or 1.0
        r_multiple = pnl / risk
        hold = _hours_since(pos["opened_ts"])
        decision_note = (f"{pos['gear']} {pos['coin']} closed at {fill:.6g} ({reason}); "
                         f"net ${pnl:+,.2f}, {r_multiple:+.2f}R")
        trade = dict(pos, pnl=pnl, r_multiple=r_multiple)
        lessons = learn_update(trade)
        lesson = " · ".join(lessons[-2:]) if lessons else ""
        close_trade(pos["id"], fill, proceeds, fee, pnl, r_multiple, hold, reason, lesson,
                    order.get("id"))
        event("trade", f"CLOSE {pos['coin']} [{pos['gear']}] {r_multiple:+.2f}R "
                       f"(${pnl:+,.2f}) — {reason}. {lesson}")
        record_decision(symbol, "SELL", decision_note, fill)
        self.realized += pnl
        return True

    # ------------------------------------------------------------------ entries
    def _enter(self, client: AlpacaClient) -> None:
        if get_value("paused", "0") != "0" or self.equity <= 0:
            return
        held = {pos["coin"] for pos in open_trades()}
        exposure = self.exposure
        if len(held) >= SETTINGS.max_positions:
            return
        for coin, row in sorted(self.ctx.items(), key=_score, reverse=True):
            if len(held) >= SETTINGS.max_positions:
                break
            if coin in held:
                continue
            signal = row.get("signal")
            quote = self.prices.get(coin, {})
            price = float(quote.get("price", 0) or 0)
            symbol = row["symbol"]
            if not signal or price <= 0:
                continue
            if signal["score"] < 50:
                continue
            spread = quote.get("spread")
            age = quote.get("age")
            if age is not None and age > MAX_PRINT_AGE:
                record_decision(symbol, "SKIP", f"Alpaca print is {age / 60:.0f} min old; "
                                                f"not entering on a stale price", price)
                continue
            if spread is None:
                record_decision(symbol, "SKIP", "No live bid/ask; entry cost cannot be priced", price)
                continue
            if spread > SETTINGS.max_spread:
                record_decision(symbol, "SKIP",
                                f"Spot spread {spread * 100:.2f}% is above the "
                                f"{SETTINGS.max_spread * 100:.2f}% cost gate", price)
                continue
            move = abs(price - signal["entry_ref"]) / max(row["atr"], 1e-12)
            if move > 0.75:
                record_decision(symbol, "SKIP",
                                f"Signal is stale: price has travelled {move:.1f} ATR since the "
                                f"4h close ({signal['entry_ref']:.6g})", price)
                continue
            last = recent_close(coin)
            if last and _hours_since(last.get("closed_ts", "")) * 60 < SETTINGS.cooldown_minutes:
                continue
            view = advice(signal["gear"], coin, float(row["adx"]), float(row["atr_pct"]))
            if view["mult"] <= 0:
                record_decision(symbol, "SKIP", f"Learned avoid — {view['note']}", price,
                                signal["entry_ref"], spread * 100)
                continue
            risk_usd = self.equity * SETTINGS.risk_pct * view["mult"]
            stop_dist = float(signal["stop_dist"])
            if stop_dist <= 0:
                continue
            qty_by_risk = risk_usd / stop_dist
            notional = min(
                qty_by_risk * price,
                self.equity * SETTINGS.max_position_pct,
                max(0.0, self.equity * SETTINGS.max_exposure_pct - exposure),
                self.cash * 0.95 if self.cash else self.equity,
            )
            if notional < SETTINGS.min_notional:
                record_decision(symbol, "SKIP",
                                f"Risk-sized position ${notional:,.2f} is below the "
                                f"${SETTINGS.min_notional:,.0f} minimum", price)
                continue
            if self._open(client, coin, symbol, signal, row, price, notional, stop_dist,
                          view, spread):
                held.add(coin)
                exposure += notional
        self.exposure = exposure

    def _open(self, client: AlpacaClient, coin: str, symbol: str, signal: dict, row: dict,
              price: float, notional: float, stop_dist: float, view: dict,
              spread: float | None) -> bool:
        order_id = f"ares-{coin.lower()}-{int(time.time() * 1000)}"
        try:
            before = float(client.account().get("cash", 0) or 0)
            submitted = client.market_buy(symbol, notional, order_id)
            order = client.wait_fill(submitted["id"], MAX_ORDER_WAIT)
        except (requests.RequestException, KeyError) as exc:
            event("error", f"{coin}: Alpaca buy failed — {exc}")
            record_decision(symbol, "ERROR", f"Buy failed: {exc}", price)
            return False
        if order.get("status") != "filled" or float(order.get("filled_qty") or 0) <= 0:
            event("error", f"{coin}: buy {order.get('status', 'unknown')} — no fill")
            record_decision(symbol, "ERROR", f"Buy {order.get('status')} — no fill", price)
            return False
        try:
            held = {_pair(p["symbol"]): p for p in client.positions()}
            after = float(client.account().get("cash", 0) or 0)
        except requests.RequestException as exc:
            event("error", f"{coin}: could not confirm the fill — {exc}")
            return False
        position = held.get(symbol) or {}
        qty = float(position.get("qty") or 0)
        if qty <= 0:
            event("error", f"{coin}: order filled but no broker position appeared")
            return False
        entry = float(position.get("avg_entry_price") or 0) or price
        cost = max(0.0, before - after)
        entry_fee = cost - qty * entry
        if not 0 <= entry_fee <= cost * 0.02:
            entry_fee = cost * FEE_RATE
        # Anchor the stop and target to the real fill so slippage does not
        # silently change the risk of the trade.
        stop = entry - stop_dist
        target_dist = float(signal["target"]) - float(signal["entry_ref"])
        target = entry + target_dist
        if target <= entry:
            target = entry + stop_dist * 1.5
        risk_usd = qty * stop_dist
        trade_id = open_trade(coin, symbol, signal["gear"], signal["setup"], entry, qty, cost,
                              stop, target, risk_usd, qty * entry, entry_fee,
                              signal["features"], float(signal["score"]), order.get("id"))
        event("trade", f"OPEN {coin} [{signal['gear']}/{signal['setup']}] {qty:.8g} @ "
                       f"{entry:.6g} (${qty * entry:,.2f}) — stop {stop:.6g}, target "
                       f"{target:.6g} ({signal['rr']:.2f}R), risk ${risk_usd:,.2f} "
                       f"(x{view['mult']:.2f} learned sizing), order {str(order.get('id'))[:8]}")
        record_decision(symbol, "BUY",
                        f"{signal['why']}. {view['note']}", entry, target, (spread or 0) * 100)
        return True

    # ------------------------------------------------------------------ cycle
    def _cycle(self, client: AlpacaClient) -> None:
        self.prices = self._market(client)
        if not self.prices:
            raise RuntimeError("Alpaca returned no crypto quotes")
        symbols = [SETTINGS.spot_symbol(c) for c in SETTINGS.universe]
        bars = client.bars(symbols, days=SETTINGS.bars_days)
        self._analyse(bars)
        self._mark(client)
        self._manage(client)
        self._enter(client)
        self._mark(client)
        record_mark(self.equity, self.exposure, len(open_trades()), self.headline_gear, self.cash)
        with self._lock:
            self.start_equity = float(get_value("gear_start_equity", "0") or 0)
            self.last_cycle = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.status = ("Trading the trend gear" if self.headline_gear == TREND else
                           "Trading the range gear" if self.headline_gear == RANGE else
                           "Standing aside — no clean regime")
            self.last_error = None

    # ------------------------------------------------------------------ dashboard
    def snapshot(self) -> dict:
        with self._lock:
            positions = open_trades()
            book = []
            for pos in positions:
                price = float(self.prices.get(pos["coin"], {}).get("price", pos["entry"]))
                pnl = pos["qty"] * (price - pos["entry"])
                risk = float(pos["risk_usd"]) or 1.0
                span = max(pos["target"] - pos["stop"], 1e-12)
                book.append({
                    "id": pos["id"], "coin": pos["coin"], "symbol": pos["symbol"],
                    "gear": pos["gear"], "setup": pos["setup"],
                    "opened": pos["opened_ts"], "hold_hours": _hours_since(pos["opened_ts"]),
                    "entry": pos["entry"], "price": price, "qty": pos["qty"],
                    "notional": pos["notional"], "cost": pos["cost"],
                    "stop": pos["stop"], "target": pos["target"],
                    "risk_usd": risk, "pnl": pnl, "pnl_pct": pnl / pos["cost"] * 100 if pos["cost"] else 0,
                    "r_now": pnl / risk if risk else 0,
                    "progress": max(0.0, min(1.0, (price - pos["stop"]) / span)),
                    "high": pos.get("high_price") or pos["entry"],
                    "score": pos.get("score"), "adx": pos.get("adx"),
                    "er": pos.get("er"), "atr_pct": pos.get("atr_pct"),
                    "order_id": pos.get("order_id"),
                })
            universe = []
            for coin in SETTINGS.universe:
                row = self.ctx.get(coin, {})
                quote = self.prices.get(coin, {})
                signal = row.get("signal")
                age = quote.get("age")
                held = any(p["coin"] == coin for p in book)
                if held:
                    action = "in position"
                elif signal:
                    action = f"{signal['setup']} ready"
                elif row.get("gear") == STAND_ASIDE and row.get("direction") == "down":
                    action = "stand aside (downtrend)"
                elif row.get("gear") == STAND_ASIDE:
                    action = "no clean gear"
                else:
                    action = "waiting for setup"
                universe.append({
                    "coin": coin, "symbol": row.get("symbol", SETTINGS.spot_symbol(coin)),
                    "price": quote.get("price", 0.0), "spread": quote.get("spread"),
                    "age": age, "source": quote.get("source"),
                    "gear": row.get("gear", STAND_ASIDE), "direction": row.get("direction", "flat"),
                    "adx": row.get("adx", 0.0), "er": row.get("er", 0.0),
                    "atr_pct": row.get("atr_pct", 0.0), "rsi": row.get("rsi", 0.0),
                    "stable": row.get("stable", 0),
                    "score": (signal or {}).get("score", 0),
                    "setup": (signal or {}).get("setup"),
                    "rr": (signal or {}).get("rr"),
                    "reason": row.get("reason", "Waiting for data"),
                    "action": action, "held": held,
                })
            marks = recent_marks(600)
            closed = closed_trades(24)
            learner = learn_summary()
            net = self.equity - self.start_equity if self.start_equity else 0.0
            return {
                "status": self.status, "last_cycle": self.last_cycle, "last_error": self.last_error,
                "paused": get_value("paused", "0") != "0", "paper": SETTINGS.paper,
                "gear": self.headline_gear, "gear_counts": self.gear_counts,
                "equity": self.equity, "start_equity": self.start_equity,
                "broker_equity": self.broker_equity, "broker_error": self.broker_error,
                "broker_positions": self.broker_positions,
                "cash": self.cash, "exposure": self.exposure,
                "exposure_pct": (self.exposure / self.equity * 100) if self.equity else 0.0,
                "realized": self.realized, "unrealized": self.unrealized,
                "fees": self.fees_total,
                "net": net, "net_pct": (net / self.start_equity * 100) if self.start_equity else 0.0,
                "open_count": len(book), "max_positions": SETTINGS.max_positions,
                "risk_pct": SETTINGS.risk_pct * 100,
                "open_risk": sum(b["risk_usd"] for b in book),
                "book": book, "universe": universe,
                "learner": learner, "closed": closed,
                "marks": marks, "all_marks": marks,
                "decisions": recent_decisions(120), "events": recent_events(20),
                "started": get_value("gear_started", ""),
                "config": {
                    "trend_adx": SETTINGS.trend_adx, "trend_er": SETTINGS.trend_er,
                    "range_adx": SETTINGS.range_adx, "range_er": SETTINGS.range_er,
                    "trend_stop_atr": SETTINGS.trend_stop_atr,
                    "trend_target_atr": SETTINGS.trend_target_atr,
                    "range_stop_atr": SETTINGS.range_stop_atr,
                    "trail_atr": SETTINGS.trail_atr,
                    "max_spread": SETTINGS.max_spread,
                    "risk_pct": SETTINGS.risk_pct,
                    "max_positions": SETTINGS.max_positions,
                    "max_position_pct": SETTINGS.max_position_pct,
                    "max_exposure_pct": SETTINGS.max_exposure_pct,
                },
            }


ENGINE = Engine()
