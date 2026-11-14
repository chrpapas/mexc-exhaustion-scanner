"""Pure historical V40 sizing must not be mistaken for MEXC order risk."""
from decimal import Decimal as D

import pytest

from app.t100_v40_admission import PriorBar
from app.t100_v40_historical_margin_preview import (
    compare_historical_v40_lot_and_margin,
    V40HistoricalMarginPlanHalted,
)

ADMIT=1_998_000
BARS=tuple(
    PriorBar(
        open_ts=ADMIT-(49-i)*1800,
        high=100.,close=100.,amount_usdt=1_000_000.,
    ) for i in range(49)
)


def preview(atr=4.5, **changes):
    params=dict(
        prior_bars=BARS,admission_open_ts=ADMIT,
        equity_usdt=D("56.37671495"),
        marked_existing_usdt=D("0"),stage1_atr7_pct=atr,
        active_positions=0,already_open_symbol=False,
        used_same_bar_usdt=D("0"),entry_price=D("100"),
        contract_size="0.001",volume_step="1",min_volume="1",
        max_volume="100000",exchange_leverage_observed=1,
        pilot_notional_cap_usdt=D("12.50"),
    )
    params.update(changes)
    return compare_historical_v40_lot_and_margin(**params)


def test_low_tier_historical_target_already_exceeds_live_cap():
    result=preview()
    assert result.tier=="LOW"
    assert result.target_notional_usdt==D("14.0941787375")
    assert result.lot_quantized_notional_usdt==D("14.0")
    assert result.status=="V40_HISTORICAL_SIZE_EXCEEDS_PILOT_CAP"
    assert result.live_authorization is False
    assert result.live_order_allowed is False
    assert result.exchange_posts==0


def test_high_tier_gross_62point5pct_historical_target():
    result=preview(atr=5.8)
    assert result.tier=="HIGH"
    assert result.target_notional_usdt==D("35.23544684375")
    assert result.lot_quantized_notional_usdt==D("35.2")
    assert result.status=="V40_HISTORICAL_SIZE_EXCEEDS_PILOT_CAP"


def test_5x_is_research_shock_proxy_not_exchange_leverage_change():
    result=preview(exchange_leverage_observed=5,pilot_notional_cap_usdt=D("100"))
    assert result.historical_shock_leverage_proxy==5
    assert result.exchange_leverage_observed==5
    assert result.nominal_exchange_initial_margin_usdt==D("2.8")
    assert result.exchange_config_mutations==0
    assert result.live_order_allowed is False


def test_volatility_skip_never_produces_an_order():
    result=preview(already_open_symbol=True)
    assert result.admission_action=="REJECT"
    assert result.volume_contracts==0
    assert result.live_order_allowed is False


@pytest.mark.parametrize("change",[
    {"equity_usdt":D("NaN")},
    {"exchange_leverage_observed":0},
    {"pilot_notional_cap_usdt":D("-10")},
    {"entry_price":D("0")},
    {"active_positions":5},
    {"contract_size":"invalid"},
])
def test_bad_account_or_contract_shape_halts(change):
    with pytest.raises(V40HistoricalMarginPlanHalted):
        preview(**change)
