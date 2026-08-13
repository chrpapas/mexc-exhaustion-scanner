import pytest

from app.mexc_trade import ContractSpec, MexcTradeError


def test_contract_volume_rounds_down_to_exchange_unit():
    spec = ContractSpec(
        symbol="ABC_USDT",
        contract_size=0.1,
        vol_unit=1,
        min_vol=1,
        max_vol=100000,
        api_allowed=True,
        position_open_type=3,
    )
    assert spec.contracts_for_notional(400, 2.0) == 2000


def test_contract_volume_rejects_below_minimum():
    spec = ContractSpec(
        symbol="ABC_USDT",
        contract_size=10,
        vol_unit=1,
        min_vol=10,
        max_vol=100000,
        api_allowed=True,
        position_open_type=3,
    )
    with pytest.raises(MexcTradeError):
        spec.contracts_for_notional(10, 2.0)
