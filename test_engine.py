"""Regime, gear, learner and live-order sanity for the gear-shift desk."""
import math
import os
import tempfile

db = os.path.join(tempfile.mkdtemp(), "gear.sqlite3")
os.environ["ARES_STATE_PATH"] = db
os.environ["START_EQUITY"] = "100000"
os.environ["UNIVERSE"] = "BTC"
os.environ["RISK_PCT"] = "0.01"
os.environ["POLL_SECONDS"] = "300"

import learning
import state
from alpaca_client import AlpacaClient
from config import SETTINGS
from engine import Engine

# Regression: rounding 8dp up made Alpaca reject the LINK exit with a 403
# "insufficient balance" (requested 1633.6147755, available ...495).
assert AlpacaClient.qty_string(1633.614775495) == "1633.61477549"
assert AlpacaClient.qty_string(0.238355091) == "0.23835509"
assert AlpacaClient.qty_string(2.0) == "2"
from gears import range_signal, trend_signal
from regime import RANGE, STAND_ASIDE, TREND, classify, Regime

FEE = 0.0025


def bars_from_closes(closes, start=1_700_000_000):
    """4h bars whose closes follow the supplied path, aligned to a 4h grid."""
    base = start - start % 14400
    out = []
    prev = closes[0]
    for i, close in enumerate(closes):
        high = max(prev, close) * 1.0006
        low = min(prev, close) * 0.9994
        out.append({"ts": base + i * 14400, "o": prev, "h": high, "l": low, "c": close})
        prev = close
    return out


def hourly_from_bars(bars):
    """Split each 4h bar into four hourly bars that aggregate back to it."""
    hourly = []
    for b in bars:
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        path = [(o, o, o, o), (o, h, o, h), (h, h, l, l), (l, max(c, l), min(c, l), c)]
        for k, (ho, hh, hl, hc) in enumerate(path):
            hourly.append({"t": b["ts"] + k * 3600, "o": str(ho), "h": str(hh),
                           "l": str(hl), "c": str(hc), "v": "1"})
    return hourly


# ---------------------------------------------------------------- regime tests
trend_closes = [100.0 * (1.0008 ** i) for i in range(320)]
trend_bars = bars_from_closes(trend_closes)
trend_regs = classify(trend_bars, SETTINGS)
trend = trend_regs[-1]
print(f"trend series -> {trend.gear}/{trend.direction} ADX {trend.adx:.1f} ER {trend.er:.2f} "
      f"spread {trend.trend_spread_pct:+.2f}%")
assert trend.gear == TREND and trend.direction == "up"

# A true range: price oscillates inside a fixed high/low band, so there is no
# directional movement at all and ADX collapses.
range_bars = []
_prev = 100.0
for _i in range(340):
    _close = 100.8 if _i % 2 else 99.2
    range_bars.append({"ts": 1_700_000_000 - 1_700_000_000 % 14400 + _i * 14400,
                       "o": _prev, "h": 101.4, "l": 98.6, "c": _close})
    _prev = _close
range_regs = classify(range_bars, SETTINGS)
rng = range_regs[-1]
print(f"range series -> {rng.gear} ADX {rng.adx:.1f} ER {rng.er:.2f} width {rng.bb_width_pct:.2f}%")
assert rng.gear == RANGE, f"expected range gear, got {rng.gear}"

# ---------------------------------------------------------------- signal tests
setup = trend_signal(trend, SETTINGS)
assert setup and setup["setup"] == "breakout", "a fresh high in an uptrend must fire"
print(f"trend signal: score {setup['score']} stop {setup['stop']:.2f} target {setup['target']:.2f} "
      f"rr {setup['rr']:.2f}")
assert setup["rr"] > 2.5
assert SETTINGS.trend_stop_atr == 3.0 and abs((trend.close - setup["stop"]) - 3 * trend.atr) < 1e-9

