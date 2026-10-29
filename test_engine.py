"""Trend rule, learner and live-order sanity for the BTC trend-core desk."""
import math
import os
import tempfile
from datetime import datetime, timezone

db = os.path.join(tempfile.mkdtemp(), "gear.sqlite3")
os.environ["ARES_STATE_PATH"] = db
os.environ["START_EQUITY"] = "100000"
os.environ["UNIVERSE"] = "BTC"
os.environ["RISK_PCT"] = "0.01"
os.environ["POLL_SECONDS"] = "300"
os.environ["ALPACA_API_KEY"] = "paper-test-key"
os.environ["ALPACA_API_SECRET"] = "paper-test-secret"
os.environ["ALPACA_PAPER"] = "true"
os.environ.pop("ARES_UI_USER", None)
os.environ.pop("ARES_UI_PASSWORD", None)

import learning
import state
from alpaca_client import AlpacaClient
from config import SETTINGS
from engine import Engine
from dataclasses import replace

# The trading loop must be allowed to run on broker credentials even when the
# dashboard login is unset; the web routes remain independently protected.
without_ui_login = replace(SETTINGS, ui_user="", ui_password="")
assert without_ui_login.broker_configured
assert not without_ui_login.configured

# Regression: rounding 8dp up made Alpaca reject the LINK exit with a 403
# "insufficient balance" (requested 1633.6147755, available ...495).
assert AlpacaClient.qty_string(1633.614775495) == "1633.61477549"
assert AlpacaClient.qty_string(0.238355091) == "0.23835509"
assert AlpacaClient.qty_string(2.0) == "2"
from btc_trend import Row, analyse, signal_for
from regime import STAND_ASIDE, TREND

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


# ---------------------------------------------------------------- strategy tests
trend_closes = [100.0 * (1.0008 ** i) for i in range(320)]
trend_bars = bars_from_closes(trend_closes)
trend = analyse(trend_bars, SETTINGS)[-1]
print(f"uptrend -> {trend.gear}/{trend.direction} close {trend.close:.2f} line {trend.ema_fast:.2f} "
      f"ADX {trend.adx:.1f} ATR {trend.atr:.2f}")
assert trend.gear == TREND and trend.direction == "up", trend.reason
assert trend.close > trend.ema_fast

setup = signal_for(trend, SETTINGS)
assert setup and setup["setup"] == "trend core", "an intact trend must offer the core long"
print(f"signal: score {setup['score']} stop {setup['stop']:.2f} target {setup['target']:.2f} "
      f"(stop {SETTINGS.trend_stop_atr:.0f} ATR)")
assert abs((trend.close - setup["stop"]) - SETTINGS.trend_stop_atr * trend.atr) < 1e-9
assert setup["target"] > trend.close and setup["score"] >= 50

# A break well below the trend line is the brake: the desk sells and waits.
break_closes = trend_closes + [trend_closes[-1] * (0.90 ** (i / 6)) for i in range(1, 45)]
brake = analyse(bars_from_closes(break_closes), SETTINGS)[-1]
print(f"break -> {brake.gear}/{brake.direction} ({brake.reason})")
assert brake.gear == STAND_ASIDE and brake.direction == "down"
assert signal_for(brake, SETTINGS) is None, "a broken trend must never be bought"

# Just below the line but inside the brake band: the desk still owns BTC.
inside = Row(ts=1, gear=TREND, direction="flat", close=99.0, adx=20.0, er=0.2,
             atr=1.0, atr_pct=1.0, ema_fast=100.0, ema_slow=98.0, trend_spread_pct=-1.0,
             rsi=45.0, bb_low=98.0, bb_mid=100.0, bb_up=102.0, bb_width_pct=4.0,
             don_high=103.0, don_low=97.0, stable=1, reason="inside band")
assert signal_for(inside, SETTINGS) is not None, "inside the band the core position is still held"

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
assert pos["gear"] == TREND and pos["setup"] == "trend core"
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

# spread gate: a thin book must refuse the entry even with a perfect setup.
# The stop exit above armed the re-entry cooldown, so age it out first.
with state.connect() as conn:
    conn.execute("UPDATE gear_trades SET closed_ts='2020-01-01T00:00:00+00:00'")
broker.price = trend.close
broker.latest_market = lambda symbols: {s: {"price": broker.price, "age_s": 1.0,
                                            "spread": 0.004, "source": "trade"} for s in symbols}
state.set_value("paused", "0")
# The synthetic stop was a 5% account loss, which correctly armed the daily
# loss brake; mark a fresh UTC day at the post-loss equity to test the gate.
state.set_value("gear_baseline_date", datetime.now(timezone.utc).date().isoformat())
state.set_value("gear_baseline_equity", f"{broker.cash:.8f}")
eng2 = Engine()
eng2._cycle(broker)
again = state.open_trades()
assert not again, "a 0.40% spread must block the entry"
last = state.recent_decisions(3)[0]
print(f"wide spread -> {last['decision']}: {last['reason']}")
assert "cost gate" in last["reason"]

