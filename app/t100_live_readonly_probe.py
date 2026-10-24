"""Strict read-only MEXC transport facade for contract discovery.

Does not assume exchange pagination schema or certify atomicity.
Uses only injected private GET capability; no POST, order, or stop mutations.
"""
from __future__ import annotations
from typing import Any, Protocol


class VenueReadHalted(RuntimeError):
    pass


class PrivateGet(Protocol):
    async def _private_get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...


class ReadOnlyMexcProbe:
    def __init__(self, client: PrivateGet):
        self._client = client

    async def raw_open_positions(self) -> list[dict[str, Any]]:
        try:
            raw = await self._client._private_get(
                "/api/v1/private/position/open_positions"
            )
        except Exception as exc:
            raise VenueReadHalted("POSITIONS_GET_FAILED") from exc
        if not isinstance(raw, list) or any(not isinstance(p, dict) for p in raw):
            raise VenueReadHalted("POSITIONS_RESPONSE_UNVERIFIED")
        return raw

    async def raw_open_stops(self) -> list[dict[str, Any]]:
        try:
            raw = await self._client._private_get(
                "/api/v1/private/stoporder/open_orders"
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

