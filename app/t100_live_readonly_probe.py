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

    async def raw_stop_page(self, page: int, size: int) -> Any:
        if type(page) is not int or type(size) is not int or page < 1 or not 1 <= size <= 100:
            raise VenueReadHalted("INVALID_STOP_PAGE_ARGUMENTS")
        try:
            raw = await self._client._private_get(
                "/api/v1/private/stoporder/list/orders",
                {"page_num": page, "page_size": size, "is_finished": 0},
            )
        except Exception as exc:
            raise VenueReadHalted("STOP_PAGE_GET_FAILED") from exc
        if not isinstance(raw, dict):
            raise VenueReadHalted("STOP_PAGE_RESPONSE_UNVERIFIED")
        # Deliberately do not rename/guess response fields. Contract
        # mapping requires verified, redacted MEXC response evidence.
        return raw
