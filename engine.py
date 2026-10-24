"""Delta-neutral carry loop: long spot, short perp, settled on real funding.

Every cycle the engine marks both legs, settles any funding that has accrued
since the last settlement, and keeps the two notionals balanced. The long leg
is priced on Alpaca (the venue that would hold it); the short leg is priced on
Hyperliquid, whose funding the book collects.

The spot leg is a real order on Alpaca's paper account: every entry and exit
is submitted, filled, and reconciled against the broker's own positions. This
build has no live short venue behind it, so the short leg is a ledger position
rather than an exchange order — marks and funding for it still come from
Hyperliquid's public feed.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import requests

from alpaca_client import AlpacaClient
from config import SETTINGS
from hl_client import HL
from state import (add_funding, all_positions, close_position, event, funding_events,
                   get_value, open_position, open_positions, record_decision, record_mark,
                   recent_marks, set_value)
from strategy import basis, entry_check, exit_reason, trailing_apr

SPOT_FEE = 0.0025    # Alpaca crypto taker, spot leg
PERP_FEE = 0.00045   # Hyperliquid taker, perp leg
MAX_PRINT_AGE = 300  # seconds before an Alpaca print is treated as stale
MAX_ORDER_WAIT = 30  # seconds to wait for a crypto market order to fill


def _ms(value: str) -> int:
    """Accept either an ISO timestamp or raw epoch milliseconds."""
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _pair(symbol: str) -> str:
    """Alpaca reports crypto positions as BTCUSD; the book uses BTC/USD."""
    text = str(symbol)
    if "/" in text or len(text) < 4:
        return text
    return f"{text[:-3]}/{text[-3:]}"


class Engine:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status = "Waiting for paper credentials"
        self.last_cycle: str | None = None
        self.last_error: str | None = None
        self.started_at: str | None = None
        self.ctx: dict[str, dict] = {}
        self.prices: dict[str, float] = {}
        self.equity: float = 0.0
        self.net_delta: float = 0.0
        self.gross_notional: float = 0.0
        self.funding_total: float = 0.0
        self.apr7d: dict[str, float] = {}
        self._apr7d_at: float = 0.0
        # Broker-side truth, refreshed every cycle so the ledger can never drift
        # from what Alpaca actually holds.
        self.broker: dict[str, float] = {}
        self.broker_equity: float = 0.0
        self.broker_error: str | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ares-carry-engine", daemon=True)
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
        self._init_ledger(client)
        while not self._stop.is_set():
            try:
                self._cycle(client)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                event("error", message)
                self._set_status("Data error — holding the book; inspect logs", message)
            self._stop.wait(max(60, SETTINGS.poll_seconds))

    def _init_ledger(self, client: AlpacaClient) -> None:
        if get_value("dn_cash"):
            return
        equity = SETTINGS.dn_start_equity
        if equity <= 0:
            try:
                account = client.account()
                equity = float(account.get("equity", 0) or 0)
                set_value("dn_cash", f"{float(account.get('cash', 0) or 0):.8f}")
            except Exception:
                equity = 0.0
        if equity <= 0:
            equity = 100000.0
        set_value("dn_cash", f"{equity:.8f}")
        set_value("dn_start_equity", f"{equity:.8f}")
        set_value("dn_started", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        set_value("paused", "0")
        event("info", f"Delta-neutral ledger opened with ${equity:,.2f} of paper capital")

    # ------------------------------------------------------------------ pricing
    def _prices(self, client: AlpacaClient) -> dict[str, dict]:
        """Spot marks, with stale Alpaca prints replaced by the index."""
        symbols = [SETTINGS.spot_symbol(c) for c in SETTINGS.dn_universe]
        try:
            market = client.latest_market(symbols)
        except requests.RequestException:
            market = {}
        out: dict[str, dict] = {}
        for coin in SETTINGS.dn_universe:
            quote = market.get(SETTINGS.spot_symbol(coin), {})
            price = float(quote.get("price", 0) or 0)
            age = quote.get("age_s")
            if price > 0 and age is not None and age <= MAX_PRINT_AGE:
                out[coin] = {"price": price, "source": "alpaca", "age": age,
                             "spread": quote.get("spread")}
            elif coin in self.ctx:
                out[coin] = {"price": self.ctx[coin]["oracle"], "source": "hl-index",
                             "age": age, "spread": None}
        return out

    def _refresh_apr(self) -> None:
        """Trailing 7d funding per coin, refreshed hourly rather than each cycle."""
        now = _now_ms()
        if self.apr7d and now - self._apr7d_at < 3600 * 1000:
            return
        for coin in SETTINGS.dn_universe:
            try:
                hist = HL.funding_history(coin, now - 168 * 3600 * 1000, now)
            except requests.RequestException:
                continue
            apr = trailing_apr(hist, now, 168)
            if apr is not None:
                self.apr7d[coin] = apr
        self._apr7d_at = now

    def _cash(self) -> float:
        return float(get_value("dn_cash", "0") or 0)

    def _save_cash(self, value: float) -> None:
        set_value("dn_cash", f"{value:.8f}")

    # ------------------------------------------------------------------ accounting
    def _accrue_funding(self, positions: list[dict]) -> None:
        now = _now_ms()
        for pos in positions:
            last = get_value(f"dn_funded_{pos['id']}", "")
            start = _ms(last) if last else _ms(pos["opened_ts"])
            # Settlements land hourly; checking each cycle credits them promptly
            # without re-summing the same window.
            if now - start < max(300, SETTINGS.poll_seconds) * 1000:
                continue
            try:
                rates = HL.funding_history(pos["coin"], start, now)
            except requests.RequestException as exc:
                event("warning", f"Funding fetch failed for {pos['coin']}: {exc}")
                continue
            settled = [(ts, rate) for ts, rate in rates if ts > start]
            if not settled:
                continue
            rate_sum = sum(rate for _, rate in settled)
            amount = pos["notional"] * rate_sum
            add_funding(pos["id"], pos["coin"], amount, rate_sum)
            self.funding_total += amount
            total = float(get_value("dn_funding_all", "0") or 0) + amount
            set_value("dn_funding_all", f"{total:.8f}")
            today = datetime.now(timezone.utc).date().isoformat()
            if get_value("dn_funding_day", "") != today:
                set_value("dn_funding_day", today)
                set_value("dn_funding_today", "0")
            day_total = float(get_value("dn_funding_today", "0") or 0) + amount
            set_value("dn_funding_today", f"{day_total:.8f}")
            set_value(f"dn_funded_{pos['id']}", str(settled[-1][0]))
            if abs(amount) > 0.005:
                sign = "collected" if amount >= 0 else "paid"
                event("funding", f"{pos['coin']}: {sign} ${abs(amount):,.2f} funding over {len(settled)}h")
            pos["funding"] = float(pos.get("funding", 0) or 0) + amount

    def _mark(self, positions: list[dict]) -> tuple[float, float, float]:
        """Book equity from broker cash, mark-to-market legs and accrued funding.

        Cash is whatever Alpaca reports, so the long leg can never drift from
        the broker; the short leg's P&L and the collected funding sit on top of
        that as modelled, real-data positions.
        """
        cash = self._cash()
        spot_value = 0.0
        delta = 0.0
        gross = 0.0
        perp_pnl = 0.0
        for pos in positions:
            coin = pos["coin"]
            spot = self.prices.get(coin, {}).get("price", pos["spot_entry"])
            perp = self.ctx.get(coin, {}).get("mark", pos["perp_entry"])
            value = pos["spot_qty"] * spot
            spot_value += value
            delta += value - pos["perp_qty"] * perp
            gross += value + pos["perp_qty"] * perp
            perp_pnl += pos["perp_qty"] * (pos["perp_entry"] - perp)
        funding = float(get_value("dn_funding_all", "0") or 0)
        # Closed pairs keep the short leg's realised P&L; without this the book
        # would silently forget it when the pair left the open set.
        perp_pnl += float(get_value("dn_perp_realized", "0") or 0)
        # Modelled short-leg fees are not paid out of broker cash, so they are
        # subtracted here to keep the book's P&L identical to its cost basis.
        modelled = float(get_value("dn_perp_fees", "0") or 0)
        equity = cash + spot_value + perp_pnl + funding - modelled
        return equity, (delta / equity if equity else 0.0), gross

    # ------------------------------------------------------------------ decisions
    def _manage(self, client: AlpacaClient, ctx: dict[str, dict], prices: dict[str, float],
                equity: float) -> None:
        now = _now_ms()
        paused = get_value("paused", "1") != "0"
        positions = open_positions()
        held = {p["coin"] for p in positions}

        for pos in positions:
            coin = pos["coin"]
            row = ctx.get(coin)
            mark_info = prices.get(coin, {})
            spot = mark_info.get("price", pos["spot_entry"])
            if not row or spot <= 0:
                record_decision(pos["symbol"], "HOLD", "No fresh two-venue price; holding the hedge", spot)
                continue
            premium = row["premium"]
            gap = basis(spot, row["mark"])
            try:
                hist = HL.funding_history(coin, now - 72 * 3600 * 1000, now)
            except requests.RequestException:
                hist = []
            apr72 = trailing_apr(hist, now, 72)
            reason = exit_reason(apr72, premium, SETTINGS.dn_exit_apr, SETTINGS.dn_max_premium)
            if reason:
                if self._close(client, pos, spot, row["mark"], reason):
                    held.discard(coin)
            else:
                state = "HOLD"
                detail = (f"Funding {apr72:+.1f}% APR (72h) to the short; perp premium "
                          f"{premium * 100:+.3f}%, venue gap {gap * 100:+.3f}%; exits at "
                          f"{SETTINGS.dn_exit_apr:.1f}% APR or {SETTINGS.dn_max_premium * 100:.1f}% premium") \
                         if apr72 is not None else "Building 72h funding history; holding the hedge"
                record_decision(pos["symbol"], state, detail, spot, row["mark"], premium * 100)

        if paused or equity <= 0:
            return
        slots = max(1, SETTINGS.dn_slots)
        target = equity * SETTINGS.dn_notional_pct / slots
        for coin in SETTINGS.dn_universe:
            if len(held) >= slots:
                break
            if coin in held:
                continue
            row = ctx.get(coin)
            mark_info = prices.get(coin, {})
            spot = mark_info.get("price", 0.0)
            if not row or not spot:
                record_decision(SETTINGS.spot_symbol(coin), "SKIP", "Missing spot or perp price", spot)
                continue
            premium = row["premium"]
            gap = basis(spot, row["mark"])
            apr = self.apr7d.get(coin, row["funding_apr"])
            ok, why = entry_check(row, apr, premium, SETTINGS.dn_enter_apr, SETTINGS.dn_max_premium,
                                  gap=gap, max_gap=0.01, spread=mark_info.get("spread"),
                                  max_spread=SETTINGS.dn_max_spread)
            source = mark_info.get("source", "alpaca")
            if source != "alpaca":
                why += f" · spot marked on Hyperliquid index (Alpaca print stale)"
            record_decision(SETTINGS.spot_symbol(coin), "BUY" if ok else "SKIP", why, spot, row["mark"], premium * 100)
            if ok and self._open(client, coin, spot, row["mark"], premium, target):
                held.add(coin)

    def _add_fees(self, fee: float, modelled: float = 0.0) -> None:
        set_value("dn_fees_all", f"{float(get_value('dn_fees_all', '0') or 0) + fee:.8f}")
        if modelled:
            set_value("dn_perp_fees", f"{float(get_value('dn_perp_fees', '0') or 0) + modelled:.8f}")

    def _open(self, client: AlpacaClient, coin: str, spot: float, perp: float,
              premium: float, notional: float) -> bool:
        """Buy the spot leg on Alpaca, then book the hedge from the actual fill."""
        symbol = SETTINGS.spot_symbol(coin)
        client_order_id = f"ares-{coin.lower()}-{int(time.time() * 1000)}"
        try:
            before = float(client.account().get("cash", 0) or 0)
            submitted = client.market_buy(symbol, notional, client_order_id)
            order = client.wait_fill(submitted["id"], MAX_ORDER_WAIT)
        except (requests.RequestException, KeyError) as exc:
            event("error", f"{coin}: Alpaca spot order failed — {exc}")
            record_decision(symbol, "ERROR", f"Spot order failed: {exc}", spot, perp)
            return False
        if order.get("status") != "filled" or float(order.get("filled_qty") or 0) <= 0:
            event("error", f"{coin}: spot order {order.get('status', 'unknown')} — "
                           f"no fill, hedge not opened")
            record_decision(symbol, "ERROR", f"Spot order {order.get('status')} — no fill", spot, perp)
            return False
        # Alpaca charges its crypto fee in coin, so the broker's position — not
        # the order — is the truth about what the book now holds, and the hedge
        # is sized from that position rather than from the requested notional.
        try:
            held = {_pair(p["symbol"]): p for p in client.positions()}
            after = float(client.account().get("cash", 0) or 0)
        except requests.RequestException as exc:
            event("error", f"{coin}: could not confirm the spot fill — {exc}")
            return False
        position = held.get(symbol) or {}
        qty = float(position.get("qty") or 0)
        if qty <= 0:
            event("error", f"{coin}: order filled but no broker position appeared; hedge not booked")
            return False
        entry = float(position.get("avg_entry_price") or 0) or spot
        basis = float(position.get("cost_basis") or 0) or qty * entry
        spent = max(0.0, before - after)
        spot_fee = spent - basis
        if not 0 <= spot_fee <= basis * 0.02:
            spot_fee = basis * SPOT_FEE
        perp_qty = basis / perp
        perp_fee = basis * PERP_FEE
        fee = spot_fee + perp_fee
        self._save_cash(after)
        self._add_fees(fee, perp_fee)
        pid = open_position(coin, symbol, basis, qty, perp_qty, entry, perp, premium, fee,
                            order.get("id"))
        set_value(f"dn_funded_{pid}", str(_now_ms()))
        event("trade", f"OPEN {coin}: bought {qty:.8g} spot on Alpaca at {entry:.6g} "
                       f"(${basis:,.2f}, fee ${fee:,.2f}, order {str(order.get('id'))[:8]}), "
                       f"short ${basis:,.2f} perp at {perp:.6g} (basis {premium * 100:+.2f}%)")
        record_decision(symbol, "FILLED", f"Spot filled at {entry:.6g} on Alpaca order "
                                          f"{str(order.get('id'))[:8]}; perp hedge booked on "
                                          f"the broker's own quantity", entry, perp)
        return True

    def _close(self, client: AlpacaClient, pos: dict, spot: float, perp: float, reason: str) -> bool:
        """Sell the spot leg on Alpaca; only then close the ledger pair."""
        symbol = pos["symbol"]
        client_order_id = f"ares-x-{pos['coin'].lower()}-{int(time.time() * 1000)}"
        # Sell exactly what the broker holds, so rounding never strands dust.
        qty = pos["spot_qty"]
        try:
            held = {_pair(p["symbol"]): float(p.get("qty") or 0) for p in client.positions()}
            if held.get(symbol, 0) > 0:
                qty = held[symbol]
            before = float(client.account().get("cash", 0) or 0)
            submitted = client.market_sell(symbol, AlpacaClient.qty_string(qty),
                                           client_order_id)
            order = client.wait_fill(submitted["id"], MAX_ORDER_WAIT)
            after = float(client.account().get("cash", 0) or 0)
        except requests.RequestException as exc:
            event("error", f"{pos['coin']}: exit order failed — {exc}; still holding the pair")
            return False
        filled_qty = float(order.get("filled_qty") or 0)
        fill = float(order.get("filled_avg_price") or 0)
        if order.get("status") != "filled" or filled_qty <= 0 or fill <= 0:
            event("error", f"{pos['coin']}: exit order {order.get('status', 'unknown')} — "
                           f"still holding the pair")
            return False
        received = max(0.0, after - before)
        gross = filled_qty * fill
        spot_fee = gross - received
        if not 0 <= spot_fee <= gross * 0.02:
            spot_fee = gross * SPOT_FEE
        perp_fee = gross * PERP_FEE
        fee = spot_fee + perp_fee
        self._save_cash(after)
        self._add_fees(fee, perp_fee)
        realized = float(get_value("dn_perp_realized", "0") or 0) \
            + pos["perp_qty"] * (pos["perp_entry"] - perp)
        set_value("dn_perp_realized", f"{realized:.8f}")
        close_position(pos["id"], fill, perp, reason, order.get("id"))
        event("trade", f"CLOSE {pos['coin']}: sold {filled_qty:.8g} spot at {fill:.6g} on Alpaca; "
                       f"{reason}. Received ${received:,.2f}")
        return True

    def _reconcile(self, client: AlpacaClient, positions: list[dict]) -> None:
        """Compare the ledger with what the broker actually holds."""
        try:
            self.broker = {_pair(p["symbol"]): float(p.get("qty") or 0) for p in client.positions()}
            account = client.account()
            self.broker_equity = float(account.get("equity", 0) or 0)
            # The broker's cash is the ledger's cash: nothing is netted by hand.
            self._save_cash(float(account.get("cash", 0) or 0))
            self.broker_error = None
        except requests.RequestException as exc:
            self.broker_error = str(exc)
            return
        for pos in positions:
            held = self.broker.get(pos["symbol"], 0.0)
            want = pos["spot_qty"]
            ok = want > 0 and abs(held - want) <= want * 0.005
            if not ok and not get_value(f"dn_mismatch_{pos['id']}"):
                set_value(f"dn_mismatch_{pos['id']}", "1")
                event("warning", f"{pos['coin']}: ledger holds {want:.8g} spot, Alpaca holds {held:.8g}")
        tracked = {p["symbol"] for p in positions}
        stray = sorted(s for s, qty in self.broker.items() if qty > 0 and s not in tracked)
        if stray and not get_value("dn_stray_flagged"):
            set_value("dn_stray_flagged", "1")
            event("warning", f"Alpaca holds crypto outside the book: {', '.join(stray)}")

    # ------------------------------------------------------------------ cycle
    def _cycle(self, client: AlpacaClient) -> None:
        self.ctx = HL.context()
        self.prices = self._prices(client)
        self._refresh_apr()
        positions = open_positions()
        self._accrue_funding(positions)

        equity, net_delta, gross = self._mark(positions)
        today = datetime.now(timezone.utc).date().isoformat()
        if get_value("dn_baseline_date") != today:
            set_value("dn_baseline_date", today)
            set_value("dn_baseline_equity", f"{equity:.8f}")
        baseline = float(get_value("dn_baseline_equity", str(equity)) or equity)
        if baseline > 0 and equity <= baseline * (1 - SETTINGS.max_daily_loss_pct):
            if get_value("paused", "1") != "1":
                set_value("paused", "1")
                event("warning", f"Daily loss limit: equity ${equity:,.2f} vs baseline ${baseline:,.2f}; entries paused")

        self._manage(client, self.ctx, self.prices, equity)
        positions = open_positions()
        self._reconcile(client, positions)
        equity, net_delta, gross = self._mark(positions)
        funding_cum = sum(float(p.get("funding", 0) or 0) for p in positions)
        record_mark(equity, net_delta, gross, funding_cum)

        with self._lock:
            self.equity = equity
            self.net_delta = net_delta
            self.gross_notional = gross
            self.funding_total = funding_cum
            self.last_cycle = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.status = "Carry book live" if get_value("paused", "1") == "0" else "Paused — holding hedges"
            self.last_error = None

    # ------------------------------------------------------------------ dashboard
    def _basis_pnl(self) -> float:
        """Price P&L of both legs, from each leg's own venue mark."""
        total = 0.0
        for pos in all_positions():
            if pos["status"] == "closed":
                spot, perp = pos["exit_spot"], pos["exit_perp"]
            else:
                mark_info = self.prices.get(pos["coin"], {})
                spot = mark_info.get("price", pos["spot_entry"])
                perp = self.ctx.get(pos["coin"], {}).get("mark", pos["perp_entry"])
            if spot is None or perp is None:
                continue
            total += pos["spot_qty"] * (spot - pos["spot_entry"])
            total += pos["perp_qty"] * (pos["perp_entry"] - perp)
        return total

    def snapshot(self) -> dict:
        with self._lock:
            positions = open_positions()
            cash = self._cash()
            start = float(get_value("dn_start_equity", "0") or 0)
            today = datetime.now(timezone.utc).date().isoformat()
            marks = recent_marks(1)
            book = []
            for pos in positions:
                coin = pos["coin"]
                row = self.ctx.get(coin, {})
                mark_info = self.prices.get(coin, {})
                spot = mark_info.get("price", pos["spot_entry"])
                perp = row.get("mark", pos["perp_entry"])
                premium = row.get("premium", basis(spot, perp))
                gap = basis(spot, perp)
                pnl_spot = pos["spot_qty"] * (spot - pos["spot_entry"])
                pnl_perp = pos["perp_qty"] * (pos["perp_entry"] - perp)
                book.append({
                    "id": pos["id"], "coin": coin, "symbol": pos["symbol"],
                    "opened": pos["opened_ts"], "notional": pos["notional"],
                    "spot_entry": pos["spot_entry"], "spot": spot,
                    "perp_entry": pos["perp_entry"], "perp": perp,
                    "premium": premium, "entry_premium": pos["entry_premium"],
                    "funding": float(pos.get("funding", 0) or 0),
                    "pnl_spot": pnl_spot, "pnl_perp": pnl_perp,
                    "net": pnl_spot + pnl_perp + float(pos.get("funding", 0) or 0),
                    "funding_apr": row.get("funding_apr", 0.0),
                    "apr7d": self.apr7d.get(coin, row.get("funding_apr", 0.0)),
                    "margin": pos["notional"] * SETTINGS.dn_margin_pct,
                    "venue_gap": gap,
                    "spot_source": mark_info.get("source", "alpaca"),
                    "spot_age": mark_info.get("age"),
                    "order_id": pos.get("order_id"),
                    "broker_qty": self.broker.get(pos["symbol"], 0.0),
                    "broker_ok": (pos["spot_qty"] > 0 and
                                  abs(self.broker.get(pos["symbol"], 0.0) - pos["spot_qty"])
                                  <= pos["spot_qty"] * 0.005),
                })
            carry = 0.0
            weight = 0.0
            for b in book:
                carry += b["apr7d"] * b["notional"]
                weight += b["notional"]
            universe = []
            for coin in SETTINGS.dn_universe:
                row = self.ctx.get(coin, {})
                mark_info = self.prices.get(coin, {})
                spot = mark_info.get("price", 0.0)
                universe.append({
                    "coin": coin, "symbol": SETTINGS.spot_symbol(coin),
                    "funding_apr": row.get("funding_apr", 0.0),
                    "apr7d": self.apr7d.get(coin, row.get("funding_apr", 0.0)),
                    "premium": row.get("premium", 0.0),
                    "gap": basis(spot, row["mark"]) if spot else 0.0,
                    "spot": spot, "mark": row.get("mark", 0.0),
                    "source": mark_info.get("source", "-"),
                    "age": mark_info.get("age"),
                    "spread": mark_info.get("spread"),
                    "in_book": any(b["coin"] == coin for b in book),
                })
            return {
                "status": self.status, "last_cycle": self.last_cycle, "last_error": self.last_error,
                "paused": get_value("paused", "1") != "0", "paper": SETTINGS.paper,
                "broker_equity": self.broker_equity, "broker_positions": len(self.broker),
                "broker_error": self.broker_error,
                "started": get_value("dn_started", ""),
                "start_equity": start, "equity": self.equity, "cash": cash,
                "funding_total": float(get_value("dn_funding_all", "0") or 0),
                "fees_total": float(get_value("dn_fees_all", "0") or 0),
                "basis_total": self._basis_pnl(),
                "funding_today": float(get_value("dn_funding_today", "0") or 0),
                "funding_day": get_value("dn_funding_day", ""),
                "net_delta_pct": self.net_delta * 100,
                "gross_notional": self.gross_notional,
                "margin_used": sum(b["margin"] for b in book),
                "carry_apr": (carry / weight) if weight else 0.0,
                "slots": SETTINGS.dn_slots, "universe": universe, "book": book,
                "leverage": 1 / SETTINGS.dn_margin_pct,
                "marks": marks,
                "all_marks": recent_marks(600),
                "funding_events": funding_events(18),
                "config": {
                    "notional_pct": SETTINGS.dn_notional_pct, "slots": SETTINGS.dn_slots,
                    "enter_apr": SETTINGS.dn_enter_apr, "exit_apr": SETTINGS.dn_exit_apr,
                    "max_premium": SETTINGS.dn_max_premium, "margin_pct": SETTINGS.dn_margin_pct,
                    "max_spread": SETTINGS.dn_max_spread,
                    "spot_fee": SPOT_FEE, "perp_fee": PERP_FEE,
                },
            }


ENGINE = Engine()
