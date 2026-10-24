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
        db.execute("CREATE TABLE IF NOT EXISTS trades (client_order_id TEXT PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty TEXT, notional REAL, reason TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, price REAL, average REAL, spread_pct REAL)")
        # Delta-neutral ledger: one row per hedged pair, plus its equity marks.
        db.execute("""CREATE TABLE IF NOT EXISTS dn_positions (
            id INTEGER PRIMARY KEY,
            coin TEXT NOT NULL,
            symbol TEXT NOT NULL,
            opened_ts TEXT NOT NULL,
            closed_ts TEXT,
            notional REAL NOT NULL,
            spot_qty REAL NOT NULL,
            perp_qty REAL NOT NULL,
            spot_entry REAL NOT NULL,
            perp_entry REAL NOT NULL,
            entry_premium REAL NOT NULL,
            entry_fee REAL NOT NULL,
            funding REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            exit_spot REAL,
            exit_perp REAL,
            exit_reason TEXT
        )""")
        db.execute("CREATE TABLE IF NOT EXISTS dn_marks (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, equity REAL NOT NULL, net_delta REAL NOT NULL, gross_notional REAL NOT NULL, funding_cum REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS dn_funding (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, coin TEXT NOT NULL, rate REAL NOT NULL, amount REAL NOT NULL)")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(decisions)")}
        if "average" not in columns:
            db.execute("ALTER TABLE decisions ADD COLUMN average REAL")
        if "spread_pct" not in columns:
            db.execute("ALTER TABLE decisions ADD COLUMN spread_pct REAL")
        # Broker order ids make every ledger pair auditable against Alpaca.
        pos_columns = {row["name"] for row in db.execute("PRAGMA table_info(dn_positions)")}
        if "order_id" not in pos_columns:
            db.execute("ALTER TABLE dn_positions ADD COLUMN order_id TEXT")
        if "exit_order_id" not in pos_columns:
            db.execute("ALTER TABLE dn_positions ADD COLUMN exit_order_id TEXT")
        db.execute("INSERT OR IGNORE INTO kv(key,value) VALUES('paused','1')")


def event(level: str, message: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO events(ts,level,message) VALUES(?,?,?)", (datetime.now(timezone.utc).isoformat(), level, message[:500]))
        db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 100)")


def recent_events(limit: int = 30) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT ts,level,message FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def recent_trades(limit: int = 20) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT ts,symbol,side,qty,notional,reason FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
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


def record_trade(client_order_id: str, symbol: str, side: str, qty: str | None, notional: float | None, reason: str) -> None:
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO trades(client_order_id,ts,symbol,side,qty,notional,reason) VALUES(?,?,?,?,?,?,?)",
                   (client_order_id, datetime.now(timezone.utc).isoformat(), symbol, side, qty, notional, reason))


def get_value(key: str, default: str = "") -> str:
    with connect() as db:
        row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_value(key: str, value: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def open_positions() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM dn_positions WHERE status='open' ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def open_position(coin: str, symbol: str, notional: float, spot_qty: float, perp_qty: float,
                  spot_entry: float, perp_entry: float, entry_premium: float, entry_fee: float,
                  order_id: str | None = None) -> int:
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO dn_positions(coin,symbol,opened_ts,notional,spot_qty,perp_qty,spot_entry,
               perp_entry,entry_premium,entry_fee,order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (coin, symbol, datetime.now(timezone.utc).isoformat(), notional, spot_qty, perp_qty,
             spot_entry, perp_entry, entry_premium, entry_fee, order_id))
    return int(cursor.lastrowid)


def close_position(position_id: int, exit_spot: float, exit_perp: float, reason: str,
                   exit_order_id: str | None = None) -> None:
    with connect() as db:
        db.execute("""UPDATE dn_positions SET status='closed', closed_ts=?, exit_spot=?, exit_perp=?,
                   exit_reason=?, exit_order_id=? WHERE id=?""",
                   (datetime.now(timezone.utc).isoformat(), exit_spot, exit_perp, reason[:300],
                    exit_order_id, position_id))


def add_funding(position_id: int, coin: str, amount: float, rate: float) -> None:
    with connect() as db:
        db.execute("UPDATE dn_positions SET funding=funding+? WHERE id=?", (amount, position_id))
        db.execute("INSERT INTO dn_funding(ts,coin,rate,amount) VALUES(?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), coin, rate, amount))
        db.execute("DELETE FROM dn_funding WHERE id NOT IN (SELECT id FROM dn_funding ORDER BY id DESC LIMIT 600)")


def record_mark(equity: float, net_delta: float, gross_notional: float, funding_cum: float) -> None:
    with connect() as db:
        db.execute("INSERT INTO dn_marks(ts,equity,net_delta,gross_notional,funding_cum) VALUES(?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), equity, net_delta, gross_notional, funding_cum))
        db.execute("DELETE FROM dn_marks WHERE id NOT IN (SELECT id FROM dn_marks ORDER BY id DESC LIMIT 2880)")


def recent_marks(limit: int = 800) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT ts,equity,net_delta,gross_notional,funding_cum FROM dn_marks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in reversed(rows)]


def funding_events(limit: int = 40) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT ts,coin,rate,amount FROM dn_funding ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def all_positions() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM dn_positions ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def closed_positions(limit: int = 20) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM dn_positions WHERE status='closed' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
