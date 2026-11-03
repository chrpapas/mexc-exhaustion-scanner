"""Read-only MEXC private WS -> REST lifecycle correlation.

Consumes semantically valid private WS events and immediately reads the
authoritative REST endpoint needed to corroborate that exact event identity.
Reports are redacted to channel/count/status booleans.

No exchange mutation and no database write occurs.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.t100_live_close_provenance import find_finished_stop_by_id
from app.t100_live_private_ws import observe_private_ws
from app.t100_live_readonly_probe import ReadOnlyMexcProbe
from app.t100_live_ws_readiness_gate import (
    assess_ws_full_lifecycle_promotion,
    assess_ws_pre_pilot_contract,
)
from app.t100_live_ws_semantics import (
    FILL_CHANNEL,
    POSITION_CHANNEL,
    STOP_CHANNEL,
    assess_private_ws_rest_correlation,
)


class WsRestCorrelationHalted(RuntimeError):
    pass


class PrivateWsRestCorrelator:
    def __init__(self, probe: ReadOnlyMexcProbe):
        self.probe = probe
        self.reports: list[dict[str, Any]] = []
        self.observed = {
            POSITION_CHANNEL: 0,
            STOP_CHANNEL: 0,
            FILL_CHANNEL: 0,
        }
        self.correlated = {
            POSITION_CHANNEL: 0,
            STOP_CHANNEL: 0,
            FILL_CHANNEL: 0,
        }
        self.failed = {
            POSITION_CHANNEL: 0,
            STOP_CHANNEL: 0,
            FILL_CHANNEL: 0,
        }

    async def __call__(
        self,
        channel: str,
        data: dict[str, Any],
        envelope_ts: Any | None,
    ) -> None:
        if channel not in self.observed:
            raise WsRestCorrelationHalted(
                "WS_CORRELATION_CHANNEL_UNSUPPORTED"
            )
        self.observed[channel] += 1

        try:
            rest_row = await self._read_matching_rest_row(channel, data)
            report = assess_private_ws_rest_correlation(
                channel,
                data,
                rest_row,
            )
        except Exception:
            self.failed[channel] += 1
            self.reports.append({
                "channel": channel,
                "lifecycle_semantics_verified": False,
                "status": "WS_REST_CORRELATION_READ_OR_MATCH_FAILED",
            })
            return

        redacted = {
            "channel": channel,
            "lifecycle_semantics_verified": (
                report.get("lifecycle_semantics_verified") is True
            ),
            "status": report.get("status"),
        }
        self.reports.append(redacted)
        if redacted["lifecycle_semantics_verified"]:
            self.correlated[channel] += 1
        else:
            self.failed[channel] += 1

    async def _read_matching_rest_row(
        self,
        channel: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        symbol = data.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise WsRestCorrelationHalted(
                "WS_CORRELATION_SYMBOL_MISSING"
            )

        if channel == POSITION_CHANNEL:
            # Pre-pilot / entry-path correlation is defined for a position
            # still present in current inventory. Closed-position events need
            # a separately certified historical-position reader.
            if data.get("state") == 3:
                raise WsRestCorrelationHalted(
                    "CLOSED_POSITION_HISTORY_CORRELATION_NOT_IMPLEMENTED"
                )
            rows = await self.probe.raw_open_positions(symbol)
            pid = str(data.get("positionId") or "")
            matches = [
                row for row in rows
                if str(row.get("positionId") or row.get("id") or "")
                == pid
            ]
        elif channel == STOP_CHANNEL:
            stop_id = str(data.get("id") or "")
            if not stop_id:
                raise WsRestCorrelationHalted(
                    "WS_CORRELATION_STOP_ID_MISSING"
                )
            if data.get("isFinished") == 1:
                return await find_finished_stop_by_id(
                    self.probe,
                    stop_order_id=stop_id,
                )
            rows = await self.probe.raw_open_stops(symbol)
            matches = [
                row for row in rows
                if str(
                    row.get("id")
                    or row.get("stopPlanOrderId")
                    or ""
                ) == stop_id
            ]
        else:
            order_id = str(data.get("orderId") or "")
            fill_id = str(data.get("id") or "")
            rows = await self.probe.raw_order_deal_details(order_id)
            matches = [
                row for row in rows
                if str(row.get("id") or "") == fill_id
            ]

        if len(matches) != 1:
            raise WsRestCorrelationHalted(
                "WS_CORRELATION_REST_ROW_NOT_UNIQUE"
            )
        return matches[0]

    def shareable(self) -> dict[str, Any]:
        promotion = assess_ws_full_lifecycle_promotion(self.reports)
        return {
            "position_events_observed": self.observed[POSITION_CHANNEL],
            "stop_events_observed": self.observed[STOP_CHANNEL],
            "fill_events_observed": self.observed[FILL_CHANNEL],
            "position_events_correlated": self.correlated[POSITION_CHANNEL],
            "stop_events_correlated": self.correlated[STOP_CHANNEL],
            "fill_events_correlated": self.correlated[FILL_CHANNEL],
            "position_correlation_failures": self.failed[POSITION_CHANNEL],
            "stop_correlation_failures": self.failed[STOP_CHANNEL],
            "fill_correlation_failures": self.failed[FILL_CHANNEL],
            "private_ws_full_lifecycle_verified": promotion[
                "private_ws_full_lifecycle_verified"
            ],
            "live_authorization": False,
        }


async def run_ws_rest_lifecycle_probe(
    *,
    client: Any,
    api_key: str,
    api_secret: str,
    duration_seconds: float = 30.0,
    connect: Any | None = None,
) -> dict[str, Any]:
    """Observe real private WS and correlate natural events by signed REST GET."""
    probe = ReadOnlyMexcProbe(client)
    correlator = PrivateWsRestCorrelator(probe)
    report = await observe_private_ws(
        api_key=api_key,
        api_secret=api_secret,
        duration_seconds=duration_seconds,
        connect=connect,
        on_private_event=correlator,
    )
    ws = asdict(report)
    pre_pilot = assess_ws_pre_pilot_contract(
        ws,
        semantic_parser_contract_verified=True,
    )
    return {
        "read_only": True,
        "live_order_submission_possible": False,
        "ws_observation": ws,
        "ws_rest_correlation": correlator.shareable(),
        "private_ws_pre_pilot_contract_verified": pre_pilot[
            "private_ws_pre_pilot_contract_verified"
        ],
        "private_ws_full_lifecycle_verified": correlator.shareable()[
            "private_ws_full_lifecycle_verified"
        ],
        "entry_allowed": False,
        "live_authorization": False,
    }
