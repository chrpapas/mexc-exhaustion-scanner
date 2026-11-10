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

    async def raw_position_mode(self) -> int:
        """Signed GET: 1=hedge; 2=one-way. Unknown is NOT hedge.

        Accept only the documented positionMode field or the direct integer
        envelope data variant observed in client integrations. Never modify
        account configuration to force an expected mode.
        """
        try:
            data=await self._client._private_get(
                "/api/v1/private/position/position_mode"
            )
        except Exception:
            raise VenueReadHalted("POSITION_MODE_GET_FAILED") from None
        if type(data) is dict:
            if set(data)!={"positionMode"}:
                raise VenueReadHalted("POSITION_MODE_RESPONSE_UNVERIFIED")
            data=data["positionMode"]
        if type(data) is not int or data not in (1,2):
            raise VenueReadHalted("POSITION_MODE_RESPONSE_UNVERIFIED")
        return data

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

    async def raw_position_leverage(
        self, symbol: str,
    ) -> list[dict[str, Any]]:
        """Private signed GET of configured long/short leverage and margin.

        Unlike account/risk_limit, this is the documented dedicated
        position/leverage settings endpoint. It may omit 'symbol' from
        its result rows, because the request is scoped and the official
        example does. Never infer write permission, venue atomicity,
        future stressed-tier safety or authorization to change settings.
        """
        if (
            type(symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT",symbol) is None
        ):
            raise VenueReadHalted("INVALID_POSITION_LEVERAGE_SYMBOL")
        try:
            raw=await self._client._private_get(
                "/api/v1/private/position/leverage",{"symbol":symbol},
            )
        except Exception:
            raise VenueReadHalted("POSITION_LEVERAGE_GET_FAILED") from None
        if (
            type(raw) is not list or not 1<=len(raw)<=2
            or any(type(row) is not dict for row in raw)
        ):
            raise VenueReadHalted("POSITION_LEVERAGE_RESPONSE_UNVERIFIED")
        sides=set()
        for row in raw:
            side=row.get("positionType")
            if (
                type(side) is not int or side not in (1,2)
                or side in sides
                or ("symbol" in row and row["symbol"]!=symbol)
                or type(row.get("openType")) is not int
                or row["openType"] not in (1,2)
                or type(row.get("leverage")) is not int
                or not 1<=row["leverage"]<=1000
            ):
                raise VenueReadHalted("POSITION_LEVERAGE_ROW_UNVERIFIED")
            sides.add(side)
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

    async def raw_open_position_by_id(
        self, position_id: str,
    ) -> list[dict[str, Any]]:
        """Exact position-ID GET. Empty does not on its own prove closed."""
        if (type(position_id) is not str or
                re.fullmatch(r"[1-9][0-9]*", position_id) is None):
            raise VenueReadHalted("INVALID_EXACT_POSITION_ID")
        try:
            raw = await self._client._private_get(
                "/api/v1/private/position/open_positions",
                {"positionId": int(position_id)},
            )
        except Exception:
            raise VenueReadHalted("EXACT_OPEN_POSITION_GET_FAILED") from None
        if type(raw) is not list or any(type(row) is not dict for row in raw):
            raise VenueReadHalted("EXACT_OPEN_POSITION_RESPONSE_UNVERIFIED")
        if any(str(row.get("positionId")) != position_id for row in raw):
            raise VenueReadHalted("EXACT_OPEN_POSITION_FILTER_BROKEN")
        if len(raw) > 1:
            raise VenueReadHalted("EXACT_OPEN_POSITION_DUPLICATE")
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

    async def raw_history_positions_page(
        self, symbol: str, *, page: int, size: int,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Signed GET of ONE historical-position page. No completeness claim.

        The actual dedicated T100 MEXC subaccount (2026-11-06) returned
        data=[rows] without pagination metadata; the documentation describes
        data={resultList,totalCount,totalPage,...}. Both are accepted as
        *positive row evidence*. Only the metadata-object variant can provide
        a verifiable complete paginated census. A short bare array does NOT
        certify that all historical positions have been returned.
        """
        if (
            type(symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
            or type(page) is not int or page < 1
            or type(size) is not int or not 1 <= size <= 100
        ):
            raise VenueReadHalted("INVALID_HISTORY_POSITION_PAGE_ARGUMENTS")
        try:
            raw = await self._client._private_get(
                "/api/v1/private/position/list/history_positions",
                {"symbol": symbol, "page_num": page, "page_size": size},
            )
        except Exception:
            raise VenueReadHalted("HISTORY_POSITIONS_GET_FAILED") from None

        if type(raw) is list:
            if len(raw) > size or any(type(r) is not dict for r in raw):
                raise VenueReadHalted("HISTORY_POSITION_ARRAY_ROWS_UNVERIFIED")
            # A successful historical GET returning [] remains UNKNOWN
            # for any target missing from that page, not proven absent.
            return raw
        if type(raw) is not dict:
            raise VenueReadHalted("HISTORY_POSITIONS_RESPONSE_UNVERIFIED")
        rows = raw.get("resultList")
        if type(rows) is not list or any(type(r) is not dict for r in rows):
            raise VenueReadHalted("HISTORY_POSITIONS_ROWS_UNVERIFIED")
        keys = ("pageSize", "totalCount", "totalPage", "currentPage")
        if any(type(raw.get(key)) is not int for key in keys):
            raise VenueReadHalted("HISTORY_POSITIONS_METADATA_UNVERIFIED")
        if (raw["pageSize"] != size or raw["currentPage"] != page
            or raw["totalCount"] < 0 or raw["totalPage"] < 0
            or len(rows) > size):
            raise VenueReadHalted("HISTORY_POSITIONS_PAGINATION_CONTRADICTION")
        return raw

    async def raw_order_by_external_oid(
        self, symbol: str, external_oid: str,
    ) -> dict[str, Any] | None:
        """Exact immutable order origin lookup via GET only.

        Reject path-injection, unsupported symbols, empty/mismatched venue
        responses and unrecognized keys. Missing is UNKNOWN, never NO_FILL.
        Neither missing orders nor timeouts authorize a new order attempt.
        """
        if (
            type(symbol) is not str
            or re.fullmatch(r"[A-Z0-9]+_USDT", symbol) is None
            or type(external_oid) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", external_oid) is None
        ):
            raise VenueReadHalted("EXTERNAL_ORDER_IDENTITY_INVALID")
        try:
            raw = await self._client._private_get(
                f"/api/v1/private/order/external/{symbol}/{external_oid}"
            )
        except Exception:
            raise VenueReadHalted("EXTERNAL_ORDER_GET_FAILED") from None
        if raw is None:
            return None
        if type(raw) is not dict:
            raise VenueReadHalted("EXTERNAL_ORDER_RESPONSE_INVALID")
        if (
            raw.get("symbol") != symbol
            or raw.get("externalOid") != external_oid
            or not str(raw.get("orderId") or "").isdigit()
            or int(str(raw["orderId"])) <= 0
        ):
            raise VenueReadHalted("EXTERNAL_ORDER_IDENTITY_MISMATCH")
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

