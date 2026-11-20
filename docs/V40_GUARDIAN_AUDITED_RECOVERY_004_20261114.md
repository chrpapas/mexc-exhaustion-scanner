# Guardian safety incident 003: reviewed one-time recovery 004 (2026-11-14)

## Evidence independently observed

Render guardian service `srv-dbqumbbrjlhs73cfoi50` in workspace
`tea-d3oimrjipnbc7381g6jg` recorded three explicit
`V40_GUARDIAN_HEDGE_MODE_SIGNED_GET_FAILED_HALT` alerts
on **2026-11-13 at 17:42:05, 19:09:01 and 21:58:40 UTC**.

Every alert was durably recorded, sent to Discord and followed after the
guardian's restart by a fresh signed GET double-inventory observation of
a FLAT hedge-mode account. The signed GET had not yet been changed to
preserve an HTTP/API subcategory, so its exact transport root cause
(429/5xx/auth/timeout) CANNOT be reconstructed retrospectively. This is
positive evidence of a read-stage failure, not proof of a particular
HTTP code, nor permission to assume any FUTURE GET failure is harmless.

The SELECT-only archive audit under guardian's existing DB role showed:
- legacy: 1 generic critical delivered;
- reviewed 001: 1 generic critical delivered;
- recovery 002: 1 generic critical delivered;
- recovery 003: 3 generic criticals delivered;
- no undelivered critical messages or other incident codes in those
  four generations;
- original 003 latch permanently true; current 003 inventory flat.

## 004 recovery safety model

The fixed 004 recovery code tests the above EXACT historical incident
counts, delivery status, predecessor critical latches and fresh 003
flat state from live Postgres. The code checks two NEW independently
signed MEXC account inventory/hedge GETs, plus immutable ledger
recovery in each observation. Any mismatch prevents a new scope.

If and only if those checks pass, it inserts one NEW scope 004
`AUDITED_ROLLOVER` row and leaves every older scope and outbox
record unchanged. It never sets a boolean override to permit orders.
The independent SQL trader POST gate can only evaluate 004's NEW,
CURRENT, exactly-flat, unlatched heartbeat and empty critical/pending
outbox, within 15 seconds. If the 004 witness fails, another sticky
critical is recorded. Automatic 005 is prohibited.

The idempotent, opt-in, single transient position-mode GET retry from
PR #45 is enabled on the guardian. It does NOT retry authentication,
wrong modes or any position/stop/ledger discrepancy.

## Limitations before real V40 portfolio trading

- Guardian recovery removes only a failed-observation historical
  readiness block. It does NOT certify complete 7.238406× V40
  strategy order execution, native trailing stop amendments, margin
  funding sufficiency or actual SL execution.
- The deployed trader is still the single-position $12.50 canary and
  not the four-position historical strategy. This change MUST NOT
  be used to enable it as a substitute.
- A second-to-last moment GET is not a formal distributed
  MEXC/Postgres atomicity guarantee. A later missing stop or unexpected
  position is a critical incident and blocks further entries.
- The only production mutation from this migration is the audited
  creation of one new guardian incident generation, not an order,
  stop price change, funds transfer, leverage change, schema change
  or historical incident deletion.
