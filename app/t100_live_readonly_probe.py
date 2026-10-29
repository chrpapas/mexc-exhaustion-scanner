"""Strict read-only MEXC transport facade for contract discovery.

Does not assume exchange pagination schema or certify atomicity.
Uses only injected private GET capability; no POST, order, or stop mutations.
"""
from __future__ import annotations
from typing import Any, Protocol
import re


class VenueReadHalted(RuntimeError):
    pass


class PrivateGet(Protocol):
    async def _private_get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


class ReadOnlyMexcProbe:
    def __init__(self, client: PrivateGet):
        self._client = client

    async def raw_account_assets(self) -> list[dict[str, Any]]:
        try:
            raw = await self._client._private_get(
                "/api/v1/private/account/assets"
            )
        except Exception as exc:
            raise VenueReadHalted("ACCOUNT_ASSETS_GET_FAILED") from exc
        if (
            not isinstance(raw, list)
            or any(not isinstance(row, dict) for row in raw)
        ):
            raise VenueReadHalted("ACCOUNT_ASSETS_RESPONSE_UNVERIFIED")
        return raw

    async def raw_account_risk_limits(
        self, symbol: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Private GET-only CURRENT risk tiers; not future stressed-tier proof.

        MEXC documents data as symbol -> list[side-specific tier rows].
        This method validates collection shape and explicit symbol/side
        consistency, but DOES NOT certify future risk levels, liquidation
        fees, account equity, or permission to submit orders.
        """
        if (
            symbol is not None
            and (
                not isinstance(symbol, str)
                or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
            )
        ):
            raise VenueReadHalted("INVALID_RISK_LIMIT_SYMBOL_FILTER")
        try:
            raw = await self._client._private_get(
                "/api/v1/private/account/risk_limit",
                None if symbol is None else {"symbol": symbol},
            )
        except Exception:
            raise VenueReadHalted("RISK_LIMIT_GET_FAILED") from None
        if not isinstance(raw, dict) or not raw:
            raise VenueReadHalted("RISK_LIMIT_RESPONSE_UNVERIFIED")
        if symbol is not None and set(raw) != {symbol}:
            raise VenueReadHalted("RISK_LIMIT_SYMBOL_MISMATCH")
        for key, rows in raw.items():
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[A-Z0-9]+_USDT", key) is None
                or not isinstance(rows, list)
                or not rows
            ):
                raise VenueReadHalted("RISK_LIMIT_RESPONSE_UNVERIFIED")
            sides = set()
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("symbol") != key
                    or type(row.get("positionType")) is not int
                    or row["positionType"] not in (1, 2)
                    or type(row.get("level")) is not int
                    or row["level"] <= 0
                ):
                    raise VenueReadHalted("RISK_LIMIT_ROW_UNVERIFIED")
                side = row["positionType"]
                if side in sides:
                    raise VenueReadHalted("RISK_LIMIT_DUPLICATE_POSITION_SIDE")
                sides.add(side)
        return raw

    async def raw_open_positions(
        self, symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        if symbol is not None and re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None:
            raise VenueReadHalted("INVALID_POSITION_SYMBOL_FILTER")
        try:
            raw = await self._client._private_get(
                "/api/v1/private/position/open_positions",
                None if symbol is None else {"symbol": symbol},
            )
        except Exception as exc:
            raise VenueReadHalted("POSITIONS_GET_FAILED") from exc
        if not isinstance(raw, list) or any(not isinstance(p, dict) for p in raw):
            raise VenueReadHalted("POSITIONS_RESPONSE_UNVERIFIED")
        return raw

    async def raw_open_order_counts(self) -> dict[str, int]:
        """Read MEXC account-level in-flight order counts via GET only."""
        try:
            raw = await self._client._private_get(
                "/api/v1/private/order/open_order_total_count"
            )
        except Exception as exc:
            raise VenueReadHalted("OPEN_ORDER_COUNTS_GET_FAILED") from exc
        if not isinstance(raw, dict):
            raise VenueReadHalted("OPEN_ORDER_COUNTS_RESPONSE_UNVERIFIED")
        required = (
            "sumCount",
            "limitOrderCount",
            "stopOrderCount",
            "planOrderCount",
            "trackOrderCount",
        )
        result: dict[str, int] = {}
        for field in required:
            value = raw.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VenueReadHalted("OPEN_ORDER_COUNTS_RESPONSE_UNVERIFIED")
            result[field] = value
        if result["sumCount"] != sum(
            result[field]
            for field in required
            if field != "sumCount"
        ):
            raise VenueReadHalted("OPEN_ORDER_COUNTS_SUM_MISMATCH")
        return result

    async def raw_open_stops(
        self, symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        if symbol is not None and re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None:
            raise VenueReadHalted("INVALID_STOP_SYMBOL_FILTER")
        try:
            raw = await self._client._private_get(
                "/api/v1/private/stoporder/open_orders",
                None if symbol is None else {"symbol": symbol},
            )
        except Exception as exc:
            raise VenueReadHalted("OPEN_STOPS_GET_FAILED") from exc
        if not isinstance(raw, list) or any(not isinstance(s, dict) for s in raw):
            raise VenueReadHalted("OPEN_STOPS_RESPONSE_UNVERIFIED")
        return raw

    async def raw_stop_page(
        self, page: int, size: int, *, is_finished: int = 1,
    ) -> list[dict[str, Any]]:
        """Read one TP/SL page without claiming pagination completeness.

        Current MEXC documentation describes data as a list even though
        page_num/page_size are request parameters. Because the response
        does not report a total/page count, a short page is schema evidence
        only and must never be treated as proof that history is complete.
        """
        if (
            type(page) is not int or type(size) is not int
            or page < 1 or not 1 <= size <= 100
            or type(is_finished) is not int or is_finished not in {0, 1}
        ):
            raise VenueReadHalted("INVALID_STOP_PAGE_ARGUMENTS")
        try:
            raw = await self._client._private_get(
                "/api/v1/private/stoporder/list/orders",
                {
                    "page_num": page,
                    "page_size": size,
                    "is_finished": is_finished,
                },
            )
        except Exception as exc:
            raise VenueReadHalted("STOP_PAGE_GET_FAILED") from exc
        if not isinstance(raw, list) or any(not isinstance(s, dict) for s in raw):
            raise VenueReadHalted("STOP_PAGE_RESPONSE_UNVERIFIED")
        if len(raw) > size:
            raise VenueReadHalted("STOP_PAGE_OVERFLOW")
        return raw

    async def raw_funding_page(
        self, position_id: str, *, page: int, size: int,
    ) -> dict[str, Any]:
        """Read one funding-record page for an observed position.

        MEXC documents explicit pageSize/totalCount/totalPage/currentPage
        metadata for this endpoint. This method preserves the venue payload;
        completeness is established separately by the strict page collector.
        """
        if (
            not isinstance(position_id, str) or not position_id.isdigit()
            or int(position_id) <= 0
            or type(page) is not int or page < 1
            or type(size) is not int or not 1 <= size <= 100
        ):
            raise VenueReadHalted("INVALID_FUNDING_PAGE_ARGUMENTS")
        try:
            raw = await self._client._private_get(
                "/api/v1/private/position/funding_records",
                {
                    "position_id": int(position_id),
                    "page_num": page,
                    "page_size": size,
                },
            )
        except Exception as exc:
            raise VenueReadHalted("FUNDING_PAGE_GET_FAILED") from exc
        if not isinstance(raw, dict):
            raise VenueReadHalted("FUNDING_PAGE_RESPONSE_UNVERIFIED")
        rows = raw.get("resultList")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise VenueReadHalted("FUNDING_PAGE_ROWS_UNVERIFIED")
        return raw

    async def raw_order_by_id(self, order_id: str) -> dict[str, Any]:
        """Read one MEXC futures order by immutable exchange order ID."""
        if (
            not isinstance(order_id, str) or not order_id.isdigit()
            or int(order_id) <= 0
        ):
            raise VenueReadHalted("INVALID_ORDER_ID")
        try:
            raw = await self._client._private_get(
                f"/api/v1/private/order/get/{order_id}"
            )
        except Exception as exc:
            raise VenueReadHalted("ORDER_GET_FAILED") from exc
        if not isinstance(raw, dict):
            raise VenueReadHalted("ORDER_RESPONSE_UNVERIFIED")
        return raw

    async def raw_order_deal_details(
        self, order_id: str,
    ) -> list[dict[str, Any]]:
        """Read all trade records for one MEXC futures order ID."""
        if (
            not isinstance(order_id, str) or not order_id.isdigit()
            or int(order_id) <= 0
        ):
            raise VenueReadHalted("INVALID_ORDER_ID")
        try:
            raw = await self._client._private_get(
                f"/api/v1/private/order/deal_details/{order_id}"
            )
        except Exception as exc:
            raise VenueReadHalted("ORDER_DEALS_GET_FAILED") from exc
        if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
            raise VenueReadHalted("ORDER_DEALS_RESPONSE_UNVERIFIED")
        return raw

