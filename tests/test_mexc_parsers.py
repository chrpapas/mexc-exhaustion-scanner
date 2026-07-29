from datetime import UTC, datetime

from app.mexc import (
    is_crypto_usdt_contract,
    parse_klines,
    parse_spot_usdt_assets,
    parse_ticker,
)


def test_parse_ticker() -> None:
    ticker = parse_ticker(
        {
            "symbol": "ABC_USDT",
            "lastPrice": 2.5,
            "bid1": 2.49,
            "ask1": 2.51,
            "amount24": 15_000_000,
            "volume24": 7_000_000,
            "holdVol": 123_000,
            "riseFallRate": 0.25,
            "timestamp": 1_700_000_000_000,
        }
    )
    assert ticker is not None
    assert ticker.symbol == "ABC_USDT"
    assert ticker.spread_pct is not None
    assert ticker.observed_at.tzinfo is UTC


def test_parse_klines() -> None:
    rows = parse_klines(
        "ABC_USDT",
        "Min15",
        {
            "time": [1_700_000_000],
            "open": [1.0],
            "high": [1.2],
            "low": [0.9],
            "close": [1.1],
            "vol": [1000],
            "amount": [1100],
        },
    )
    assert len(rows) == 1
    assert rows[0].open_time == datetime.fromtimestamp(1_700_000_000, UTC)
    assert rows[0].close == 1.1


def test_parse_spot_usdt_assets() -> None:
    assets = parse_spot_usdt_assets(
        {
            "symbols": [
                {
                    "status": "ENABLED",
                    "baseAsset": "ABC",
                    "quoteAsset": "USDT",
                    "permissions": ["SPOT"],
                },
                {
                    "status": "1",
                    "baseAsset": "XYZ",
                    "quoteAsset": "USDT",
                    "permissions": ["SPOT"],
                },
                {
                    "status": "DISABLED",
                    "baseAsset": "OLD",
                    "quoteAsset": "USDT",
                    "permissions": ["SPOT"],
                },
                {
                    "status": "ENABLED",
                    "baseAsset": "ABC",
                    "quoteAsset": "BTC",
                    "permissions": ["SPOT"],
                },
            ]
        }
    )
    assert assets == {"ABC", "XYZ"}


def _contract(base: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": f"{base}_USDT",
        "baseCoin": base,
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "state": 0,
        "isHidden": False,
        "displayNameEn": f"{base}_USDT SWAP",
    }
    row.update(overrides)
    return row


def test_crypto_filter_accepts_spot_backed_altcoin() -> None:
    assert is_crypto_usdt_contract(_contract("PENGU"), {"PENGU", "BTC"})


def test_crypto_filter_rejects_synthetic_markets() -> None:
    spot = {"BTC", "NVIDIA", "QQQSTOCK", "UKOIL", "NAS100"}
    assert not is_crypto_usdt_contract(_contract("NVIDIA"), spot)
    assert not is_crypto_usdt_contract(_contract("QQQSTOCK"), spot)
    assert not is_crypto_usdt_contract(_contract("UKOIL"), spot)
    assert not is_crypto_usdt_contract(_contract("NAS100"), spot)


def test_crypto_filter_rejects_futures_only_and_leveraged_products() -> None:
    assert not is_crypto_usdt_contract(_contract("UNKNOWN"), {"BTC"})
    assert not is_crypto_usdt_contract(_contract("BTC3L"), {"BTC3L"})


def test_crypto_filter_accepts_scaled_and_aliased_crypto_contracts() -> None:
    assert is_crypto_usdt_contract(_contract("1000PEPE"), {"PEPE"})
    assert is_crypto_usdt_contract(_contract("FILECOIN"), {"FIL"})
    assert is_crypto_usdt_contract(_contract("PUMPFUN"), {"PUMP"})
