"""Structured Discord operations notifications for T100 live-shadow runtime.

Notification-only. No exchange, database, or order mutations.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable

from app.trader_notifier import TraderNotifier


@dataclass(frozen=True)
class OpsNotice:
    key: str
    title: str
    description: str
    severity: str
    fields: tuple[dict[str, Any], ...] = ()


SEVERITY_COLORS = {
    "info": 0x5865F2,
    "success": 0x57F287,
    "warning": 0xFEE75C,
    "critical": 0xED4245,
}


class T100LiveOpsNotifier:
    """Deduplicated important-event notifier.

    Critical notices bypass cooldown. Other notices with the same key are
    suppressed for cooldown_seconds to avoid Discord spam.
    """

    def __init__(
        self,
        webhook_url: str | None,
        *,
        cooldown_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(cooldown_seconds, bool)
            or not isinstance(cooldown_seconds, (int, float))
            or cooldown_seconds < 0
        ):
            raise ValueError("INVALID_NOTIFICATION_COOLDOWN")
        self._notifier = TraderNotifier(webhook_url)
        self._cooldown = float(cooldown_seconds)
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self._notifier.webhook_url)

    async def close(self) -> None:
        await self._notifier.close()

    async def send(self, notice: OpsNotice, *, force: bool = False) -> bool:
        if notice.severity not in SEVERITY_COLORS:
            raise ValueError("INVALID_NOTIFICATION_SEVERITY")
        if not notice.key or not notice.title or not notice.description:
            raise ValueError("INVALID_NOTIFICATION_CONTENT")

        now = self._clock()
        last = self._last_sent.get(notice.key)
        if (
            not force
            and notice.severity != "critical"
            and last is not None
            and now - last < self._cooldown
        ):
            return False

        sent = await self._notifier.send(
            notice.title,
            notice.description,
            list(notice.fields),
            color=SEVERITY_COLORS[notice.severity],
        )
        if sent:
            self._last_sent[notice.key] = now
        return sent
