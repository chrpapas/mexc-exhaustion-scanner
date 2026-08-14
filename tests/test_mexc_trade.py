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

from app.mexc_price_stream import ticker_price_from_message


def test_ticker_websocket_parser():
    message = '{"channel":"push.ticker","data":{"lastPrice":0.1234,"symbol":"VELVET_USDT"},"symbol":"VELVET_USDT"}'
    assert ticker_price_from_message(message, "VELVET_USDT") == pytest.approx(0.1234)


def test_ticker_websocket_parser_ignores_other_symbol():
    message = '{"channel":"push.ticker","data":{"lastPrice":0.1234,"symbol":"VELVET_USDT"},"symbol":"VELVET_USDT"}'
    assert ticker_price_from_message(message, "BTC_USDT") is None
