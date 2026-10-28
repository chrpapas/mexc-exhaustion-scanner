"""No-POST MEXC cross-risk frame collector for the offline V40 witness.

Only inject a verified READ-ONLY private probe and an HTTP public GET client.
This module never constructs clients, reads credentials, issues a mutation,
starts Render, or regards an observed account position as T100-owned.

Each frame independently bookends account-wide positions/current TP-SL,
checks symbol-scoped redundancy, account order totals, USDT margin economics,
then obtains exact private risk limits, country-specific public contract info,
and the dedicated, TIMESTAMPED public FAIR price for each observed symbol.

Healthy evidence is CURRENT-ONLY and NONATOMIC. It does not prove full
cross-wallet collateral semantics, future liquidation tiers, protected
position ownership, available margin or live trading authorization.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from app.t100_live_consistency import CRITICAL_POSITION_FIELDS, CRITICAL_STOP_FIELDS
from app.t100_live_cross_risk_observation import CrossRiskRead
from app.t100_live_inventory_completeness import assess_current_inventory_completeness
from app.t100_live_position_margin_witness import assess_position_margin_witness
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_symbol_crosscheck import crosscheck_symbol_scoped_inventory

CONTRACT_COUNTRY = "/api/v1/contract/detail/country"
FAIR_PRICE_PREFIX = "/api/v1/contract/fair_price/"


class CrossRiskCollectionHalted(RuntimeError):
    pass


class PublicHttpGet(Protocol):
    async def get(self, path: str, *, params: dict[str, str] | None = None) -> Any: ...


def _canonical(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(json.dumps(
        {k: row[k] for k in fields if k in row},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) for row in rows))


def _positive(raw: Any) -> bool:
    if type(raw) is bool or raw is None:
        return False
    try:
        n = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return n.is_finite() and n > 0


class MexcReadOnlyCrossRiskCollector:
    def __init__(
        self, *, private: ReadOnlyMexcProbe, public: PublicHttpGet,
        clock_ms: Callable[[], int], max_frame_ms: int = 2500,
    ) -> None:
        if (
            not isinstance(private, ReadOnlyMexcProbe) or public is None
            or not callable(clock_ms)
            or type(max_frame_ms) is not int
            or not 0 < max_frame_ms <= 5000
        ):
            raise ValueError("CROSS_RISK_COLLECTOR_CONFIGURATION_INVALID")
        self.private = private
        self.public = public
        self.clock_ms = clock_ms
        self.max_frame_ms = max_frame_ms

    async def _public_data(
        self, path: str, *, symbol: str, params: dict[str, str] | None,
    ) -> dict[str, Any]:
        try:
            response = await self.public.get(path, params=params)
            response.raise_for_status()
            envelope = response.json()
        except Exception:
            raise CrossRiskCollectionHalted("PUBLIC_RISK_GET_FAILED") from None
        if (
            not isinstance(envelope, dict)
            or envelope.get("success") is not True
            or envelope.get("code") != 0
            or type(envelope.get("data")) is not dict
            or envelope["data"].get("symbol") != symbol
        ):
            raise CrossRiskCollectionHalted("PUBLIC_RISK_RESPONSE_UNVERIFIED")
        return envelope["data"]

    async def read(self) -> CrossRiskRead:
        stage = "CLOCK"
        try:
            started = self.clock_ms()
            if type(started) is not int:
                raise CrossRiskCollectionHalted("COLLECTOR_CLOCK_INVALID")
            stage = "PRIVATE_POSITIONS"
            before_positions = await self.private.raw_open_positions()
            stage = "PRIVATE_STOPS"
            before_stops = await self.private.raw_open_stops()
            if (
                not 1 <= len(before_positions) <= 4
                or len(before_stops) != len(before_positions)
            ):
                raise CrossRiskCollectionHalted("COLLECTOR_NONFLAT_ONE_TO_FOUR_REQUIRED")
            symbols = {p.get("symbol") for p in before_positions}
            if (
                None in symbols or len(symbols) != len(before_positions)
                or any(type(s) is not str or re.fullmatch(r"[A-Z0-9]+_USDT", s) is None
                       for s in symbols)
                or any(row.get("symbol") not in symbols for row in before_stops)
            ):
                raise CrossRiskCollectionHalted("COLLECTOR_POSITION_SYMBOLS_INVALID")

            stage = "SYMBOL_CROSSCHECK"
            redundant = await crosscheck_symbol_scoped_inventory(
                self.private, before_positions, before_stops, max_symbols=4,
            )
            stage = "ASSETS"
            assets = await self.private.raw_account_assets()
            stage = "MARGIN_WITNESS"
            margin = assess_position_margin_witness(before_positions, assets)
            stage = "ORDER_COUNTS"
            counts = await self.private.raw_open_order_counts()

            risks: dict[str, list[dict[str, Any]]] = {}
            contracts: dict[str, dict[str, Any]] = {}
            marks: dict[str, dict[str, Any]] = {}
            for symbol in sorted(symbols):
                stage = "PRIVATE_RISK"
                private = await self.private.raw_account_risk_limits(symbol)
                if set(private) != {symbol}:
                    raise CrossRiskCollectionHalted("COLLECTOR_PRIVATE_RISK_SCOPE_DRIFT")
                risks[symbol] = private[symbol]
                stage = "PUBLIC_CONTRACT"
                contracts[symbol] = await self._public_data(
                    CONTRACT_COUNTRY, symbol=symbol, params={"symbol": symbol},
                )
                stage = "PUBLIC_FAIR"
                fair = await self._public_data(
                    FAIR_PRICE_PREFIX + symbol, symbol=symbol, params=None,
                )
                ts = fair.get("timestamp")
                if (
                    type(ts) is not int
                    or ts < started - self.max_frame_ms
                    or ts > started + self.max_frame_ms
                    or not _positive(fair.get("fairPrice"))
                ):
                    raise CrossRiskCollectionHalted("COLLECTOR_FAIR_TIMESTAMP_OR_PRICE_INVALID")
                marks[symbol] = {
                    "fairPrice": fair["fairPrice"],
                    "observed_at_ms": ts,
                }

            stage = "POST_POSITIONS"
            after_positions = await self.private.raw_open_positions()
            stage = "POST_STOPS"
            after_stops = await self.private.raw_open_stops()
            stage = "FINAL_CLOCK"
            finished = self.clock_ms()
            if (
                type(finished) is not int
                or not 0 < finished - started <= self.max_frame_ms
                or any(
                    mark["observed_at_ms"] > finished + 100
                    or finished - mark["observed_at_ms"] > self.max_frame_ms
                    for mark in marks.values()
                )
            ):
                raise CrossRiskCollectionHalted("COLLECTOR_READ_WINDOW_STALE")
            stage = "CONSISTENCY"
            consistent = (
                _canonical(before_positions, CRITICAL_POSITION_FIELDS)
                == _canonical(after_positions, CRITICAL_POSITION_FIELDS)
                and _canonical(before_stops, CRITICAL_STOP_FIELDS)
                == _canonical(after_stops, CRITICAL_STOP_FIELDS)
            )
            stage = "CERTIFICATE"
            certificate = assess_current_inventory_completeness(
                positions=before_positions, stops=before_stops,
                open_order_counts=counts,
                critical_inventory_double_read_consistent=consistent,
                position_margin_witness=margin,
                symbol_scoped_crosscheck=redundant,
            )
            if (
                certificate.get("account_wide_completeness_verified") is not True
                or margin.get("position_inventory_economic_witness_observed") is not True
            ):
                raise CrossRiskCollectionHalted("COLLECTOR_INVENTORY_INCOMPLETE")
            # The documented all-account-assets GET has no continuation pages.
            # This flag is limited to the scope and semantics of that GET.
            return CrossRiskRead(
                observed_at_ms=finished,
                positions=after_positions, stops=after_stops, assets=assets,
                private_risk_by_symbol=risks,
                public_contract_by_symbol=contracts,
                fair_by_symbol=marks,
                positions_complete=True, stops_complete=True,
                assets_complete=True,
            )
        except CrossRiskCollectionHalted:
            raise
        except Exception:
            # Only fixed internal phase identifiers are exported: no URL,
            # symbol, account identifiers, balances or raw exception strings.
            raise CrossRiskCollectionHalted(
                "COLLECTOR_READ_FAILED_AT_" + stage
            ) from None
