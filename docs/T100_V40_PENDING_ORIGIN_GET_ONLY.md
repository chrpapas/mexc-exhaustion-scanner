# T100/V40 pending external-origin audit — READ ONLY, NOT DEPLOYED

**Development checkpoint, 2026-11-03.** This is a narrow optional
diagnostic for a V40 order that remains RESERVED, SUBMITTING, UNKNOWN,
ACK or HALTED after a restart. A pending order intentionally **blocks**
the normal `--restart-preflight`; this separate command does not
weaken that block and cannot recover a live position or submit a trade.

## Design and scope

The opt-in CLI path:

`python -m app.t100_live_four_slot_readonly_cli --pending-origin-audit`

requires the existing three controls `T100_EXECUTION_MODE=paper`,
`T100_LIVE_ORDER_API_ENABLED=false`, and
`T100_LIVE_KILL_SWITCH=true`, plus locally supplied API credentials
and private prompts for the restricted PostgreSQL connection.
Do **not** paste keys, passwords or the DB URI into chat or GitHub.
A venue read-only API key is preferable; the CLI does not independently
certify the exchange-side key permissions. This command must not be
used against current production until migrations 031/032, role grants,
source pin and an operator-approved isolated diagnostic plan are ready.

The first step reads the exact V40 reservation/order-intent/immutable
dispatch-claim provenance in a restricted, SELECT-only
`REPEATABLE READ` SQL transaction. It requires at least one unresolved
attempt and valid immutable symbol/origin identities. **It will not
construct an exchange client** if the SQL gate is missing, contradictory,
already settled or inaccessible.

It then rechecks the SQL snapshot after client construction and before
GET. Every non-RESERVED pending order is queried ONLY by its exact
`symbol/externalOid` through the existing validated
`/api/v1/private/order/external/...` GET endpoint, for at most two
rounds and at most four active slots. The result must have the exact
origin, positive order ID and side=3 (SHORT_SELL). Any side/identity
mismatch, timeout, schema problem or changed order payload blocks.

The CLI obtains a THIRD restricted SQL snapshot after the GET rounds.
If its durable state differs from the pre-GET snapshot, the observation
is discarded as `PENDING_ORIGIN_SQL_CHANGED_DURING_GET_NO_RETRY`.
Two MEXC GETs and SQL bookends detect observed drift only; they do
**not** establish MEXC/SQL atomicity, exclude exchange ABA changes,
guarantee position settlement or guarantee a native stop was accepted.

## Redacted statuses

- `PENDING_ORIGIN_ORDERS_SEEN_UNPROTECTED_NONAUTHORIZING`: each
  queried exact origin returned the same short-order response twice.
  It is NOT proof of a final fill or native stop.
- `PENDING_ORIGIN_ALL_ABSENT_STILL_UNKNOWN_NO_RETRY`: both GETs found
  no order. This does NOT prove an exchange POST never occurred.
- `PENDING_ORIGIN_MIXED_OBSERVATIONS_NO_RETRY`: some origins were seen
  and some absent. All remain unresolved.
- `PENDING_ORIGIN_ORDER_BOOKENDS_CHANGED_NO_RETRY`: order data drifted
  across the two rounds. No retry, no auto-repair.
- `PENDING_ORIGIN_RESERVED_ONLY_NO_EXCHANGE_INFERENCE`: reservation
  has no staged immutable order intent. No venue order GET performed.
- Invalid SQL state, missing tables/role, wrong origin or order side,
  GET exceptions, and before/after SQL drift all return BLOCKED codes.

Only counts and status strings are printed. No symbols, order/position
IDs, balances, raw API payloads, DB URI, credentials or prices are
returned. Every output keeps `entry_allowed=false`,
`live_authorization=false`, `retry_allowed=false`,
`mexc_orders_sent=false`, `database_writes_sent=false` and
`native_stop_protection_verified=false`. Manual positions are never
adopted or touched.

## Verification and release boundary

Offline mocked tests cover valid and missing origins, mixed outcomes,
side mismatch, order/SQL drift, malformed identities, reserved-only
states, credential-safe output and invalid control combinations.
Disposable PostgreSQL 16 tests prove a real restricted SQL triage may
read pending origins with and without an immutable dispatch claim, but
neither a matching order nor an absent order can advance a ledger state
or resubmit a trade.

No order POST, stop creation/amendment/close, production SQL mutation,
Render deployment or credential change has been made.

**This is not a safe live executor.** Native MEXC stop lifecycle,
account-wide collateral/margin tiers, complete four-slot execution,
protective Render soak, independent signoff and a separately approved
small live venue test all remain blockers.
