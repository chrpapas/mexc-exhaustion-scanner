from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_upper(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    values = tuple(dict.fromkeys(v.strip().upper() for v in raw.split(",") if v.strip()))
    return values


@dataclass(frozen=True, slots=True)
class TraderSettings:
    database_url: str
    mexc_base_url: str
    mexc_ws_url: str
    trading_mode: str
    margin_mode: str
    legacy_position_maturity: str
    leverage: int
    allowed_risk_tiers: tuple[str, ...]
    max_open_positions: int
    slot_allocation_pct: float
    max_total_exposure_pct: float
    max_standard_positions: int
    max_high_risk_positions: int
    standard_hold_days: int
    high_risk_timeout_days: int
    allow_same_symbol_parallel: bool
    paper_starting_equity_usdt: float
    paper_taker_fee_rate: float
    poll_seconds: int
    process_existing_signals: bool
    max_signal_age_seconds: int
    profit_target_pct: float
    protection_arm_pct: float
    trail_callback_pct: float
    protection_update_step_pct: float
    protection_notify_step_pct: float
    mexc_api_key: str | None
    mexc_api_secret: str | None
    mexc_live_order_api_enabled: bool
    live_trading_confirm: str
    trader_events_webhook_url: str | None
    heartbeat_seconds: int
    error_alert_cooldown_seconds: int
    log_level: str

    @classmethod
    def from_env(cls) -> "TraderSettings":
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is required")
        if database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url.removeprefix("postgres://")

        base_url = os.getenv("MEXC_BASE_URL", "https://api.mexc.com").rstrip("/")
        if base_url == "https://contract.mexc.com":
            base_url = "https://api.mexc.com"

        webhook = (
            os.getenv("DISCORD_TRADER_EVENTS_WEBHOOK_URL")
            or os.getenv("DISCORD_TRADER_WEBHOOK_URL")
            or None
        )
        allowed_risk_tiers = _csv_upper("TRADER_ALLOWED_RISK_TIERS", "STANDARD,HIGH_RISK")
        max_open_positions = int(os.getenv("TRADER_MAX_OPEN_POSITIONS", "6"))
        if max_open_positions < 1:
            raise ValueError("TRADER_MAX_OPEN_POSITIONS must be >= 1")
        max_total_exposure_pct = float(os.getenv("TRADER_MAX_TOTAL_EXPOSURE_PCT", "20"))
        slot_allocation_pct = float(
            os.getenv("TRADER_SLOT_ALLOCATION_PCT", str(max_total_exposure_pct / max_open_positions))
        )
        paired_tiers = "STANDARD" in allowed_risk_tiers and "HIGH_RISK" in allowed_risk_tiers
        max_standard_positions = int(
            os.getenv(
                "TRADER_MAX_STANDARD_POSITIONS",
                str(max(0, max_open_positions - 1) if paired_tiers else max_open_positions),
            )
        )
        max_high_risk_positions = int(
            os.getenv(
                "TRADER_MAX_HIGH_RISK_POSITIONS",
                "1" if paired_tiers else str(max_open_positions),
            )
        )
        settings = cls(
            database_url=database_url,
            mexc_base_url=base_url,
            mexc_ws_url=os.getenv("MEXC_WS_URL", "wss://contract.mexc.com/edge").strip(),
            trading_mode=os.getenv("TRADING_MODE", "paper").strip().lower(),
            margin_mode=os.getenv("TRADER_MARGIN_MODE", "cross").strip().lower(),
            legacy_position_maturity=os.getenv("TRADER_POSITION_MATURITY", "profit_20").strip().lower(),
            leverage=int(os.getenv("TRADER_LEVERAGE", "1")),
            allowed_risk_tiers=allowed_risk_tiers,
            max_open_positions=max_open_positions,
            slot_allocation_pct=slot_allocation_pct,
            max_total_exposure_pct=max_total_exposure_pct,
            max_standard_positions=max_standard_positions,
            max_high_risk_positions=max_high_risk_positions,
            standard_hold_days=int(os.getenv("TRADER_STANDARD_HOLD_DAYS", "7")),
            high_risk_timeout_days=int(os.getenv("TRADER_HIGH_RISK_TIMEOUT_DAYS", "4")),
            allow_same_symbol_parallel=_bool("TRADER_ALLOW_SAME_SYMBOL_PARALLEL", False),
            paper_starting_equity_usdt=float(os.getenv("PAPER_STARTING_EQUITY_USDT", "2000")),
            paper_taker_fee_rate=float(os.getenv("TRADER_PAPER_TAKER_FEE_RATE", "0.0008")),
            poll_seconds=int(os.getenv("TRADER_POLL_SECONDS", "5")),
            process_existing_signals=_bool("TRADER_PROCESS_EXISTING_SIGNALS", False),
            max_signal_age_seconds=int(os.getenv("TRADER_MAX_SIGNAL_AGE_SECONDS", "900")),
            profit_target_pct=float(os.getenv("TRADER_PROFIT_TARGET_PCT", "20")),
            protection_arm_pct=float(os.getenv("TRADER_PROTECTION_ARM_PCT", "25")),
            trail_callback_pct=float(os.getenv("TRADER_TRAIL_CALLBACK_PCT", "15")),
            protection_update_step_pct=float(os.getenv("TRADER_PROTECTION_UPDATE_STEP_PCT", "1")),
            protection_notify_step_pct=float(os.getenv("TRADER_PROTECTION_NOTIFY_STEP_PCT", "10")),
            mexc_api_key=os.getenv("MEXC_API_KEY") or None,
            mexc_api_secret=os.getenv("MEXC_API_SECRET") or None,
            mexc_live_order_api_enabled=_bool("MEXC_LIVE_ORDER_API_ENABLED", False),
            live_trading_confirm=os.getenv("LIVE_TRADING_CONFIRM", "").strip(),
            trader_events_webhook_url=webhook,
            heartbeat_seconds=int(os.getenv("TRADER_DISCORD_HEARTBEAT_SECONDS", "900")),
            error_alert_cooldown_seconds=int(os.getenv("TRADER_ERROR_ALERT_COOLDOWN_SECONDS", "300")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.trading_mode not in {"paper", "live"}:
            raise ValueError("TRADING_MODE must be paper or live")
        if self.margin_mode not in {"cross", "isolated"}:
            raise ValueError("TRADER_MARGIN_MODE must be cross or isolated")
        if self.legacy_position_maturity not in {"profit_20", "1d", "2d", "3d", "4d", "5d", "6d", "7d", "10d", "14d"}:
            raise ValueError("TRADER_POSITION_MATURITY legacy value is invalid")
        if self.leverage < 1:
            raise ValueError("TRADER_LEVERAGE must be >= 1")
        if not self.allowed_risk_tiers:
            raise ValueError("TRADER_ALLOWED_RISK_TIERS cannot be empty")
        unknown = set(self.allowed_risk_tiers) - {"STANDARD", "HIGH_RISK", "EXTREME_RISK"}
        if unknown:
            raise ValueError(f"unsupported risk tiers: {sorted(unknown)}")
        if self.max_open_positions < 1:
            raise ValueError("TRADER_MAX_OPEN_POSITIONS must be >= 1")
        if not 0 < self.slot_allocation_pct <= 100:
            raise ValueError("TRADER_SLOT_ALLOCATION_PCT must be in (0,100]")
        if not 0 < self.max_total_exposure_pct <= 100:
            raise ValueError("TRADER_MAX_TOTAL_EXPOSURE_PCT must be in (0,100]")
        if self.max_standard_positions < 0 or self.max_standard_positions > self.max_open_positions:
            raise ValueError("TRADER_MAX_STANDARD_POSITIONS must be between 0 and max slots")
        if self.max_high_risk_positions < 0 or self.max_high_risk_positions > self.max_open_positions:
            raise ValueError("TRADER_MAX_HIGH_RISK_POSITIONS must be between 0 and max slots")
        if "STANDARD" in self.allowed_risk_tiers and "HIGH_RISK" in self.allowed_risk_tiers:
            if self.max_standard_positions + self.max_high_risk_positions > self.max_open_positions:
                raise ValueError("STANDARD + HIGH_RISK tier capacities cannot exceed TRADER_MAX_OPEN_POSITIONS")
        supported_holds = {1, 2, 3, 4, 5, 6, 7, 10, 14}
        if self.standard_hold_days not in supported_holds:
            raise ValueError("TRADER_STANDARD_HOLD_DAYS must be one of 1,2,3,4,5,6,7,10,14")
        if self.high_risk_timeout_days not in supported_holds:
            raise ValueError("TRADER_HIGH_RISK_TIMEOUT_DAYS must be one of 1,2,3,4,5,6,7,10,14")
        if self.paper_starting_equity_usdt <= 0:
            raise ValueError("PAPER_STARTING_EQUITY_USDT must be positive")
        if self.paper_taker_fee_rate < 0 or self.paper_taker_fee_rate >= 0.02:
            raise ValueError("TRADER_PAPER_TAKER_FEE_RATE looks invalid")
        if self.poll_seconds < 1:
            raise ValueError("TRADER_POLL_SECONDS must be >= 1")
        if self.max_signal_age_seconds <= 0:
            raise ValueError("TRADER_MAX_SIGNAL_AGE_SECONDS must be positive")
        if not 0 < self.profit_target_pct < 100:
            raise ValueError("TRADER_PROFIT_TARGET_PCT must be between 0 and 100")
        if not self.profit_target_pct < self.protection_arm_pct < 100:
            raise ValueError("TRADER_PROTECTION_ARM_PCT must exceed target and be below 100")
        if not 0 < self.trail_callback_pct < 100:
            raise ValueError("TRADER_TRAIL_CALLBACK_PCT must be between 0 and 100")
        if self.protection_update_step_pct <= 0 or self.protection_notify_step_pct <= 0:
            raise ValueError("protection step settings must be positive")
        if self.heartbeat_seconds < 60:
            raise ValueError("TRADER_DISCORD_HEARTBEAT_SECONDS must be >= 60")
        if self.error_alert_cooldown_seconds < 60:
            raise ValueError("TRADER_ERROR_ALERT_COOLDOWN_SECONDS must be >= 60")
        if self.trading_mode == "live":
            if not self.mexc_api_key or not self.mexc_api_secret:
                raise RuntimeError("MEXC_API_KEY and MEXC_API_SECRET are required for live mode")
            if not self.mexc_live_order_api_enabled:
                raise RuntimeError(
                    "Live mode is fail-closed. Set MEXC_LIVE_ORDER_API_ENABLED=true only after "
                    "running trader_preflight successfully with a Futures-enabled API key."
                )
            if self.live_trading_confirm != "I_UNDERSTAND_LIVE_TRADING":
                raise RuntimeError(
                    "Set LIVE_TRADING_CONFIRM=I_UNDERSTAND_LIVE_TRADING to arm live execution"
                )

    @property
    def slot_fraction(self) -> float:
        return self.slot_allocation_pct / 100.0

    @property
    def max_total_exposure_fraction(self) -> float:
        return self.max_total_exposure_pct / 100.0

    @property
    def open_type(self) -> int:
        return 2 if self.margin_mode == "cross" else 1

    @property
    def capital_strategy_label(self) -> str:
        return "cross_portfolio" if self.margin_mode == "cross" else "isolated_portfolio"

    @property
    def capital_strategy(self) -> str:
        return "cross_20" if self.margin_mode == "cross" else "isolated_full"

    @property
    def position_maturity(self) -> str:
        return self.legacy_position_maturity

    @property
    def position_fraction(self) -> float:
        return self.max_total_exposure_fraction

    @property
    def liquidation_proxy_pct(self) -> float:
        return 400.0 if self.margin_mode == "cross" else 100.0

    @property
    def maturity_seconds(self) -> int | None:
        if self.legacy_position_maturity == "profit_20":
            return None
        return int(self.legacy_position_maturity.removesuffix("d")) * 86400
