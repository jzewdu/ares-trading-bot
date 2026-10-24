"""Ledger sanity: broker-backed fills, funding accrual, and a neutral hedge."""
import os
import tempfile
import time

db = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
os.environ["ARES_STATE_PATH"] = db
os.environ["DN_START_EQUITY"] = "100000"
os.environ["DN_UNIVERSE"] = "BTC,ETH"

import engine as E
import state
from state import get_value, set_value

SPOT = {"BTC": 60000.0, "ETH": 2500.0}
PERP = {c: v * 1.001 for c, v in SPOT.items()}
FEE = 0.0025  # Alpaca crypto fee, taken in coin


class FakeHL:
    def context(self):
        return {c: {"coin": c, "funding_hourly": 0.0000125, "funding_apr": 10.95,
                    "premium": 0.0005, "mark": PERP[c], "oracle": SPOT[c],
                    "open_interest": 0, "day_volume": 0} for c in SPOT}

    def funding_history(self, coin, start, end=None):
        # Settlements land on the hour, as Hyperliquid's do.
        now = int(time.time() * 1000)
        hour = 3600 * 1000
        out, ts = [], now - now % hour - 200 * hour
        while ts <= now:
            out.append((ts, 0.0000125))
            ts += hour
        return [(t, r) for t, r in out if t > start and (end is None or t <= end)]


class FakeBroker:
    """Alpaca stand-in: fills market orders, charges its fee in coin, keeps cash."""

    def __init__(self, prices):
        self.prices = prices
        self.book: dict[str, float] = {}
        self.basis: dict[str, float] = {}
        self.orders: dict[str, dict] = {}
        self.seq = 0
        self.cash = 100000.0

    def latest_market(self, symbols):
        return {s: {"price": self.prices[s.split("/")[0]], "age_s": 2.0, "source": "trade",
                    "spread": 0.0003, "bid": 0.0, "ask": 0.0}
                for s in symbols}

    def equity(self):
        holdings = sum(q * self.prices[s.split("/")[0]] for s, q in self.book.items())
        return self.cash + holdings

    def account(self):
        return {"equity": f"{self.equity()}", "cash": f"{self.cash}"}

    def positions(self):
        return [{"symbol": s.replace("/", ""), "qty": str(q),
                 "avg_entry_price": str(self.basis[s] / q), "cost_basis": str(self.basis[s])}
                for s, q in self.book.items() if q > 0]

    def _record(self, symbol, qty, price, cash_delta):
        self.seq += 1
        oid = f"order-{self.seq}"
        self.cash += cash_delta
        self.orders[oid] = {"id": oid, "status": "filled", "filled_qty": str(qty),
                            "filled_avg_price": str(price)}
        return self.orders[oid]

    def market_buy(self, symbol, notional, client_order_id):
        price = self.prices[symbol.split("/")[0]]
        fee = notional * FEE
        qty = round((notional - fee) / price, 8)      # fee taken in coin
        gross_qty = round(notional / price, 8)        # what the order reports
        self.book[symbol] = self.book.get(symbol, 0.0) + qty
        self.basis[symbol] = self.basis.get(symbol, 0.0) + (notional - fee)
        return self._record(symbol, gross_qty, price, -notional)

    def market_sell(self, symbol, qty, client_order_id):
        price = self.prices[symbol.split("/")[0]]
        qty = float(qty)
        self.book[symbol] = self.book.get(symbol, 0.0) - qty
        self.basis[symbol] = self.basis.get(symbol, 0.0) - qty * price
        return self._record(symbol, qty, price, qty * price * (1 - FEE))

    def wait_fill(self, order_id, timeout=30):
        return self.orders[order_id]

    @staticmethod
    def qty_string(qty):
        return f"{qty:.8f}".rstrip("0").rstrip(".")


E.HL = FakeHL()
state.initialize()
set_value("paused", "0")
broker = FakeBroker(dict(SPOT))
eng = E.Engine()
eng._init_ledger(broker)
eng._cycle(broker)  # full cycle: prices, funding, entries, reconcile

book = state.open_positions()
print(f"opened {len(book)} pairs")
assert len(book) == 2, "both eligible pairs should open"
for p in book:
    print(f"  {p['coin']} notional {p['notional']:.2f} spot {p['spot_entry']:.2f} "
          f"perp {p['perp_entry']:.2f} order {p['order_id']}")
    assert p["order_id"], "every pair must carry the broker's order id"
    broker_qty = broker.book.get(p["symbol"], 0.0)
    assert broker_qty > 0, "the broker must actually hold the spot leg"
    # This is the bug that shipped: the ledger held fee-inclusive qty while the
    # broker held less, leaving the hedge silently short.
    assert abs(broker_qty - p["spot_qty"]) < 1e-9, "ledger qty must equal broker qty"
    assert abs(p["notional"] - p["spot_qty"] * p["spot_entry"]) < 1e-6, "notional must be the fill"