fake_range = Regime(
    ts=1, gear=RANGE, direction="flat", close=98.0, adx=12.0, er=0.10, atr=1.0, atr_pct=1.02,
    ema_fast=99.5, ema_slow=99.8, trend_spread_pct=-0.3, rsi=27.0, bb_low=98.2, bb_mid=101.0,
    bb_up=103.8, bb_width_pct=5.7, don_high=103.0, don_low=96.0, stable=3, reason="test",
)
rsetup = range_signal(fake_range, SETTINGS)
assert rsetup and rsetup["setup"] == "band fade", "an oversold lower-band tag must fire"
print(f"range signal: score {rsetup['score']} stop {rsetup['stop']:.2f} target {rsetup['target']:.2f} "
      f"rr {rsetup['rr']:.2f}")
assert abs(rsetup["target"] - 101.0) < 1e-9 and abs(rsetup["stop"] - 96.0) < 1e-9

# a chase (already 2 ATR through the high) must not fire
chase = Regime(**{**trend.as_dict(), "close": trend.don_high + 2 * trend.atr})
assert trend_signal(chase, SETTINGS) is None, "the desk must not chase a breakout"

# ---------------------------------------------------------------- learner tests
state.initialize()

fresh = learning.advice("trend", "BTC", 26, 1.2)
assert fresh["stance"] == "learning" and fresh["mult"] == 1.0, fresh

for _ in range(6):
    learning.update({"gear": "trend", "coin": "ETH", "adx": 26, "atr_pct": 1.2, "r_multiple": 1.4})
winner = learning.advice("trend", "ETH", 26, 1.2)
print(f"winner bucket -> {winner['stance']} x{winner['mult']:.2f} ({winner['note']})")
assert winner["stance"] == "lean_in" and winner["mult"] > 1.0, winner

for _ in range(8):
    learning.update({"gear": "trend", "coin": "LTC", "adx": 26, "atr_pct": 1.2, "r_multiple": -1.0})
loser = learning.advice("trend", "LTC", 26, 1.2)
print(f"loser bucket -> {loser['stance']} x{loser['mult']:.2f} ({loser['note']})")
assert loser["stance"] == "avoid" and loser["mult"] == 0.0, loser

# evidence decays: the winner bucket softens after a long quiet spell
with state.connect() as conn:
    conn.execute("UPDATE learn SET updated_ts='2020-01-01T00:00:00+00:00' WHERE key LIKE '%coin=ETH%'")
eth_stats = learning.stats(["gear=trend|coin=ETH"])
decayed = learning.advice("trend", "ETH", 26, 1.2)
print(f"decayed ETH evidence -> n={eth_stats['n']:.3f}; advice falls back to {decayed['scope']} "
      f"({decayed['stance']})")
assert eth_stats["n"] < 1.0, "old evidence must decay to nearly nothing"
assert decayed["scope"] != "coin + context", "decayed bucket must lose its authority"


# ---------------------------------------------------------------- engine test
class FakeBroker:
    def __init__(self, price, hourly):
        self.price = price
        self.hourly = hourly
        self.cash = 100000.0
        self.qty = 0.0
        self.basis = 0.0
        self.orders = {}
        self.seq = 0

    def latest_market(self, symbols):
        return {s: {"price": self.price, "age_s": 1.0, "spread": 0.0003,
                    "source": "trade"} for s in symbols}

    def bars(self, symbols, days=90):
        return {"BTC/USD": self.hourly}

    def account(self):
        return {"equity": f"{self.cash + self.qty * self.price}", "cash": f"{self.cash}"}

    def positions(self):
        if self.qty <= 0:
            return []
        return [{"symbol": "BTCUSD", "qty": str(self.qty),
                 "avg_entry_price": str(self.basis / self.qty),
                 "cost_basis": str(self.basis)}]

    def _fill(self, qty, price, cash_delta):
        self.seq += 1
        oid = f"order-{self.seq}"
        self.orders[oid] = {"id": oid, "status": "filled", "filled_qty": str(qty),
                            "filled_avg_price": str(price)}
        return self.orders[oid]

    def market_buy(self, symbol, notional, client_order_id):
        fee = notional * FEE
        qty = (notional - fee) / self.price
        self.qty += qty
        self.basis += notional - fee
        self.cash -= notional
        return self._fill(qty, self.price, -notional)

    def market_sell(self, symbol, qty, client_order_id):
        qty = float(qty)
        proceeds = qty * self.price * (1 - FEE)
        self.cash += proceeds
        self.qty -= qty
        self.basis -= self.basis * (qty / (qty + self.qty)) if self.qty > 0 else self.basis
        return self._fill(qty, self.price, proceeds)

    def wait_fill(self, order_id, timeout=30):
        return self.orders[order_id]

    @staticmethod
    def qty_string(qty):
        return f"{qty:.8f}".rstrip("0").rstrip(".")


