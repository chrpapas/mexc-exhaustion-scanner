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

@pytest.mark.asyncio
async def test_position_stop_payload_is_fixed_and_venue_unverified():
    """Regression fixture only: venue rejected this shape in a prior pilot.

    Current docs list these optional fields, but the combination is unverified.
    No venue network call is performed; the private POST is stubbed.
    """
    from unittest.mock import AsyncMock, patch

    from app.mexc_trade import MexcTradeClient

    client = MexcTradeClient("https://api.mexc.com", api_key="TEST", api_secret="TEST")
    try:
        with patch.object(client, "_private_post", new_callable=AsyncMock) as send:
            send.return_value = {"id": 2345}
            stop_id = await client.place_position_stop(
                position_id=1234, contracts=1, stop_price=95000.5,
            )
        assert stop_id == 2345
        send.assert_awaited_once_with(
            "/api/v1/private/stoporder/place",
            {
                "lossTrend": 1,
                "profitTrend": 1,
                "positionId": 1234,
                "vol": 1,
                "stopLossPrice": 95000.5,
                "priceProtect": 0,
                "profitLossVolType": "SAME",
                "volType": 2,
                "stopLossReverse": 2,
                "stopLossType": 0,
            },
        )
    finally:
        await client.close()