eng._reconcile(broker, book)
assert eng.broker_error is None
book = state.open_positions()
assert len(eng.broker) == 2, "reconciliation must see both broker positions"
assert all(b["broker_ok"] for b in eng.snapshot()["book"]), "each position must verify against broker"

equity, delta, gross = eng._mark(book)
eng.equity = equity
fees = float(get_value("dn_fees_all"))
modelled = float(get_value("dn_perp_fees"))
print(f"equity {equity:,.2f}  broker {broker.equity():,.2f}  fees {fees:,.2f} "
      f"(perp modelled {modelled:,.2f})  net delta {delta * 100:+.4f}%")
assert abs(delta) < 1.0, "book should be delta neutral"
assert abs(equity - (100000 - 2 * (20000 * FEE + 19950 * 0.00045))) < 0.01, \
    "equity must equal real spot fees plus modelled perp fees"
assert abs((broker.equity() - modelled) - equity) < 0.01, "ledger must tie to the broker"

# reconciliation identity: net = funding + basis - fees
snapshot = eng.snapshot()
net = snapshot["equity"] - snapshot["start_equity"]
assert abs(net - (snapshot["funding_total"] + snapshot["basis_total"] - snapshot["fees_total"])) < 0.02, \
    "the P&L strip must reconcile exactly"

# simulate two hourly settlements, measured from a window between boundaries
HOUR = 3600 * 1000
now_ms = int(time.time() * 1000)
start = now_ms - now_ms % HOUR - 2 * HOUR + 60_000
for p in book:
    set_value(f"dn_funded_{p['id']}", str(start))
eng._accrue_funding(state.open_positions())
equity2, _, _ = eng._mark(state.open_positions())
expected = sum(p["notional"] * 0.0000125 * 2 for p in book)
print(f"after 2h funding: equity {equity2:,.4f}  gain {equity2 - equity:,.4f}  expected ~{expected:,.4f}")
assert abs((equity2 - equity) - expected) < 0.05, "funding accrual mismatch"

# price move must not change equity: the hedge is neutral
SPOT2 = {c: v * 1.10 for c, v in SPOT.items()}
PERP2 = {c: v * 1.10 for c, v in PERP.items()}
broker.prices.update(SPOT2)
eng.prices = {c: {"price": p, "source": "alpaca", "age": 2.0, "spread": 0.0003}
              for c, p in SPOT2.items()}
eng.ctx = {c: dict(v, mark=PERP2[c]) for c, v in eng.ctx.items()}
equity3, delta3, _ = eng._mark(state.open_positions())
print(f"after +10% move: equity {equity3:,.4f} (was {equity2:,.4f}) delta {delta3 * 100:+.3f}%")
assert abs(equity3 - equity2) < 1.0, "spot rise must be offset by the short"

# exit must sell on the broker, realize the short leg, and leave equity unchanged
pos = state.open_positions()[0]
held = broker.book.get(pos["symbol"], 0.0)
assert eng._close(broker, pos, SPOT2[pos["coin"]], PERP2[pos["coin"]], "test exit") is True
equity4, _, _ = eng._mark(state.open_positions())
print(f"closed one pair: broker qty {held:.8g} -> {broker.book.get(pos['symbol'], 0.0):.8g}, "
      f"equity {equity4:,.2f} (cost of exit {equity3 - equity4:,.2f})")
assert abs(broker.book.get(pos["symbol"], 0.0)) < 1e-9, "the spot leg must leave the broker"
assert 0 < equity3 - equity4 < 120, "closing must cost only the exit fees"

# a thin book must refuse the entry even when funding is attractive
from strategy import entry_check
ok, why = entry_check({"mark": 60000.0}, 10.0, 0.0005, 2.0, 0.02,
                      gap=0.0005, spread=0.0017, max_spread=0.0010)
print(f"wide book: {ok} — {why}")
assert not ok and "spread" in why, "a 0.17% spot spread must block entry"
ok, why = entry_check({"mark": 60000.0}, 10.0, 0.0005, 2.0, 0.02,
                      gap=0.0005, spread=0.0003, max_spread=0.0010)
assert ok, f"a deep book should pass: {why}"
print("OK")
