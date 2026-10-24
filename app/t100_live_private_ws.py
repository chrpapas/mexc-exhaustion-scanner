"""Bounded read-only MEXC Futures private WebSocket observer.

Authenticates and subscribes only to private account push channels:
position, stop.planorder and order.deal.

No REST/WS order submission, cancellation, stop modification or database
writes. Output is redacted to counts, channel names, field names/types and
timing diagnostics; no symbols, IDs, prices, quantities, fees or credentials.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable



WS_URL = "wss://contract.mexc.com/edge"
ALLOWED_CHANNELS = frozenset({
    "push.personal.position",
    "push.personal.stop.planorder",
    "push.personal.order.deal",
})
FILTER_KEYS = ("position", "stop.planorder", "order.deal")


class PrivateWsHalted(RuntimeError):
    pass


def ws_login_signature(api_key: str, api_secret: str, req_time_ms: str) -> str:
    if not api_key or not api_secret or not req_time_ms.isdigit():
        raise ValueError("INVALID_WS_LOGIN_INPUT")
    target = f"{api_key}{req_time_ms}".encode()
    return hmac.new(
        api_secret.encode(), target, hashlib.sha256
    ).hexdigest()


def _decode_message(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            pass
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PrivateWsHalted("WS_NON_UTF8_MESSAGE") from exc
    if not isinstance(raw, str):
        raise PrivateWsHalted("WS_MESSAGE_TYPE_INVALID")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PrivateWsHalted("WS_MESSAGE_JSON_INVALID") from exc
    if not isinstance(payload, dict):
        raise PrivateWsHalted("WS_MESSAGE_OBJECT_REQUIRED")
    return payload


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "other"


@dataclass
class ChannelEvidence:
    count: int = 0
    field_types: dict[str, set[str]] = field(default_factory=dict)

    def observe(self, data: Any) -> None:
        if not isinstance(data, dict):
            raise PrivateWsHalted("WS_PRIVATE_DATA_OBJECT_REQUIRED")
        self.count += 1
        for key, value in data.items():
            if not isinstance(key, str):
                raise PrivateWsHalted("WS_PRIVATE_FIELD_NAME_INVALID")
            self.field_types.setdefault(key, set()).add(_kind(value))

    def shareable(self) -> dict[str, Any]:
        return {
            "events_seen": self.count,
            "field_types": {
                key: sorted(values)
                for key, values in sorted(self.field_types.items())
            },
        }


@dataclass(frozen=True)
class PrivateWsReport:
    login_verified: bool
    filter_sent: bool
    pongs_seen: int
    private_events_seen: int
    position_events_seen: int
    stop_plan_events_seen: int
    fill_events_seen: int
    unexpected_private_channels: tuple[str, ...]
    channel_schema: dict[str, Any]
    elapsed_ms: int
    status: str
    read_only: bool = True
    entry_allowed: bool = False
    live_authorization: bool = False


async def observe_private_ws(
    *,
    api_key: str,
    api_secret: str,
    duration_seconds: float = 30.0,
    ws_url: str = WS_URL,
    connect: Callable[..., Any] | None = None,
    wall_time_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> PrivateWsReport:
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not 1 <= float(duration_seconds) <= 300
    ):
        raise ValueError("INVALID_WS_OBSERVATION_DURATION")
    if not api_key or not api_secret:
        raise PrivateWsHalted("WS_CREDENTIALS_MISSING")
    if not isinstance(ws_url, str) or not ws_url.startswith("wss://"):
        raise ValueError("INVALID_WS_URL")

    if connect is None:
        try:
            import websockets
        except ImportError as exc:
            raise PrivateWsHalted("WS_RUNTIME_DEPENDENCY_MISSING") from exc
        connect = websockets.connect

    start = monotonic_ns()
    login_verified = False
    filter_sent = False
    pongs = 0
    unexpected: set[str] = set()
    evidence = {channel: ChannelEvidence() for channel in ALLOWED_CHANNELS}

    async def _observe() -> None:
        nonlocal login_verified, filter_sent, pongs
        async with connect(
            ws_url,
            ping_interval=None,
            open_timeout=10,
            close_timeout=5,
            max_size=2_000_000,
        ) as ws:
            req_time = str(wall_time_ms())
            login = {
                "method": "login",
                "param": {
                    "apiKey": api_key,
                    "reqTime": req_time,
                    "signature": ws_login_signature(
                        api_key, api_secret, req_time
                    ),
                    "subscribe": False,
                },
            }
            await ws.send(json.dumps(login, separators=(",", ":")))

            deadline = asyncio.get_running_loop().time() + float(
                duration_seconds
            )
            next_ping = asyncio.get_running_loop().time() + 15.0

            while True:
                now = asyncio.get_running_loop().time()
                if now >= deadline:
                    return
                if now >= next_ping:
                    await ws.send('{"method":"ping"}')
                    next_ping = now + 15.0

                timeout = min(
                    max(0.01, deadline - now),
                    max(0.01, next_ping - now),
                )
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue

                message = _decode_message(raw)
                channel = message.get("channel")
                if channel == "rs.error":
                    raise PrivateWsHalted("WS_SERVER_ERROR")
                if channel == "rs.login":
                    if message.get("data") != "success":
                        raise PrivateWsHalted("WS_LOGIN_REJECTED")
                    login_verified = True
                    if not filter_sent:
                        await ws.send(json.dumps(
                            {
                                "method": "personal.filter",
                                "param": {
                                    "filters": [
                                        {"filter": key}
                                        for key in FILTER_KEYS
                                    ]
                                },
                            },
                            separators=(",", ":"),
                        ))
                        filter_sent = True
                    continue
                if channel == "pong":
                    pongs += 1
                    continue

                if isinstance(channel, str) and channel.startswith(
                    "push.personal."
                ):
                    if channel not in ALLOWED_CHANNELS:
                        unexpected.add(channel)
                        continue
                    if not login_verified or not filter_sent:
                        raise PrivateWsHalted(
                            "WS_PRIVATE_EVENT_BEFORE_AUTH_FILTER"
                        )
                    evidence[channel].observe(message.get("data"))

    try:
        await asyncio.wait_for(
            _observe(),
            timeout=float(duration_seconds) + 15.0,
        )
    except asyncio.TimeoutError as exc:
        raise PrivateWsHalted("WS_OBSERVER_TIMEOUT") from exc

    elapsed_ms = max(0, (monotonic_ns() - start) // 1_000_000)
    if not login_verified:
        raise PrivateWsHalted("WS_LOGIN_NOT_VERIFIED")
    if not filter_sent:
        raise PrivateWsHalted("WS_FILTER_NOT_SENT")

    position_count = evidence["push.personal.position"].count
    stop_count = evidence["push.personal.stop.planorder"].count
    fill_count = evidence["push.personal.order.deal"].count
    total = position_count + stop_count + fill_count

    return PrivateWsReport(
        login_verified=True,
        filter_sent=True,
        pongs_seen=pongs,
        private_events_seen=total,
        position_events_seen=position_count,
        stop_plan_events_seen=stop_count,
        fill_events_seen=fill_count,
        unexpected_private_channels=tuple(sorted(unexpected)),
        channel_schema={
            channel: evidence[channel].shareable()
            for channel in sorted(ALLOWED_CHANNELS)
        },
        elapsed_ms=elapsed_ms,
        status=(
            "COMPLETED_PRIVATE_WS_OBSERVATION"
            if not unexpected
            else "COMPLETED_WITH_UNEXPECTED_PRIVATE_CHANNELS"
        ),
    )
