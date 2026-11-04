# Dedicated T100/225 pilot — real signed GET and observed Discord message (2026-11-04)

**Evidence origin:** operator's terminal output from a local macOS command plus
operator's independent confirmation of the exact message in the Discord channel.
The exact copy of `t100_pilot_real_get_discord_smoke.py` in the working
conversation was reviewed; no API secrets, webhook URLs, raw order IDs or
private exchange payloads are included here.

## Reported successful live observations

```text
KEYCHAIN=DEDICATED_PILOT_KEYS_FOUND
MEXC_PRIVATE_GET=PASS_6_SIGNED_PRIVATE_GETS
PILOT_INVENTORY=FLAT_NO_OPEN_ORDERS_OBSERVED
MEXC_MUTATIONS=ZERO
DISCORD=HTTP_ACCEPTED_CHECK_CHANNEL
PILOT_LIVE_AUTHORIZATION=FALSE
```

The operator subsequently independently confirmed the actual Discord channel
displayed the following message:

> T100/225 dedicated pilot smoke check: signed MEXC GET=PASS_6_SIGNED_PRIVATE_GETS; inventory=FLAT_NO_OPEN_ORDERS_OBSERVED. NO MEXC ORDER SENT. This is NOT a live-trading authorization.

This corroborates **actual Discord message receipt in the reported channel**,
not merely an HTTP 2xx response from Discord.

The script's MEXC HTTP code uses only signed GETs to:
`/api/v1/private/position/open_positions`,
`/api/v1/private/stoporder/open_orders`, and
`/api/v1/private/order/open_order_total_count`.
It obtains the pilot API key and secret only from macOS Keychain service
`t100225-mexc-pilot-subaccount` without fallback to Render/main credentials.
Two snapshots, each with three GETs, passed strict result-shape and
order-count consistency checks, matched exactly, and showed no open positions,
native stops or outstanding orders. No MEXC order submission, cancellation,
amendment or other exchange mutation was performed by this smoke script.
The Discord POST used a user-entered private dedicated webhook.

## Exactly what is and is not verified

**Verified for this one-shot test (operator reported):**
- Keychain access to the dedicated pilot key succeeded.
- Six real signed MEXC private GETs succeeded, with mutually consistent
  inventory shapes and two matching flat snapshots.
- One real Discord HTTP webhook message was accepted *and independently
  seen in the channel by the operator*.
- Zero MEXC mutations by this script; no live authorization granted.

**Not verified by this test:**
- Continuous independent watchdog service with the same credentials,
  the watchdog's separate exchange-side read-only key restriction, restart
  reliability, unattended Discord delivery, independent heartbeat process,
  or alert escalation on real protection loss.
- Server-side API-key permission flags; the pilot key previously sent a
  deliberately authorized real one-contract entry. A GET-only transport
  does not convert a trade-capable key into a read-only key.
- STOP-active timing, native stop trigger fills, native stop amendment,
  emergency close, cross margin, four-slot exposure or Render deployment.
- Any guarantee that the pilot remains flat after the bounded snapshot.

The separate existing Render live-shadow worker is NOT the dedicated $50
pilot. Its contemporaneous logs reported a protection-loss kill latch and
observed position without TP/SL. Do not conflate those service observations
with this flat pilot subaccount or reset that kill latch.

**Release status remains blocked**:
`entry_allowed=false`, `live_authorization=false`.
The real one-shot MEXC GET and Discord receipt milestone is now satisfied;
the next distinct milestone is an independently supervised, real continuous
GET-only pilot watchdog with a separate server-side read-only key and
verified liveness/alert behavior. No production or live order promotion.
