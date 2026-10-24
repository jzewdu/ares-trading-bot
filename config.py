"""Configuration for the paper-first Ares bot."""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass


def _load_env_file() -> None:
    """Load the project's .env so credential edits apply on reload.

    systemd only re-reads EnvironmentFile when the unit restarts, which needs
    root. Reading the file here keeps .env authoritative for the app itself.
    """
    path = pathlib.Path(__file__).with_name(".env")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ[key] = value


_load_env_file()


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _symbols(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return tuple(symbol.strip().upper() for symbol in value.split(",") if symbol.strip())


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    api_secret: str = os.getenv("ALPACA_API_SECRET", "")
    paper: bool = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    ui_user: str = os.getenv("ARES_UI_USER", "")
    ui_password: str = os.getenv("ARES_UI_PASSWORD", "")

    # Delta-neutral carry book. The spot leg is priced on Alpaca, where the
    # long side would live; the short leg is priced on Hyperliquid, the venue
    # whose funding the strategy harvests.
    dn_universe: tuple[str, ...] = _symbols("DN_UNIVERSE", ("LINK", "BTC", "ETH"))
    dn_slots: int = _int("DN_SLOTS", 3)
    dn_notional_pct: float = _float("DN_NOTIONAL_PCT", 0.60)
    dn_margin_pct: float = _float("DN_MARGIN_PCT", 0.25)
    # Enter while a perp is paying shorts, leave if it starts charging them.
    dn_enter_apr: float = _float("DN_ENTER_APR", 2.0)
    dn_exit_apr: float = _float("DN_EXIT_APR", -3.0)
    dn_max_premium: float = _float("DN_MAX_PREMIUM", 0.02)
    # Alpaca's quoted bid/ask on the spot pair. BTC/ETH quote near 0.03%, LINK
    # 0.17%, LTC 0.54% — a market order pays that spread twice, so entries are
    # refused above 0.10%.
    dn_max_spread: float = _float("DN_MAX_SPREAD", 0.0010)
    dn_rebalance_drift: float = _float("DN_REBALANCE_DRIFT", 0.05)
    dn_start_equity: float = _float("DN_START_EQUITY", 0.0)

    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", 0.03)
    poll_seconds: int = _int("POLL_SECONDS", 300)
    state_path: str = os.getenv("ARES_STATE_PATH", "ares_state.sqlite3")
    bind_host: str = os.getenv("ARES_BIND", "127.0.0.1")
    tls_cert: str = os.getenv("ARES_TLS_CERT", "")
    tls_key: str = os.getenv("ARES_TLS_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.ui_user and self.ui_password)

    def spot_symbol(self, coin: str) -> str:
        return f"{coin}/USD"


SETTINGS = Settings()
