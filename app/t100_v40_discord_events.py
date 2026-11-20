"""V40 signal/execution Discord notices, separate from trading authorization.

Only facts already proven by a V40 scanner receipt, execution result, or
verified emergency close may be reported. Never send account credentials,
exchange HTTP bodies, or arbitrary exception text. A missing/unreachable
webhook does not change order decisions or protection.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.t100_live_ops_notifier import OpsNotice, T100LiveOpsNotifier

LOG = logging.getLogger(__name__)

# V40's dedicated webhook takes precedence. Legacy Discord names are
# backwards-compatible *only* if the value is a valid Discord webhook URL.
HOOK_KEYS = (
    "T100_V40_DISCORD_WEBHOOK_URL",
    "DISCORD_TRADER_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL",
    "T100_GUARDIAN_DISCORD_WEBHOOK_URL",
)
_SYMBOL = re.compile(r"[A-Z0-9]{1,30}_USDT\Z")
_CODE = re.compile(r"[A-Z0-9_]{5,140}\Z")


def webhook_from_env(env: Mapping[str, str]) -> str | None:
    """Never log/return a secret URL to diagnostics; only use for transport."""
    for key in HOOK_KEYS:
        raw = env.get(key, "")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = urlsplit(raw)
            if (
                parsed.scheme == "https"
                and parsed.hostname in ("discord.com", "discordapp.com")
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment
                and re.fullmatch(
                    r"/api/webhooks/[0-9]{10,24}/[A-Za-z0-9_-]{20,256}",
                    parsed.path,
                )
            ):
                return raw
        except ValueError:
            pass
    return None


def _identity(signal_id: Any, symbol: Any) -> tuple[int, str] | None:
    if (
        type(signal_id) is int and signal_id > 0
        and type(symbol) is str and _SYMBOL.fullmatch(symbol)
    ):
        return signal_id, symbol
    return None


def stage2_notice(receipt: Any) -> OpsNotice | None:
    """Yellow only for validated Stage2 + below-VOL30 scanner receipts."""
    identity = _identity(
        getattr(receipt, "signal_id", None),
        getattr(receipt, "symbol", None),
    )
    vol = getattr(receipt, "pre24_vol_pct", None)
    tier = getattr(receipt, "tier", None)
    p2_at = getattr(receipt, "p2_at", None)
    if (
        identity is None or type(vol) is not float
        or not 0 <= vol < 30
        or tier not in ("LOW_100", "HIGH_225")
        or p2_at is None or getattr(p2_at, "tzinfo", None) is None
    ):
        return None
    sid, symbol = identity
    return OpsNotice(
        key=f"v40-stage2-{sid}",
        title=f"🟡 V40 Phase 2 qualified • {symbol}",
        description=(
            "A genuine Stage2 exhaustion signal passed the V40 VOL30 gate. "
            "**SIGNAL ONLY — no exchange order has been placed.**"
        ),
        severity="warning",
        fields=(
            {"name": "Signal ID", "value": str(sid), "inline": True},
            {"name": "Tier", "value": tier, "inline": True},
            {"name": "VOL30", "value": f"{vol:.4f}%", "inline": True},
            {"name": "Signal time (UTC)", "value": p2_at.isoformat(), "inline": False},
        ),
    )


def trader_notice(report: Any) -> OpsNotice | None:
    status = getattr(report, "status", None)
    if type(status) is not str or not _CODE.fullmatch(status):
        return None
    if status in (
        "V40_FIRST_CANARY_WAIT_FOR_GENUINE_STAGE2",
        "V40_FIRST_CANARY_REJECTED_SIGNAL_NO_RETRY",
    ):
        return None
    sid = getattr(report, "signal_id", None)
    if type(sid) is not int or sid <= 0:
        sid = None
    reason = getattr(report, "precheck_failure_code", None)
    if type(reason) is not str or not _CODE.fullmatch(reason):
        reason = "not_available"
    posted = getattr(report, "order_post_may_have_occurred", False) is True
    ident = f"Signal #{sid}" if sid is not None else "No signal ID"
    key = f"v40-executor-{sid if sid is not None else 'runtime'}-{status}"
    if status == "V40_ONE_PROTECTED_ENTRY_RECOVERED_NOT_AUTONOMOUS":
        if getattr(report, "verified_entry", False) is not True:
            return None
        return OpsNotice(
            key=key,
            title="🟢 V40 SHORT entry independently verified",
            description=(
                f"{ident}: the first-canary protected entry and its "
                "native stop were independently recovered from the exchange. "
                "**This is a confirmed entry, not merely an order request.**"
            ),
            severity="success",
        )
    if status == "V40_FIRST_CANARY_GENUINE_VOL30_SKIP":
        return OpsNotice(
            key=key,title="🟡 V40 candidate skipped by VOL30",
            description=f"{ident}: volatility gate rejected the signal. **NO ORDER.**",
            severity="warning",
        )
    if status == "V40_FIRST_CANARY_CURRENT_RISK_DEPTH_OR_ACCOUNT_HALT":
        return OpsNotice(
            key=key,
            title="🔴 V40 entry rejected • preflight error",
            description=(
                f"{ident}: current signed account, risk, contract, or order-book "
                f"verification failed. Code: `{reason}`. **NO ORDER POST.**"
            ),
            severity="critical",
        )
    if posted:
        return OpsNotice(
            key=key,
            title="🔴 V40 execution outcome uncertain — reconcile account",
            description=(
                f"{ident}: status `{status}`. An exchange order **may** have "
                "been submitted. Check signed exchange state before any further trade."
            ),
            severity="critical",
        )
    return OpsNotice(
        key=key,
        title="🟡 V40 signal not traded",
        description=f"{ident}: status `{status}`. No verified order submission.",
        severity="warning",
    )


def verified_emergency_close_notice(report: Any) -> OpsNotice | None:
    """Emergency CLOSE, never mislabelled as a native SL75 trigger."""
    if (
        getattr(report, "durable_checkpoint_and_slot_closed", False) is not True
        or getattr(report, "signed_close_and_flat_verified", False) is not True
    ):
        return None
    return OpsNotice(
        key="v40-emergency-close-" + str(getattr(report, "status", "complete")),
        title="🔴 V40 emergency close verified",
        description=(
            "The dedicated canary emergency close was reconciled against a "
            "signed flat account and committed checkpoint. **Not a reported SL75 hit.**"
        ),
        severity="critical",
    )


async def send_v40_notice(
    env: Mapping[str, str],
    notice: OpsNotice,
    *,
    timeout_seconds: float = 3.0,
) -> bool:
    """Best-effort transport; cannot change any live trading outcome."""
    webhook = webhook_from_env(env)
    if not webhook:
        LOG.warning("V40_DISCORD_NOT_CONFIGURED event=%s",notice.key)
        return False
    sender = T100LiveOpsNotifier(webhook, cooldown_seconds=0)
    try:
        sent = await asyncio.wait_for(sender.send(notice), timeout_seconds)
        LOG.info(
            "V40_DISCORD_DELIVERY event=%s success=%s",
            notice.key, bool(sent),
        )
        return bool(sent)
    except Exception:
        LOG.warning("V40_DISCORD_DELIVERY_FAILED event=%s",notice.key)
        return False
    finally:
        await sender.close()
