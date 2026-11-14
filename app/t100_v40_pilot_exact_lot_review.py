"""Pure V40 tiny-pilot exact-lot feasibility review; no exchange or database.

The frozen strategy's target is NOT the pilot's executable quantity. This
replay-only calculator applies a separately bounded pilot ceiling before
rounding down onto the observed contract lot grid. It never increases size
to reach exchange minimum, changes the strategy target, certifies live
collateral/liquidation or substitutes for an exact signed MEXC depth quote.

The caller must independently source terms from fresh MEXC GETs and enforce
daily loss, CROSS 1x, private permissions, full venue inventory and real
market-stop protection. No private API, SQL, orders or config in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation
from typing import Any

from app.t100_live_execution import EntryIntent, modeled_short_loss_at_native_stop


@dataclass(frozen=True)
class V40PilotLotReview:
    status: str
    signal_id: int
    symbol: str
    capped_target_usdt: D = D(0)
    executable_notional_usdt: D = D(0)
    executable_contracts: D = D(0)
    modeled_stop_loss_usdt: D = D(0)
    exact_depth_verified: bool = False
    exchange_market_stop_verified: bool = False
    entry_allowed: bool = False
    live_authorization: bool = False
    mexc_orders_sent: int = 0
    database_writes: int = 0


def _n(value: Any, *, allow_zero: bool = False) -> D | None:
    if type(value) is bool:
        return None
    try:
        n = D(str(value))
    except (TypeError,ValueError,InvalidOperation):
        return None
    return n if n.is_finite() and (n >= 0 if allow_zero else n > 0) else None


def review_v40_pilot_exact_lot(
    *, signal_id: int, symbol: str, reference_price: Any,
    frozen_modeled_notional_usdt: Any, equity_usdt: Any,
    available_usdt: Any, contract_size: Any, volume_step: Any,
    min_volume: Any, max_volume: Any, price_tick: Any,
    max_order_usdt: Any = "12.50",
    max_equity_fraction: Any = "0.25",
    max_daily_loss_usdt: Any = "12.50",
    realized_today_loss_usdt: Any = "0",
    max_slippage_bps: Any = "100",
) -> V40PilotLotReview:
    """Return an explainable, *non-authorizing* pilot sized-lot decision.

    This models a 75% adverse SHORT stop, not guaranteed loss. A future
    executor must compute exact venue loss/liquidation risk independently.
    """
    def halt(code: str) -> V40PilotLotReview:
        return V40PilotLotReview(code,signal_id,symbol)
    values = {
        k:_n(v, allow_zero=k in ("realized_today_loss_usdt","max_slippage_bps"))
        for k,v in locals().items() if k in {
            "reference_price","frozen_modeled_notional_usdt",
            "equity_usdt","available_usdt","contract_size",
            "volume_step","min_volume","max_volume","price_tick",
            "max_order_usdt","max_equity_fraction","max_daily_loss_usdt",
            "realized_today_loss_usdt","max_slippage_bps",
        }
    }
    if (
        type(signal_id) is not int or signal_id<=0
        or type(symbol) is not str or not symbol.endswith("_USDT")
        or symbol in ("BTC_USDT","ETH_USDT")
        or any(v is None for v in values.values())
        or len(values)!=14
    ):
        return halt("V40_PILOT_LOT_INPUTS_INVALID")
    v=values
    if (
        v["max_equity_fraction"] > D("0.25")
        or v["max_order_usdt"] > D("12.50")
        or v["available_usdt"] > v["equity_usdt"]+D("0.00000001")
        or v["max_slippage_bps"] > D("300")
        or v["realized_today_loss_usdt"] >= v["max_daily_loss_usdt"]
    ):
        return halt("V40_PILOT_LOT_RISK_BOUNDARY_INVALID")
    target=min(
        v["frozen_modeled_notional_usdt"],
        v["max_order_usdt"],
        v["equity_usdt"] * v["max_equity_fraction"],
        v["available_usdt"],
    )
    if target<=0:
        return halt("V40_PILOT_LOT_NOTIONAL_UNAVAILABLE")
    try:
        intent=EntryIntent.build(
            signal_id=signal_id,symbol=symbol,
            reference_price=v["reference_price"],notional_usdt=target,
            contract_size=v["contract_size"],
            volume_step=v["volume_step"],
            min_volume=v["min_volume"],max_volume=v["max_volume"],
            price_tick=v["price_tick"],
        )
        stop_loss=modeled_short_loss_at_native_stop(
            intent,max_adverse_slippage_bps=v["max_slippage_bps"],
        )
    except (ValueError,ArithmeticError,OverflowError):
        return halt("V40_PILOT_LOT_EXCHANGE_MINIMUM_OR_GRID_REJECTED")
    except Exception:
        return halt("V40_PILOT_LOT_LOSS_MODEL_UNVERIFIED")
    if (
        intent.notional_usdt > target
        or intent.notional_usdt > v["frozen_modeled_notional_usdt"]
        or intent.notional_usdt > v["max_order_usdt"]
    ):
        return halt("V40_PILOT_LOT_QUANTIZATION_EXCEEDED_CEILING")
    if stop_loss + v["realized_today_loss_usdt"] >= v["max_daily_loss_usdt"]:
        return V40PilotLotReview(
            "V40_PILOT_LOT_MODELED_STOP_LOSS_EXCEEDS_DAILY_LIMIT",
            signal_id,symbol,capped_target_usdt=target,
            executable_notional_usdt=intent.notional_usdt,
            executable_contracts=intent.volume_contracts,
            modeled_stop_loss_usdt=stop_loss,
        )
    return V40PilotLotReview(
        "V40_PILOT_LOT_FEASIBLE_NONAUTHORIZING",
        signal_id,symbol,capped_target_usdt=target,
        executable_notional_usdt=intent.notional_usdt,
        executable_contracts=intent.volume_contracts,
        modeled_stop_loss_usdt=stop_loss,
    )
