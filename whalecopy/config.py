"""Settings come from config.yaml (strategy, wallets, limits) and env vars (secrets, mode).

Env vars always win over the YAML file so you can change a limit in your cloud
dashboard without redeploying.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

KALSHI_BASE_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
LIVE_CONFIRM_PHRASE = "yes-real-money"


@dataclass
class Settings:
    # --- mode -------------------------------------------------------------
    mode: str = "paper"                 # paper | live
    live_confirm: str = ""              # must equal LIVE_CONFIRM_PHRASE for live
    kalshi_env: str = "prod"            # prod | demo
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""   # path to the PEM file
    kalshi_private_key_pem: str = ""    # or the PEM contents (handy for cloud secrets)
    data_dir: str = "./data"

    # --- who to follow ----------------------------------------------------
    wallets: list[str] = field(default_factory=list)

    # --- which whale trades count as signals -------------------------------
    min_whale_trade_usd: float = 500.0   # ignore smaller whale buys
    big_whale_trade_usd: float = 5000.0  # at/above this we size at max_trade_usd
    min_price: float = 0.05              # skip longshots
    max_price: float = 0.92              # skip near-certainties (little upside after fees)
    max_signal_age_sec: int = 600        # ignore whale trades older than this
    buildup_window_sec: int = 900        # add up a whale's small buys of one outcome over this window
    mirror_sells: bool = True            # when a followed whale sells, close our copy

    # --- sizing (USD per copied trade) -------------------------------------
    min_trade_usd: float = 25.0
    max_trade_usd: float = 50.0
    max_slippage: float = 0.03           # pay at most whale price + 3 cents
    paper_fee_rate: float = 0.07         # paper-mode fee estimate: rate * contracts * p * (1-p)
    time_in_force: str = "immediate_or_cancel"

    # --- risk limits --------------------------------------------------------
    daily_max_usd: float = 250.0
    max_open_usd: float = 1000.0
    per_market_max_usd: float = 100.0
    max_trades_per_day: int = 15
    cooldown_minutes: int = 60           # one copy per wallet+market per window
    paused: bool = False                 # kill switch

    # --- market matching ----------------------------------------------------
    auto_approve_score: float = 92.0     # matches at/above this are used without review
    review_min_score: float = 70.0       # below this we don't even queue for review
    date_tolerance_days: float = 3.0
    sports_auto_approve: bool = False    # use slug-based game matches in LIVE mode (paper always does)
    index_refresh_hours: float = 4.0

    # --- loop / notifications ------------------------------------------------
    poll_seconds: int = 30
    notify_webhook_url: str = ""         # Slack or Discord incoming webhook (optional)
    log_level: str = "INFO"

    # --- web dashboard (read-only; off unless a password is set) ------------------
    dashboard_password: str = ""         # set as a secret: DASHBOARD_PASSWORD
    dashboard_port: int = 8080
    wallet_names: list[str] = field(default_factory=list)  # "0xabc...=lava-lava" labels

    # ------------------------------------------------------------------------
    @property
    def kalshi_base_url(self) -> str:
        return KALSHI_BASE_URLS[self.kalshi_env]

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "whalecopy.db"

    def private_key_bytes(self) -> bytes | None:
        if self.kalshi_private_key_pem.strip():
            return self.kalshi_private_key_pem.replace("\\n", "\n").encode()
        if self.kalshi_private_key_path:
            return Path(self.kalshi_private_key_path).expanduser().read_bytes()
        return None

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError(f"MODE must be 'paper' or 'live', got {self.mode!r}")
        if self.kalshi_env not in KALSHI_BASE_URLS:
            raise ValueError(f"KALSHI_ENV must be one of {list(KALSHI_BASE_URLS)}")
        if self.min_trade_usd <= 0 or self.max_trade_usd < self.min_trade_usd:
            raise ValueError("need 0 < min_trade_usd <= max_trade_usd")
        if not (0 < self.min_price < self.max_price < 1):
            raise ValueError("need 0 < min_price < max_price < 1")
        if self.time_in_force not in ("immediate_or_cancel", "fill_or_kill"):
            raise ValueError("time_in_force must be immediate_or_cancel or fill_or_kill")
        if self.is_live:
            if self.live_confirm != LIVE_CONFIRM_PHRASE:
                raise ValueError(
                    f"Live mode needs LIVE_CONFIRM={LIVE_CONFIRM_PHRASE} as a second switch."
                )
            if not self.kalshi_api_key_id or self.private_key_bytes() is None:
                raise ValueError("Live mode needs KALSHI_API_KEY_ID and a private key.")
        for name in ("min_whale_trade_usd", "big_whale_trade_usd", "daily_max_usd",
                     "max_open_usd", "per_market_max_usd", "max_slippage", "paper_fee_rate",
                     "max_trades_per_day", "poll_seconds", "buildup_window_sec"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} can't be negative")
        if self.per_market_max_usd < self.min_trade_usd:
            raise ValueError("per_market_max_usd must be at least min_trade_usd")
        if self.big_whale_trade_usd < self.min_whale_trade_usd:
            raise ValueError("big_whale_trade_usd must be >= min_whale_trade_usd")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        if self.dashboard_password and len(self.dashboard_password) < 8:
            raise ValueError("DASHBOARD_PASSWORD must be at least 8 characters")
        for w in self.wallets:
            if not (w.startswith("0x") and len(w) == 42):
                raise ValueError(f"Not a Polymarket wallet address: {w!r}")


_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off", ""}


def _coerce(name: str, value, type_name: str):
    """Coerce by the field's declared type, not by whatever type the YAML happened to use."""
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        raise ValueError(f"{name.upper()} must be true or false, got {value!r}")
    if type_name == "int":
        return int(float(value))
    if type_name == "float":
        return float(value)
    if type_name.startswith("list"):
        if value is None:
            return []
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return list(value)
    return "" if value is None else str(value)


def load_settings(config_path: str | None = None) -> Settings:
    s = Settings()
    path = Path(config_path or os.environ.get("WHALECOPY_CONFIG", "config.yaml"))
    types = {f.name: str(f.type) for f in fields(Settings)}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        unknown = set(data) - set(types)
        if unknown:
            raise ValueError(f"Unknown keys in {path}: {sorted(unknown)}")
        for secret in ("live_confirm", "kalshi_private_key_pem", "dashboard_password"):
            if data.get(secret):
                raise ValueError(f"Set {secret.upper()} as an environment variable, "
                                 f"not in {path.name}")
        for k, v in data.items():
            setattr(s, k, _coerce(k, v, types[k]))
    for name, type_name in types.items():
        env = os.environ.get(name.upper())
        if env is not None and env != "":
            setattr(s, name, _coerce(name, env, type_name))
    s.wallets = [w.strip().lower() for w in (s.wallets or [])]
    s.validate()
    return s
