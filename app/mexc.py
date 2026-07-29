from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.models import Candle, Ticker


class MexcApiError(RuntimeError):
    """Raised when a MEXC public endpoint cannot be read safely."""


class AsyncRateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._minimum_interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            sleep_for = self._minimum_interval - (now - self._last_request)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            self._last_request = loop.time()


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_ticker(item: dict[str, Any], fallback_time: datetime | None = None) -> Ticker | None:
    symbol = str(item.get("symbol") or "").upper()
    last_price = _float(item.get("lastPrice"))
    if not symbol or last_price is None or last_price <= 0:
        return None
    fallback = fallback_time or datetime.now(UTC)
    timestamp_ms = item.get("timestamp")
    try:
        observed_at = datetime.fromtimestamp(int(timestamp_ms) / 1000, UTC) if timestamp_ms else fallback
    except (TypeError, ValueError, OSError):
        observed_at = fallback
    return Ticker(
        symbol=symbol,
        observed_at=observed_at,
        last_price=last_price,
        bid1=_float(item.get("bid1")),
        ask1=_float(item.get("ask1")),
        amount24=_float(item.get("amount24")) or 0.0,
        volume24=_float(item.get("volume24")) or 0.0,
        hold_vol=_float(item.get("holdVol")),
        low24=_float(item.get("lower24Price")),
        high24=_float(item.get("high24Price")),
        rise_fall_rate=_float(item.get("riseFallRate")) or 0.0,
        index_price=_float(item.get("indexPrice")),
        fair_price=_float(item.get("fairPrice")),
        funding_rate=_float(item.get("fundingRate")),
    )


def parse_klines(symbol: str, interval: str, data: dict[str, Any]) -> list[Candle]:
    arrays = [data.get(key) for key in ("time", "open", "high", "low", "close", "vol", "amount")]
    if not all(isinstance(array, list) for array in arrays):
        raise MexcApiError(f"Unexpected kline payload for {symbol} {interval}")
    lengths = {len(array) for array in arrays}
    if len(lengths) != 1:
        raise MexcApiError(f"Mismatched kline arrays for {symbol} {interval}")
    result: list[Candle] = []
    for values in zip(*arrays, strict=True):
        timestamp, open_, high, low, close, volume, amount = values
        parsed = [_float(value) for value in (open_, high, low, close, volume, amount)]
        if any(value is None for value in parsed):
            continue
        try:
            open_time = datetime.fromtimestamp(int(timestamp), UTC)
        except (TypeError, ValueError, OSError):
            continue
        result.append(
            Candle(
                symbol=symbol,
                interval=interval,
                open_time=open_time,
                open=parsed[0],  # type: ignore[arg-type]
                high=parsed[1],  # type: ignore[arg-type]
                low=parsed[2],  # type: ignore[arg-type]
                close=parsed[3],  # type: ignore[arg-type]
                volume=parsed[4],  # type: ignore[arg-type]
                amount=parsed[5],  # type: ignore[arg-type]
            )
        )
    return result


# A MEXC spot-pair requirement is the primary guard. These patterns are a second
# line of defence for obvious synthetic traditional-market and leveraged products.
_NON_CRYPTO_BASES = frozenset(
    {
        "UKOIL",
        "USOIL",
        "BRENT",
        "WTI",
        "NATGAS",
        "NGAS",
        "GOLD",
        "SILVER",
        "COPPER",
        "XAU",
        "XAG",
        "NAS100",
        "US100",
        "US30",
        "US500",
        "SPX500",
        "SP500",
        "DJI",
        "DOW",
        "DE40",
        "GER40",
        "UK100",
        "JP225",
        "HK50",
        "AUS200",
        "FRA40",
        "EU50",
        "VIX",
        "DXY",
        "NVIDIA",
        "NVDA",
        "TESLA",
        "TSLA",
        "APPLE",
        "AAPL",
        "AMAZON",
        "AMZN",
        "META",
        "GOOGLE",
        "GOOG",
        "GOOGL",
        "MICROSOFT",
        "MSFT",
        "COINBASE",
        "COIN",
        "SAMSUNGSTOCK",
        "QQQSTOCK",
        "SKHYSTOCK",
        "MUSTOCK",
    }
)
_LEVERAGED_TOKEN_RE = re.compile(r"(?:3L|3S|4L|4S|5L|5S|BULL|BEAR|UP|DOWN)$")
_SPOT_ASSET_ALIASES = {
    "FILECOIN": "FIL",
    "PUMPFUN": "PUMP",
}
_SCALE_PREFIXES = ("1000000", "100000", "10000", "1000")


def _has_matching_spot_asset(base: str, spot_usdt_assets: set[str] | frozenset[str]) -> bool:
    if base in spot_usdt_assets:
        return True
    alias = _SPOT_ASSET_ALIASES.get(base)
    if alias and alias in spot_usdt_assets:
        return True
    for prefix in _SCALE_PREFIXES:
        if base.startswith(prefix) and base.removeprefix(prefix) in spot_usdt_assets:
            return True
    return False


