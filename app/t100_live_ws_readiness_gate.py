"""Phase-specific private WebSocket readiness gates for T100.

MEXC has no sandbox environment. Therefore a real fill-event lifecycle cannot
be required before the first tiny live pilot without circularly requiring a
live fill first.

Tiny-pilot gate:
- successful read-only private WS login/filter
- no unexpected private channels
- semantic parser contract independently tested
- any naturally observed supported private events satisfy documented payload
  semantics

Autonomous-promotion gate:
- requires successful WS<->REST lifecycle correlation for position,
  TP/SL plan-order, and fill channels after real lifecycle evidence exists.
"""
from __future__ import annotations

from typing import Any

REQUIRED_LIFECYCLE_CHANNELS = frozenset({
    "push.personal.position",
    "push.personal.stop.planorder",
    "push.personal.order.deal",
})


def assess_ws_pre_pilot_contract(
    ws_report: Any,
    *,
    semantic_parser_contract_verified: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "private_ws_pre_pilot_contract_verified": False,
        "private_ws_full_lifecycle_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
    }
    if (
        not isinstance(ws_report, dict)
        or type(semantic_parser_contract_verified) is not bool
    ):
        result["status"] = "WS_PRE_PILOT_EVIDENCE_INVALID"
        return result

    unexpected = ws_report.get("unexpected_private_channels")
    if isinstance(unexpected, tuple):
        unexpected = list(unexpected)
    if not isinstance(unexpected, list):
        result["status"] = "WS_PRE_PILOT_UNEXPECTED_CHANNEL_REPORT_INVALID"
        return result

    events = ws_report.get("private_events_seen", 0)
    semantic_events = ws_report.get(
        "payload_semantic_events_verified", 0
    )
    semantic_failures = ws_report.get("payload_semantic_failures", 0)
    ints = (events, semantic_events, semantic_failures)
    if any(
        isinstance(x, bool) or not isinstance(x, int) or x < 0
        for x in ints
    ):
        result["status"] = "WS_PRE_PILOT_EVENT_COUNTS_INVALID"
        return result

    observed_events_ok = (
        events == 0
        or (
            semantic_events == events
            and semantic_failures == 0
        )
    )
    verified = all((
        semantic_parser_contract_verified,
        ws_report.get("read_only") is True,
        ws_report.get("login_verified") is True,
        ws_report.get("filter_sent") is True,
        ws_report.get("status")
        == "COMPLETED_PRIVATE_WS_OBSERVATION",
        len(unexpected) == 0,
        observed_events_ok,
    ))
    result["private_ws_pre_pilot_contract_verified"] = verified
    result["events_observed"] = events
    result["events_semantically_verified"] = semantic_events
    result["status"] = (
        "PRIVATE_WS_PRE_PILOT_CONTRACT_VERIFIED"
        if verified
        else "PRIVATE_WS_PRE_PILOT_CONTRACT_BLOCKED"
    )
    return result


def assess_ws_full_lifecycle_promotion(
    correlation_reports: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "private_ws_full_lifecycle_verified": False,
        "entry_allowed": False,
        "live_authorization": False,
    }
    if (
        not isinstance(correlation_reports, list)
        or any(not isinstance(x, dict) for x in correlation_reports)
    ):
        result["status"] = "WS_LIFECYCLE_CORRELATION_REPORT_INVALID"
        return result

    verified_channels: set[str] = set()
    failures = 0
    for report in correlation_reports:
        channel = report.get("channel")
        if channel not in REQUIRED_LIFECYCLE_CHANNELS:
            failures += 1
            continue
        if report.get("lifecycle_semantics_verified") is True:
            verified_channels.add(channel)
        else:
            failures += 1

    complete = (
        verified_channels == REQUIRED_LIFECYCLE_CHANNELS
        and failures == 0
    )
    result["verified_channel_count"] = len(verified_channels)
    result["required_channel_count"] = len(REQUIRED_LIFECYCLE_CHANNELS)
    result["correlation_failures"] = failures
    result["private_ws_full_lifecycle_verified"] = complete
    result["status"] = (
        "PRIVATE_WS_FULL_LIFECYCLE_VERIFIED"
        if complete
        else "PRIVATE_WS_FULL_LIFECYCLE_INCOMPLETE"
    )
    return result
