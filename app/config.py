from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str) -> frozenset[str]:
    raw = os.getenv(name, default)
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    discord_webhook_url: str | None
    mexc_base_url: str
    mexc_spot_base_url: str
    require_mexc_spot_pair: bool
    log_level: str
    execution_enabled: bool

    ticker_poll_seconds: int
    ticker_store_seconds: int
    candle_poll_seconds: int
    signal_poll_seconds: int
    contract_refresh_seconds: int
    funding_refresh_seconds: int

    min_amount_24h: float
    max_spread_pct: float
    state_min_run_score: int
    run_watch_min_24h: float
    run_watch_min_72h: float
    exhaustion_watch_min_72h: float
    exhaustion_watch_min_24h: float
    exhaustion_watch_max_24h: float
    active_exhaustion_min_score: int
    run_watch_alert_cooldown_minutes: int
    exhaustion_watch_alert_cooldown_minutes: int
    short_alert_cooldown_minutes: int
    short_exhaustion_score: int
    watch_alerts_enabled: bool
    max_symbols: int
    request_rate_per_second: float
    request_concurrency: int
    excluded_symbols: frozenset[str]

    @classmethod
    def from_env(cls) -> "Settings":
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url.removeprefix("postgres://")

        settings = cls(
            database_url=database_url,
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
            mexc_base_url=os.getenv("MEXC_BASE_URL", "https://contract.mexc.com"),
            mexc_spot_base_url=os.getenv("MEXC_SPOT_BASE_URL", "https://api.mexc.com"),
            require_mexc_spot_pair=_env_bool("REQUIRE_MEXC_SPOT_PAIR", True),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            execution_enabled=_env_bool("EXECUTION_ENABLED", False),
            ticker_poll_seconds=int(os.getenv("TICKER_POLL_SECONDS", "60")),
            ticker_store_seconds=int(os.getenv("TICKER_STORE_SECONDS", "300")),
            candle_poll_seconds=int(os.getenv("CANDLE_POLL_SECONDS", "900")),
            signal_poll_seconds=int(os.getenv("SIGNAL_POLL_SECONDS", "300")),
            contract_refresh_seconds=int(os.getenv("CONTRACT_REFRESH_SECONDS", "21600")),
            funding_refresh_seconds=int(os.getenv("FUNDING_REFRESH_SECONDS", "3600")),
            min_amount_24h=float(os.getenv("MIN_AMOUNT_24H", "3000000")),
            max_spread_pct=float(os.getenv("MAX_SPREAD_PCT", "0.35")),
            state_min_run_score=int(os.getenv("STATE_MIN_RUN_SCORE", "3")),
            run_watch_min_24h=float(os.getenv("RUN_WATCH_MIN_24H", "0.08")),
            run_watch_min_72h=float(os.getenv("RUN_WATCH_MIN_72H", "0.20")),
            exhaustion_watch_min_72h=float(os.getenv("EXHAUSTION_WATCH_MIN_72H", "0.30")),
            exhaustion_watch_min_24h=float(os.getenv("EXHAUSTION_WATCH_MIN_24H", "-0.05")),
            exhaustion_watch_max_24h=float(os.getenv("EXHAUSTION_WATCH_MAX_24H", "0.08")),
            active_exhaustion_min_score=int(os.getenv("ACTIVE_EXHAUSTION_MIN_SCORE", "2")),
            run_watch_alert_cooldown_minutes=int(
                os.getenv("RUN_WATCH_ALERT_COOLDOWN_MINUTES", "120")
            ),
            exhaustion_watch_alert_cooldown_minutes=int(
                os.getenv("EXHAUSTION_WATCH_ALERT_COOLDOWN_MINUTES", "120")
            ),
            short_alert_cooldown_minutes=int(os.getenv("SHORT_ALERT_COOLDOWN_MINUTES", "120")),
            short_exhaustion_score=int(os.getenv("SHORT_EXHAUSTION_SCORE", "3")),
            watch_alerts_enabled=_env_bool("WATCH_ALERTS_ENABLED", True),
            max_symbols=int(os.getenv("MAX_SYMBOLS", "250")),
            request_rate_per_second=float(os.getenv("REQUEST_RATE_PER_SECOND", "8")),
            request_concurrency=int(os.getenv("REQUEST_CONCURRENCY", "4")),
            excluded_symbols=_env_csv("EXCLUDED_SYMBOLS", "BTC_USDT,ETH_USDT"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.execution_enabled:
            raise RuntimeError("This build is shadow-mode only. Set EXECUTION_ENABLED=false.")
        for name, value in (
            ("TICKER_POLL_SECONDS", self.ticker_poll_seconds),
            ("TICKER_STORE_SECONDS", self.ticker_store_seconds),
            ("CANDLE_POLL_SECONDS", self.candle_poll_seconds),
            ("SIGNAL_POLL_SECONDS", self.signal_poll_seconds),
            ("CONTRACT_REFRESH_SECONDS", self.contract_refresh_seconds),
            ("FUNDING_REFRESH_SECONDS", self.funding_refresh_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.ticker_store_seconds < self.ticker_poll_seconds:
            raise ValueError("TICKER_STORE_SECONDS must be >= TICKER_POLL_SECONDS")
        if self.state_min_run_score < 1 or self.state_min_run_score > 6:
            raise ValueError("STATE_MIN_RUN_SCORE must be between 1 and 6")
        if self.exhaustion_watch_min_24h > self.exhaustion_watch_max_24h:
            raise ValueError("EXHAUSTION_WATCH_MIN_24H must be <= EXHAUSTION_WATCH_MAX_24H")
        if self.short_exhaustion_score < 1 or self.short_exhaustion_score > 7:
            raise ValueError("SHORT_EXHAUSTION_SCORE must be between 1 and 7")
