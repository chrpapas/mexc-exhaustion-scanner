from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class TraderSettings:
    database_url: str
    mexc_base_url: str
    trading_mode: str
    capital_strategy: str
    position_maturity: str
    paper_starting_equity_usdt: float
    poll_seconds: int
    process_existing_signals: bool
    max_signal_age_seconds: int
    profit_target_pct: float
    isolated_adverse_limit_pct: float
    cross_adverse_limit_pct: float
    mexc_api_key: str | None
    mexc_api_secret: str | None
    mexc_live_order_api_enabled: bool
    live_trading_confirm: str
    trader_discord_webhook_url: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "TraderSettings":
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url.removeprefix("postgres://")

        settings = cls(
            database_url=database_url,
            mexc_base_url=os.getenv("MEXC_BASE_URL", "https://contract.mexc.com").rstrip("/"),
            trading_mode=os.getenv("TRADING_MODE", "paper").strip().lower(),
            capital_strategy=os.getenv("TRADER_CAPITAL_STRATEGY", "cross_20").strip().lower(),
            position_maturity=os.getenv("TRADER_POSITION_MATURITY", "profit_20").strip().lower(),
            paper_starting_equity_usdt=float(os.getenv("PAPER_STARTING_EQUITY_USDT", "2000")),
            poll_seconds=int(os.getenv("TRADER_POLL_SECONDS", "5")),
            process_existing_signals=_bool("TRADER_PROCESS_EXISTING_SIGNALS", False),
            max_signal_age_seconds=int(os.getenv("TRADER_MAX_SIGNAL_AGE_SECONDS", "900")),
            profit_target_pct=float(
                os.getenv("TRADER_PROFIT_TARGET_PCT", os.getenv("TRADER_PROFIT_ACTIVATION_PCT", "20"))
            ),
            isolated_adverse_limit_pct=float(os.getenv("TRADER_ISOLATED_ADVERSE_LIMIT_PCT", "100")),
            cross_adverse_limit_pct=float(os.getenv("TRADER_CROSS_ADVERSE_LIMIT_PCT", "400")),
            mexc_api_key=os.getenv("MEXC_API_KEY") or None,
            mexc_api_secret=os.getenv("MEXC_API_SECRET") or None,
            mexc_live_order_api_enabled=_bool("MEXC_LIVE_ORDER_API_ENABLED", False),
            live_trading_confirm=os.getenv("LIVE_TRADING_CONFIRM", "").strip(),
            trader_discord_webhook_url=os.getenv("DISCORD_TRADER_WEBHOOK_URL") or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.trading_mode not in {"paper", "live"}:
            raise ValueError("TRADING_MODE must be paper or live")
        if self.capital_strategy not in {"isolated_full", "cross_20"}:
            raise ValueError("TRADER_CAPITAL_STRATEGY must be isolated_full or cross_20")
        if self.position_maturity not in {"profit_20", "1d", "2d", "3d", "7d"}:
            raise ValueError("TRADER_POSITION_MATURITY must be profit_20, 1d, 2d, 3d or 7d")
        if self.paper_starting_equity_usdt <= 0:
            raise ValueError("PAPER_STARTING_EQUITY_USDT must be positive")
        if self.poll_seconds < 1:
            raise ValueError("TRADER_POLL_SECONDS must be >= 1")
        if self.max_signal_age_seconds <= 0:
            raise ValueError("TRADER_MAX_SIGNAL_AGE_SECONDS must be positive")
        if self.profit_target_pct <= 0 or self.profit_target_pct >= 100:
            raise ValueError("TRADER_PROFIT_TARGET_PCT must be between 0 and 100")
        if self.isolated_adverse_limit_pct <= 0 or self.cross_adverse_limit_pct <= 0:
            raise ValueError("adverse limits must be positive")
        if self.cross_adverse_limit_pct <= self.isolated_adverse_limit_pct:
            raise ValueError("cross adverse limit must exceed isolated adverse limit")
        if self.trading_mode == "live":
            if not self.mexc_api_key or not self.mexc_api_secret:
                raise RuntimeError("MEXC_API_KEY and MEXC_API_SECRET are required for live mode")
            if not self.mexc_live_order_api_enabled:
                raise RuntimeError(
                    "Live mode is fail-closed because MEXC documents futures order mutation endpoints "
                    "as under maintenance. Set MEXC_LIVE_ORDER_API_ENABLED=true only after your account "
                    "has verified futures API order access."
                )
            if self.live_trading_confirm != "I_UNDERSTAND_LIVE_TRADING":
                raise RuntimeError(
                    "Set LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING to arm live execution"
                )

    @property
    def position_fraction(self) -> float:
        return 1.0 if self.capital_strategy == "isolated_full" else 0.20

    @property
    def open_type(self) -> int:
        return 1 if self.capital_strategy == "isolated_full" else 2

    @property
    def liquidation_proxy_pct(self) -> float:
        return (
            self.isolated_adverse_limit_pct
            if self.capital_strategy == "isolated_full"
            else self.cross_adverse_limit_pct
        )
    @property
    def maturity_seconds(self) -> int | None:
        if self.position_maturity == "profit_20":
            return None
        days = int(self.position_maturity.removesuffix("d"))
        return days * 24 * 60 * 60

