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


def owned_symbols() -> set[str]:
    with connect() as db:
        rows = db.execute("SELECT symbol,side FROM trades WHERE ts IN (SELECT MAX(ts) FROM trades GROUP BY symbol)").fetchall()
    return {str(row["symbol"]) for row in rows if row["side"] == "buy"}


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
