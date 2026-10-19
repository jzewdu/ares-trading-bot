"""Configuration for the paper-first Ares bot."""
from __future__ import annotations

import os
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    api_secret: str = os.getenv("ALPACA_API_SECRET", "")
    paper: bool = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    ui_user: str = os.getenv("ARES_UI_USER", "")
    ui_password: str = os.getenv("ARES_UI_PASSWORD", "")
    max_pairs: int = _int("MAX_PAIRS", 56)
    max_positions: int = _int("MAX_POSITIONS", 5)
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.03)
    max_total_exposure_pct: float = _float("MAX_TOTAL_EXPOSURE_PCT", 0.15)
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", 0.03)
    poll_seconds: int = _int("POLL_SECONDS", 300)
    state_path: str = os.getenv("ARES_STATE_PATH", "ares_state.sqlite3")
    bind_host: str = os.getenv("ARES_BIND", "127.0.0.1")
    tls_cert: str = os.getenv("ARES_TLS_CERT", "")
    tls_key: str = os.getenv("ARES_TLS_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret and self.ui_user and self.ui_password)


SETTINGS = Settings()
