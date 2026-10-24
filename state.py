"""Tiny SQLite state store, suitable for the Pi's lightweight service."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from config import SETTINGS


def connect():
    db = sqlite3.connect(SETTINGS.state_path, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def initialize() -> None:
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, price REAL, average REAL, spread_pct REAL)")
        # One row per directional trade. Entry and exit cash are the broker's
        # own deltas, so pnl is net of every fee Alpaca actually charged.
        db.execute("""CREATE TABLE IF NOT EXISTS gear_trades (
            id INTEGER PRIMARY KEY,
            coin TEXT NOT NULL,
            symbol TEXT NOT NULL,
            gear TEXT NOT NULL,
            setup TEXT NOT NULL,
            opened_ts TEXT NOT NULL,
            entry REAL NOT NULL,
            qty REAL NOT NULL,
            cost REAL NOT NULL,
            stop REAL NOT NULL,
            target REAL NOT NULL,
            risk_usd REAL NOT NULL,
            notional REAL NOT NULL,
            entry_fee REAL NOT NULL DEFAULT 0,
            adx REAL, er REAL, atr_pct REAL, rsi REAL, score REAL,
            high_price REAL,
            order_id TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            closed_ts TEXT,
            exit_price REAL,
            proceeds REAL,
            exit_fee REAL,
            pnl REAL,
            r_multiple REAL,
            hold_hours REAL,
            exit_reason TEXT,
            exit_order_id TEXT,
            lesson TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS gear_marks (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, equity REAL NOT NULL,
            exposure REAL NOT NULL, open_count INTEGER NOT NULL, gear TEXT NOT NULL, cash REAL NOT NULL
        )""")
        # Decayed trade memory, one row per learned bucket.
        db.execute("""CREATE TABLE IF NOT EXISTS learn (
            key TEXT PRIMARY KEY, n REAL NOT NULL, wins REAL NOT NULL,
            sum_r REAL NOT NULL, updated_ts TEXT NOT NULL
        )""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(decisions)")}
        if "average" not in columns:
            db.execute("ALTER TABLE decisions ADD COLUMN average REAL")
        if "spread_pct" not in columns:
            db.execute("ALTER TABLE decisions ADD COLUMN spread_pct REAL")
        db.execute("INSERT OR IGNORE INTO kv(key,value) VALUES('paused','0')")


def event(level: str, message: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO events(ts,level,message) VALUES(?,?,?)", (datetime.now(timezone.utc).isoformat(), level, message[:500]))
        db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 120)")


def recent_events(limit: int = 30) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT ts,level,message FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def recent_decisions(limit: int = 30) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT ts,symbol,decision,reason,price,average,spread_pct FROM decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def record_decision(symbol: str, decision: str, reason: str, price: float | None = None,
                    average: float | None = None, spread_pct: float | None = None) -> None:
    with connect() as db:
        db.execute("INSERT INTO decisions(ts,symbol,decision,reason,price,average,spread_pct) VALUES(?,?,?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), symbol, decision, reason[:400], price, average, spread_pct))
        db.execute("DELETE FROM decisions WHERE id NOT IN (SELECT id FROM decisions ORDER BY id DESC LIMIT 300)")


def get_value(key: str, default: str = "") -> str:
    with connect() as db:
        row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_value(key: str, value: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def open_trade(coin: str, symbol: str, gear: str, setup: str, entry: float, qty: float,
               cost: float, stop: float, target: float, risk_usd: float, notional: float,
               entry_fee: float, features: dict, score: float, order_id: str | None) -> int:
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO gear_trades(coin,symbol,gear,setup,opened_ts,entry,qty,cost,stop,target,
               risk_usd,notional,entry_fee,adx,er,atr_pct,rsi,score,high_price,order_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (coin, symbol, gear, setup, datetime.now(timezone.utc).isoformat(), entry, qty, cost,
             stop, target, risk_usd, notional, entry_fee,
             features.get("adx"), features.get("er"), features.get("atr_pct"),
             features.get("rsi"), score, entry, order_id))
    return int(cursor.lastrowid)


def update_high(trade_id: int, high: float) -> None:
    with connect() as db:
        db.execute("UPDATE gear_trades SET high_price=MAX(COALESCE(high_price,0),?) WHERE id=?", (high, trade_id))


def raise_stop(trade_id: int, stop: float) -> None:
    with connect() as db:
        db.execute("UPDATE gear_trades SET stop=? WHERE id=? AND ? > stop", (stop, trade_id, stop))


def close_trade(trade_id: int, exit_price: float, proceeds: float, exit_fee: float, pnl: float,
                r_multiple: float, hold_hours: float, reason: str, lesson: str,
                exit_order_id: str | None = None) -> None:
    with connect() as db:
        db.execute(
            """UPDATE gear_trades SET status='closed', closed_ts=?, exit_price=?, proceeds=?, exit_fee=?,
               pnl=?, r_multiple=?, hold_hours=?, exit_reason=?, lesson=?, exit_order_id=? WHERE id=?""",
            (datetime.now(timezone.utc).isoformat(), exit_price, proceeds, exit_fee, pnl, r_multiple,
             hold_hours, reason[:300], lesson[:500], exit_order_id, trade_id))


def open_trades() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM gear_trades WHERE status='open' ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def closed_trades(limit: int = 30) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM gear_trades WHERE status='closed' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def recent_close(coin: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM gear_trades WHERE coin=? AND status='closed' ORDER BY id DESC LIMIT 1", (coin,)).fetchone()
    return dict(row) if row else None


def record_mark(equity: float, exposure: float, open_count: int, gear: str, cash: float) -> None:
    with connect() as db:
        db.execute("INSERT INTO gear_marks(ts,equity,exposure,open_count,gear,cash) VALUES(?,?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), equity, exposure, open_count, gear, cash))
        db.execute("DELETE FROM gear_marks WHERE id NOT IN (SELECT id FROM gear_marks ORDER BY id DESC LIMIT 2880)")


def recent_marks(limit: int = 800) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT ts,equity,exposure,open_count,gear,cash FROM gear_marks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in reversed(rows)]


def learn_get(key: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT key,n,wins,sum_r,updated_ts FROM learn WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def learn_put(key: str, n: float, wins: float, sum_r: float) -> None:
    with connect() as db:
        db.execute(
            """INSERT INTO learn(key,n,wins,sum_r,updated_ts) VALUES(?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET n=excluded.n, wins=excluded.wins,
               sum_r=excluded.sum_r, updated_ts=excluded.updated_ts""",
            (key, n, wins, sum_r, datetime.now(timezone.utc).isoformat()))


def learn_rows() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT key,n,wins,sum_r,updated_ts FROM learn ORDER BY n DESC").fetchall()
    return [dict(row) for row in rows]