broker = FakeBroker(price=trend.close, hourly=hourly_from_bars(trend_bars))
eng = Engine()
eng._init_account(broker)
eng._cycle(broker)

book = state.open_trades()
print(f"engine opened {len(book)} trade(s) at {book[0]['entry']:.2f}; broker qty {broker.qty:.8g}")
assert len(book) == 1, "the breakout must open exactly one position"
pos = book[0]
assert pos["gear"] == TREND and pos["setup"] == "breakout"
assert pos["order_id"], "every trade must carry the broker's order id"
assert abs(broker.qty - pos["qty"]) < 1e-9, "ledger quantity must equal the broker position"
assert abs(pos["notional"] - pos["qty"] * pos["entry"]) < 1e-6
assert pos["stop"] < pos["entry"] < pos["target"]
assert pos["risk_usd"] > 0
assert eng.equity < 100000, "entry fees are real and must reduce equity"
print(f"risk ${pos['risk_usd']:.2f} ({pos['risk_usd'] / eng.equity * 100:.2f}% of equity), "
      f"stop {pos['stop']:.2f}, target {pos['target']:.2f}")

# an immediate second cycle must not stack a second position
eng._cycle(broker)
assert len(state.open_trades()) == 1, "one position per coin"

# drop through the stop: the engine must sell, realise the loss and file the lesson
entry_price = pos["entry"]
broker.price = entry_price * 0.90
eng._cycle(broker)
closed = state.closed_trades(5)
print(f"stop exit: {len(closed)} closed, pnl {closed[0]['pnl']:+.2f}, "
      f"R {closed[0]['r_multiple']:+.2f}, broker qty {broker.qty:.8g}")
assert len(closed) == 1 and closed[0]["exit_reason"].startswith("stop")
assert broker.qty < 1e-6, "the spot leg must leave the broker"
assert closed[0]["pnl"] < 0 and closed[0]["r_multiple"] < -0.5
assert "R filed" in (closed[0]["lesson"] or ""), "the learner must receive the trade"
assert float(state.learn_get("gear=trend")["n"]) >= 1
assert abs((broker.cash) - eng.equity) < 1.0, "equity must equal broker cash when flat"
left = learning.advice("trend", "BTC", pos["adx"], pos["atr_pct"])
print(f"after one loss: stance {left['stance']} x{left['mult']:.2f} n={left['n']:.0f}")

# spread gate: a thin book must refuse the entry even with a perfect setup
broker.price = trend.close
broker.latest_market = lambda symbols: {s: {"price": broker.price, "age_s": 1.0,
                                            "spread": 0.004, "source": "trade"} for s in symbols}
state.set_value("paused", "0")
eng2 = Engine()
eng2._cycle(broker)
again = state.open_trades()
assert not again, "a 0.40% spread must block the entry"
last = state.recent_decisions(3)[0]
print(f"wide spread -> {last['decision']}: {last['reason']}")
assert "cost gate" in last["reason"]

print("OK")
