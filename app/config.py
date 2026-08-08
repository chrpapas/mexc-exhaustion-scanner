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


def _env_csv_lower(name: str, default: str) -> frozenset[str]:
    raw = os.getenv(name, default)
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    discord_webhook_url: str | None
    discord_signal_levels: frozenset[str]
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
    high_risk_min_amount_24h: float
    high_risk_max_spread_pct: float
    discovery_min_return_24h: float
    discovery_min_cross_section_percentile: float
    wide_scan_seconds: int
    wide_scan_min_return_72h: float
    diagnostic_symbols: frozenset[str]
    state_min_run_score: int
    run_watch_min_24h: float
    run_watch_min_72h: float
    exhaustion_watch_min_72h: float
    exhaustion_watch_min_24h: float
    exhaustion_watch_max_24h: float
    active_exhaustion_min_score: int
    run_watch_alert_cooldown_minutes: int
    exhaustion_watch_alert_cooldown_minutes: int
    short_exhaustion_score: int
    watch_alerts_enabled: bool
    max_symbols: int
    request_rate_per_second: float
    request_concurrency: int
    excluded_symbols: frozenset[str]

    retest_window_candles: int
    retest_tolerance_atr: float
    rearm_new_high_pct: float
    episode_max_age_hours: int

    performance_poll_seconds: int
    performance_report_hour: int
    performance_report_timezone: str

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
            discord_signal_levels=_env_csv_lower(
                "DISCORD_SIGNAL_LEVELS",
                "exhaustion_watch,confirmed_short",
            ),
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
            high_risk_min_amount_24h=float(os.getenv("HIGH_RISK_MIN_AMOUNT_24H", "500000")),
            high_risk_max_spread_pct=float(os.getenv("HIGH_RISK_MAX_SPREAD_PCT", "1.0")),
            discovery_min_return_24h=float(os.getenv("DISCOVERY_MIN_RETURN_24H", "0.05")),
            discovery_min_cross_section_percentile=float(os.getenv("DISCOVERY_MIN_CROSS_SECTION_PERCENTILE", "0.70")),
            wide_scan_seconds=int(os.getenv("WIDE_SCAN_SECONDS", "3600")),
            wide_scan_min_return_72h=float(os.getenv("WIDE_SCAN_MIN_RETURN_72H", "0.20")),
            diagnostic_symbols=_env_csv("DIAGNOSTIC_SYMBOLS", "CASHCAT_USDT"),
            state_min_run_score=int(os.getenv("STATE_MIN_RUN_SCORE", "3")),
            run_watch_min_24h=float(os.getenv("RUN_WATCH_MIN_24H", "0.08")),
            run_watch_min_72h=float(os.getenv("RUN_WATCH_MIN_72H", "0.20")),
            exhaustion_watch_min_72h=float(os.getenv("EXHAUSTION_WATCH_MIN_72H", "0.30")),
            exhaustion_watch_min_24h=float(os.getenv("EXHAUSTION_WATCH_MIN_24H", "-0.25")),
            exhaustion_watch_max_24h=float(os.getenv("EXHAUSTION_WATCH_MAX_24H", "0.08")),
            active_exhaustion_min_score=int(os.getenv("ACTIVE_EXHAUSTION_MIN_SCORE", "2")),
            run_watch_alert_cooldown_minutes=int(
                os.getenv("RUN_WATCH_ALERT_COOLDOWN_MINUTES", "120")
            ),
            exhaustion_watch_alert_cooldown_minutes=int(
                os.getenv("EXHAUSTION_WATCH_ALERT_COOLDOWN_MINUTES", "120")
            ),
            short_exhaustion_score=int(os.getenv("SHORT_EXHAUSTION_SCORE", "3")),
            watch_alerts_enabled=_env_bool("WATCH_ALERTS_ENABLED", True),
            max_symbols=int(os.getenv("MAX_SYMBOLS", "400")),
            request_rate_per_second=float(os.getenv("REQUEST_RATE_PER_SECOND", "8")),
            request_concurrency=int(os.getenv("REQUEST_CONCURRENCY", "4")),
            excluded_symbols=_env_csv("EXCLUDED_SYMBOLS", "BTC_USDT,ETH_USDT"),
            retest_window_candles=int(os.getenv("RETEST_WINDOW_CANDLES", "6")),
            retest_tolerance_atr=float(os.getenv("RETEST_TOLERANCE_ATR", "0.5")),
            rearm_new_high_pct=float(os.getenv("REARM_NEW_HIGH_PCT", "0.05")),
            episode_max_age_hours=int(os.getenv("EPISODE_MAX_AGE_HOURS", "240")),
            performance_poll_seconds=int(os.getenv("PERFORMANCE_POLL_SECONDS", "300")),
            performance_report_hour=int(os.getenv("PERFORMANCE_REPORT_HOUR", "18")),
            performance_report_timezone=os.getenv("PERFORMANCE_REPORT_TIMEZONE", "Europe/Zurich"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.execution_enabled:
            raise RuntimeError("This build is shadow-mode only. Set EXECUTION_ENABLED=false.")
        allowed_signal_levels = {
            "run_watch",
            "exhaustion_watch",
            "breakdown_watch",
            "confirmed_short",
        }
        unknown_signal_levels = self.discord_signal_levels - allowed_signal_levels
        if unknown_signal_levels:
            raise ValueError(
                "DISCORD_SIGNAL_LEVELS contains unsupported levels: "
                + ",".join(sorted(unknown_signal_levels))
            )
        for name, value in (
            ("TICKER_POLL_SECONDS", self.ticker_poll_seconds),
            ("TICKER_STORE_SECONDS", self.ticker_store_seconds),
            ("CANDLE_POLL_SECONDS", self.candle_poll_seconds),
            ("SIGNAL_POLL_SECONDS", self.signal_poll_seconds),
            ("CONTRACT_REFRESH_SECONDS", self.contract_refresh_seconds),
            ("FUNDING_REFRESH_SECONDS", self.funding_refresh_seconds),
            ("WIDE_SCAN_SECONDS", self.wide_scan_seconds),
            ("RETEST_WINDOW_CANDLES", self.retest_window_candles),
            ("EPISODE_MAX_AGE_HOURS", self.episode_max_age_hours),
            ("PERFORMANCE_POLL_SECONDS", self.performance_poll_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.ticker_store_seconds < self.ticker_poll_seconds:
            raise ValueError("TICKER_STORE_SECONDS must be >= TICKER_POLL_SECONDS")
        if self.min_amount_24h < 0 or self.high_risk_min_amount_24h < 0:
            raise ValueError("liquidity amount thresholds must be non-negative")
        if self.max_spread_pct <= 0 or self.high_risk_max_spread_pct <= 0:
            raise ValueError("spread thresholds must be positive")
        if self.high_risk_min_amount_24h > self.min_amount_24h:
            raise ValueError("HIGH_RISK_MIN_AMOUNT_24H must be <= MIN_AMOUNT_24H")
        if self.high_risk_max_spread_pct < self.max_spread_pct:
            raise ValueError("HIGH_RISK_MAX_SPREAD_PCT must be >= MAX_SPREAD_PCT")
        if self.wide_scan_min_return_72h <= 0:
            raise ValueError("WIDE_SCAN_MIN_RETURN_72H must be positive")
        if not 0 <= self.discovery_min_cross_section_percentile <= 1:
            raise ValueError("DISCOVERY_MIN_CROSS_SECTION_PERCENTILE must be between 0 and 1")
        if self.state_min_run_score < 1 or self.state_min_run_score > 6:
            raise ValueError("STATE_MIN_RUN_SCORE must be between 1 and 6")
        if self.exhaustion_watch_min_24h > self.exhaustion_watch_max_24h:
            raise ValueError("EXHAUSTION_WATCH_MIN_24H must be <= EXHAUSTION_WATCH_MAX_24H")
        if self.short_exhaustion_score < 1 or self.short_exhaustion_score > 7:
            raise ValueError("SHORT_EXHAUSTION_SCORE must be between 1 and 7")
        if self.retest_tolerance_atr <= 0:
            raise ValueError("RETEST_TOLERANCE_ATR must be positive")
        if self.rearm_new_high_pct <= 0:
            raise ValueError("REARM_NEW_HIGH_PCT must be positive")
        if not 0 <= self.performance_report_hour <= 23:
            raise ValueError("PERFORMANCE_REPORT_HOUR must be between 0 and 23")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(self.performance_report_timezone)
        except Exception as exc:
            raise ValueError("PERFORMANCE_REPORT_TIMEZONE must be a valid IANA timezone") from exc