def is_crypto_usdt_contract(
    row: dict[str, Any],
    spot_usdt_assets: set[str] | frozenset[str],
    require_spot_pair: bool = True,
) -> bool:
    symbol = str(row.get("symbol") or "").upper()
    base = str(row.get("baseCoin") or "").upper()
    quote = str(row.get("quoteCoin") or "").upper()
    settle = str(row.get("settleCoin") or "").upper()
    display = " ".join(
        str(row.get(key) or "").upper() for key in ("displayName", "displayNameEn")
    )

    if not symbol or not base or quote != "USDT" or settle != "USDT":
        return False
    if int(row.get("state") or 0) != 0 or bool(row.get("isHidden", False)):
        return False
    if base in _NON_CRYPTO_BASES or base.endswith("STOCK") or symbol.startswith("STOCK_"):
        return False
    if any(marker in display for marker in (" STOCK", "INDEX", "OIL(BRENT)", "COMMODITY")):
        return False
    if _LEVERAGED_TOKEN_RE.search(base):
        return False
    if require_spot_pair and not _has_matching_spot_asset(base, spot_usdt_assets):
        return False
    return True


def parse_spot_usdt_assets(payload: dict[str, Any]) -> set[str]:
    rows = payload.get("symbols", [])
    if not isinstance(rows, list):
        raise MexcApiError("Unexpected spot exchangeInfo payload")
    assets: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        base = str(row.get("baseAsset") or "").upper()
        quote = str(row.get("quoteAsset") or "").upper()
        status = str(row.get("status") or "").upper()
        permissions = {str(value).upper() for value in row.get("permissions", []) if value is not None}
        online = status in {"1", "ENABLED"}
        if base and quote == "USDT" and online and (not permissions or "SPOT" in permissions):
            assets.add(base)
    return assets


INTERVAL_SECONDS = {
    "Min1": 60,
    "Min5": 300,
    "Min15": 900,
    "Min30": 1800,
    "Min60": 3600,
    "Hour4": 14_400,
    "Hour8": 28_800,
    "Day1": 86_400,
}


class MexcClient:
    def __init__(
        self,
        base_url: str,
        spot_base_url: str = "https://api.mexc.com",
        request_rate_per_second: float = 8.0,
        request_concurrency: int = 4,
    ) -> None:
        headers = {"User-Agent": "mexc-exhaustion-scanner/0.3"}
        timeout = httpx.Timeout(25.0)
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers)
        self._spot_client = httpx.AsyncClient(
            base_url=spot_base_url.rstrip("/"), timeout=timeout, headers=headers
        )
        self._limiter = AsyncRateLimiter(request_rate_per_second)
        self._semaphore = asyncio.Semaphore(request_concurrency)

    async def close(self) -> None:
        await asyncio.gather(self._client.aclose(), self._spot_client.aclose())

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, 5):
            try:
                async with self._semaphore:
                    await self._limiter.wait()
                    response = await client.get(path, params=params)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise MexcApiError(
                        f"MEXC error {payload.get('code')}: {payload.get('message') or payload.get('msg')}"
                    )
                return payload
            except (httpx.HTTPError, ValueError, MexcApiError) as exc:
                last_error = exc
                if attempt < 4:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
        raise MexcApiError(f"GET {path} failed after retries: {last_error}")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = await self._request_json(self._client, path, params)
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    async def get_contracts(self) -> list[dict[str, Any]]:
        data = await self._get("/api/v1/contract/detail")
        if not isinstance(data, list):
            raise MexcApiError("Unexpected contract/detail payload")
        return [row for row in data if isinstance(row, dict)]

    async def get_spot_usdt_assets(self) -> set[str]:
        payload = await self._request_json(self._spot_client, "/api/v3/exchangeInfo")
        if not isinstance(payload, dict):
            raise MexcApiError("Unexpected spot exchangeInfo payload")
        return parse_spot_usdt_assets(payload)

    async def get_tickers(self) -> list[Ticker]:
        data = await self._get("/api/v1/contract/ticker")
        rows = data if isinstance(data, list) else [data]
        fallback_time = datetime.now(UTC)
        parsed = [parse_ticker(row, fallback_time) for row in rows if isinstance(row, dict)]
        return [ticker for ticker in parsed if ticker is not None]

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_seconds: int,
        end_seconds: int | None = None,
    ) -> list[Candle]:
        interval_seconds = INTERVAL_SECONDS.get(interval)
        if interval_seconds is None:
            raise ValueError(f"Unsupported interval: {interval}")
        end = end_seconds or int(datetime.now(UTC).timestamp())
        cursor = max(0, start_seconds)
        rows_by_time: dict[datetime, Candle] = {}
        maximum_window = interval_seconds * 1_900
        while cursor <= end:
            window_end = min(end, cursor + maximum_window)
            data = await self._get(
                f"/api/v1/contract/kline/{symbol}",
                params={"interval": interval, "start": cursor, "end": window_end},
            )
            if not isinstance(data, dict):
                raise MexcApiError(f"Unexpected kline payload for {symbol} {interval}")
            batch = parse_klines(symbol, interval, data)
            for candle in batch:
                rows_by_time[candle.open_time] = candle
            if window_end >= end:
                break
            cursor = window_end + interval_seconds
        return [rows_by_time[key] for key in sorted(rows_by_time)]

    async def get_funding_history(self, symbol: str, page_size: int = 1000) -> list[dict[str, Any]]:
        data = await self._get(
            "/api/v1/contract/funding_rate/history",
            params={"symbol": symbol, "page_num": 1, "page_size": page_size},
        )
        if not isinstance(data, dict):
            raise MexcApiError(f"Unexpected funding payload for {symbol}")
        rows = data.get("resultList", [])
        return [row for row in rows if isinstance(row, dict)]
