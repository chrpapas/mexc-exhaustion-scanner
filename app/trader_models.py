from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class TradeSignal:
    id: int
    symbol: str
    signaled_at: datetime
    episode_id: int | None
    entry_hint: float | None
    risk_tier: str
    features: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TraderPosition:
    id: int
    signal_id: int
    symbol: str
    risk_tier: str
    slot_no: int | None
    mode: str
    capital_strategy: str
    exit_strategy: str
    position_maturity: str
    status: str
    opened_at: datetime
    entry_price: float
    entry_equity_usdt: float
    notional_usdt: float
    quantity_base: float
    current_price: float
    current_return_pct: float
    peak_profit_pct: float
    max_adverse_pct: float
    profit_floor_pct: float | None
    liquidation_proxy_pct: float
    target_20_at: datetime | None
    protection_armed_at: datetime | None
    mexc_protection_order_id: int | None
    breach_100_at: datetime | None
    breach_200_at: datetime | None
    breach_300_at: datetime | None
    breach_400_at: datetime | None
    entry_fee_usdt: float
    exit_fee_usdt: float
    mexc_position_id: int | None
    mexc_open_order_id: int | None
    metadata: dict[str, Any]
    run_id: str = "legacy_pre_v136"
