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
    mexc_position_id: int | None
    mexc_open_order_id: int | None
    metadata: dict[str, Any]