# ---------------------------------------------------------------- re-arm gate
# The first live brake exit sold at 79,838 and the desk re-bought at 80,149,
# $203 above the trigger it had just sold under. A brake exit must not be
# bought back at the same price: the close has to clear the brake by
# BTC_REARM_ATR ATRs first, or a market chopping across the line pays 50bp of
# fees per oscillation.
def gate_row(close, brake_level=96.0, line=100.0, atr_v=2.0):
    row = Row(ts=1, gear=TREND, direction="flat", close=close, adx=20.0, er=0.2,
              atr=atr_v, atr_pct=2.0, ema_fast=line, ema_slow=brake_level,
              trend_spread_pct=(close - line) / close * 100, rsi=45.0, bb_low=95.0,
              bb_mid=100.0, bb_up=105.0, bb_width_pct=10.0, don_high=106.0,
              don_low=94.0, stable=1, reason="test")
    data = row.as_dict()
    data.update(coin="BTC", symbol="BTC/USD", signal=signal_for(row, SETTINGS))
    return data


# The learner has already learned this synthetic BTC bucket is a loser, which
# would veto the entry for the wrong reason; the gate is what is under test.
with state.connect() as conn:
    conn.execute("DELETE FROM gear_trades")
    conn.execute("DELETE FROM learn")
for pos in state.open_trades():
    state.close_trade(pos["id"], pos["entry"], 0.0, 0.0, 0.0, 0.0, 0.0, "cleanup", "")
seed = state.open_trade("BTC", "BTC/USD", TREND, "trend core", 82000.0, 0.0, 0.0,
                        74000.0, 82000.0, 1.0, 0.0, 0.0,
                        {"adx": 20.0, "er": 0.2, "atr_pct": 1.0, "rsi": 45.0}, 80.0, "seed")
state.close_trade(seed, 79900.0, 0.0, 0.0, -560.0, -0.29, 4.0,
                  "trend brake — BTC closed below 79,926", "test brake exit")
with state.connect() as conn:
    conn.execute("UPDATE gear_trades SET closed_ts='2020-01-01T00:00:00+00:00'")
state.set_value("paused", "0")

near = Engine()
near.equity, near.cash, near.exposure = 100000.0, 100000.0, 0.0
near.ctx = {"BTC": gate_row(99.0)}
near.prices = {"BTC": {"price": 99.0, "spread": 0.0003, "age": 1.0}}
broker.price = 99.0
near._enter(broker)
print(f"0.5 ATR above the brake -> {len(state.open_trades())} position(s)")
assert not state.open_trades(), "the desk must not buy back a brake exit at the brake"
assert any("Re-arm" in str(d["reason"]) for d in state.recent_decisions(4)), \
    "the refusal must say it is waiting for the re-arm level"

far = Engine()
far.equity, far.cash, far.exposure = 100000.0, 100000.0, 0.0
far.ctx = {"BTC": gate_row(103.0)}
far.prices = {"BTC": {"price": 103.0, "spread": 0.0003, "age": 1.0}}
broker.price = 103.0
far._enter(broker)
print(f"3.5 ATR above the brake -> {len(state.open_trades())} position(s)")
assert len(state.open_trades()) == 1, "a decisive reclaim must re-enable the entry"
broker.price = trend.close

# A cycle exception is retried; it must not terminate the background loop.
import engine as engine_module

class RetryStop:
    def __init__(self):
        self.waits = 0
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, _seconds):
        self.waits += 1
        if self.waits >= 2:
            self.stopped = True
        return self.stopped


class LoopProbe(Engine):
    def __init__(self):
        super().__init__()
        self.cycles = 0
        self._stop = RetryStop()

    def _cycle(self, _client):
        self.cycles += 1
        if self.cycles == 1:
            raise RuntimeError("transient test failure")
        with self._lock:
            self.last_cycle = "retried"
            self.last_error = None
            self.status = "healthy after retry"


class LoopBroker:
    def __init__(self):
        pass

    def account(self):
        return {"equity": "100000", "cash": "100000"}


original_client = engine_module.AlpacaClient
original_settings = engine_module.SETTINGS
engine_module.AlpacaClient = LoopBroker
engine_module.SETTINGS = replace(SETTINGS, api_key="paper-test-key",
                                api_secret="paper-test-secret", paper=True, poll_seconds=60)
try:
    probe = LoopProbe()
    probe._run()
    assert probe.cycles == 2, "engine loop must recover after a transient cycle error"
    assert probe.last_cycle == "retried" and probe.last_error is None
finally:
    engine_module.AlpacaClient = original_client
    engine_module.SETTINGS = original_settings

print("OK")
