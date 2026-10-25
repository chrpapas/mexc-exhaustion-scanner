# T100 live-shadow Render supervisor — draft deployment plan

Status: **DRAFT / NOT DEPLOYABLE YET**

This document describes the independent Render worker that will supervise the
real MEXC Futures account in signed read-only mode before any live-order pilot.

It does not replace the existing paper trader and it does not enable order
submission.

## Existing Render topology

Current production services remain unchanged:

- `mexc-exhaustion-scanner` — scanner worker
- `mexc-standard-short-trader` — current paper trader worker

The live-shadow supervisor is a third independent worker:

- `mexc-t100-live-shadow`
- start command: `python -m app.t100_live_shadow_runtime`
- region: Frankfurt
- manual deploy only
- no `DATABASE_URL`
- no order gateway
- no checkpoint migrations
- no live authorization

Draft Blueprint:
`render.t100-live-shadow.draft.yaml`

## Hard execution boundary

The shadow worker requires:

```text
T100_EXECUTION_MODE=paper
T100_LIVE_ORDER_API_ENABLED=false
T100_LIVE_KILL_SWITCH=true
```

It refuses startup if:
- `T100_EXECUTION_MODE` is not `paper`
- the live-order API flag is enabled
- MEXC read credentials are absent
- Discord is required but the webhook is absent

The process deliberately does not import `t100_live_execution.LiveOrderGateway`.
It has no code path for creating, cancelling, closing or amending an exchange
order.

## Runtime observation loop

Default cycle:

1. signed GET-only preflight
2. critical projected inventory double-read
3. symbol-scoped REST redundancy
4. account-level position-margin witness
5. native TP/SL presence/semantics observation
6. bounded private WebSocket observation
7. REST -> WS -> REST continuity reconciliation
8. safety classification / one-way kill latch
9. Discord notification for meaningful state changes
10. periodic health report

Default cadence:

```text
T100_SHADOW_CYCLE_SECONDS=90
T100_SHADOW_WS_WINDOW_SECONDS=30
T100_SHADOW_HEALTH_REPORT_SECONDS=21600
T100_SHADOW_NOTIFICATION_COOLDOWN_SECONDS=900
```

## Discord operational cockpit

The worker sends notifications for events that matter operationally.

### Startup / shutdown

- live-shadow process started
- live-shadow process stopped / Render restart

### Account state

- position count changed
- current native TP/SL count changed
- protection state changed
- position observed without current native TP/SL

### Private exchange lifecycle

- private position event
- private stop-plan event
- private fill event
- unexpected private WS channel

The Discord message contains only event counts and high-level state. It does
not include credentials, exchange order IDs, position IDs, private prices,
quantities or balances.

### Safety / kill events

Examples:

- critical REST inventory changed between reads
- symbol redundancy failed
- position-margin witness failed
- native protection disappeared
- REST/WS continuity failed
- private read/connectivity failure
- unexpected private WS channel

The first kill reason is permanently retained for the process lifetime.
The worker continues observing after a kill so Discord remains useful, but it
cannot clear the latch. Operator review and process restart are required.

### Periodic health

A low-frequency health message confirms:
- process is alive
- critical REST consistency state
- symbol redundancy state
- position-margin witness state
- REST/WS continuity state
- current kill state

Routine polling and heartbeat traffic are not sent to Discord.

## Render configuration

Secrets must be configured only in Render:

```text
MEXC_API_KEY
MEXC_API_SECRET
DISCORD_TRADER_EVENTS_WEBHOOK_URL
```

The MEXC key used by this shadow service should remain read-only. No API
permission expansion is part of shadow deployment.

The draft Blueprint uses:

```yaml
autoDeployTrigger: off
```

This is intentional. Every rollout requires an explicit operator-triggered
deploy after CI and review.

## Deployment gates before creating the Render service

Do not create or deploy the worker until all are true:

- branch CI green
- local real preflight still read-only
- 5/5 REST/WS continuity soak remains green
- account-level position-margin witness has been observed on the real account
- Discord test message works
- no live-order permission is enabled
- current known account state is understood
- PR remains draft

## First Render shadow acceptance test

After the service is eventually created, it must pass all of the following
without any exchange mutation:

1. Render process starts successfully.
2. Discord receives the startup notification.
3. Signed private reads succeed.
4. Private WS login/filter/heartbeat succeeds.
5. Current account position/protection state is reported correctly.
6. Any expected existing safety issue latches and is reported once.
7. Worker remains alive and continues observing after the latch.
8. Manual Render restart produces a clean startup notification.
9. No duplicate Discord spam occurs during unchanged cycles.
10. Render logs show no POST/order/cancel/stop mutation path.
11. `entry_allowed=false` and `live_authorization=false` remain invariant.

Only after this shadow acceptance test and the remaining readiness blockers are
closed should the project move to a separate controlled live-pilot decision.
