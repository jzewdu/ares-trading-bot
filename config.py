"""Configuration for the paper-first Ares gear-shift bot."""
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

    # One instrument. A spot account cannot short, and spreading the same
    # rules over twelve coins only widened the fee bill; BTC carries the trend.
    universe: tuple[str, ...] = _symbols("UNIVERSE", ("BTC",))
    bars_days: int = _int("BARS_DAYS", 120)

    # Regime thresholds, measured on closed 4h bars.
    trend_adx: float = _float("TREND_ADX", 25.0)
    trend_er: float = _float("TREND_ER", 0.30)
    trend_spread_pct: float = _float("TREND_SPREAD_PCT", 0.35)
    range_adx: float = _float("RANGE_ADX", 22.0)
    range_er: float = _float("RANGE_ER", 0.22)
    range_min_width: float = _float("RANGE_MIN_WIDTH", 1.5)

    # Risk. Risk per trade is the distance to the stop, in equity terms.
    # A 2% risk budget against an 8-ATR stop puts about half of equity into BTC,
    # which is the point of a core position: exposure, not scalping.
    risk_pct: float = _float("RISK_PCT", 0.02)
    max_positions: int = _int("MAX_POSITIONS", 1)
    max_position_pct: float = _float("MAX_POSITION_PCT", 0.5)
    max_exposure_pct: float = _float("MAX_EXPOSURE_PCT", 0.5)
    min_notional: float = _float("MIN_NOTIONAL", 25.0)
    max_spread: float = _float("MAX_SPREAD", 0.0015)
    cooldown_minutes: int = _int("ENTRY_COOLDOWN_MIN", 60)
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", 0.03)
    # How far price may drift from the 4h close before an entry is stale.
    max_signal_drift_atr: float = _float("MAX_SIGNAL_DRIFT_ATR", 2.0)

    # The BTC trend core.
    btc_ema_bars: int = _int("BTC_EMA_BARS", 200)
    btc_brake_atr: float = _float("BTC_BRAKE_ATR", 2.0)

    # Exits. No trailing stop: on this instrument and this fee schedule it
    # turned one trend into a dozen round trips. The brake is the exit.
    trail_atr: float = _float("TRAIL_ATR", 0.0)
    trend_stop_atr: float = _float("TREND_STOP_ATR", 8.0)
    trend_target_atr: float = _float("TREND_TARGET_ATR", 400.0)
    trend_max_days: float = _float("TREND_MAX_DAYS", 365.0)
    range_stop_atr: float = _float("RANGE_STOP_ATR", 2.0)
    range_max_days: float = _float("RANGE_MAX_DAYS", 6.0)
    range_rsi: float = _float("RANGE_RSI", 35.0)
    # Where a range fade takes profit: the middle band is the high-probability
    # exit, the upper band pays more but needs a stronger move.
    range_target: str = os.getenv("RANGE_TARGET", "mid").strip().lower()

    start_equity: float = _float("START_EQUITY", 0.0)
    poll_seconds: int = _int("POLL_SECONDS", 300)
    state_path: str = os.getenv("ARES_STATE_PATH", "ares_state.sqlite3")
    bind_host: str = os.getenv("ARES_BIND", "127.0.0.1")
    tls_cert: str = os.getenv("ARES_TLS_CERT", "")
    tls_key: str = os.getenv("ARES_TLS_KEY", "")

    @property
    def broker_configured(self) -> bool:
        """Whether paper trading can authenticate; dashboard login is separate."""
        return bool(self.paper and self.api_key and self.api_secret)

    @property
    def configured(self) -> bool:
        """Whether both the broker and authenticated dashboard are configured."""
        return bool(self.broker_configured and self.ui_user and self.ui_password)

    def spot_symbol(self, coin: str) -> str:
        return f"{coin}/USD"


SETTINGS = Settings()
