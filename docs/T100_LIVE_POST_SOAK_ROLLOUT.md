# T100 live post-soak rollout sequence

Status: **procedure only — do not execute during the active protected-position
soak.** No step in this document is itself authorization to trade.

## Preconditions

The current read-only Render protected-position soak must first complete its
uninterrupted observation window. Record the exact deployed commit and verify
that no deploy, restart, hardware failure, suspension, or process failure
occurred during the entire window.

The feature-branch live-safety CI must be green at the exact code revision
being reviewed.

## Phase 1 — preserve the soak evidence

1. Read Render event history for `mexc-t100-live-shadow`.
2. Verify the uninterrupted interval from its current deployment start.
3. Record deployed commit, start/end UTC, and absence/presence of failure
   events in PR #11.
4. Do not redeploy the soak worker merely to pick up newer branch code before
   the soak evidence is recorded.

## Phase 2 — database migration with live orders still impossible

1. Keep all T100 execution settings in paper/read-only mode.
2. Take/verify the available database backup/recovery point.
3. Using the migration/table-owner credential, apply:
   - `migrations/027_t100_live_order_intents.sql`;
   - `migrations/028_t100_live_vol30_h4.sql` (full V40 parent/child state);
   - `migrations/029_t100_live_durable_state.sql`;
   - `migrations/030_t100_live_executor_lease.sql`.
   Apply each only after inspecting existing objects and matching expected
   definitions, inside a reviewed transaction from a trusted permitted
   connection. `CREATE TABLE IF NOT EXISTS` is not schema-drift validation.
4. Do not point an order-capable worker at the database.
5. Verify all expected tables, columns, indexes and BIGSERIAL sequences exist.

These four migrations are create-only and have no references to paper portfolio
tables. They do not establish V40 execution-parity or authorize trading.
The read-only `app.t100_live_db_inspect` checks the full 027–030 table set.

## Phase 3 — dedicated restricted runtime database credential

Use a database credential distinct from the migration/table-owner credential.
Render supports multiple PostgreSQL users; if a Render-managed credential is
created, remember that it becomes the database's new default credential and
can affect Blueprint-managed connection references on a later sync.

Replace `__T100_LIVE_RUNTIME_ROLE__` in:

`sql/t100_live_runtime_privileges_template.sql`

with the dedicated runtime role and apply it using the owner credential.

Required policy:

- mutable live state:
  - SELECT, INSERT, UPDATE
  - no DELETE
  - no TRUNCATE
- append-only live evidence:
  - SELECT, INSERT only
  - no UPDATE
  - no DELETE
  - no TRUNCATE
- VOL30 parent/child tables:
  - SELECT, INSERT
  - UPDATE only on explicit lifecycle/ack/fill columns
  - no DELETE/TRUNCATE and no table-wide UPDATE
- executor lease:
  - SELECT, INSERT
  - UPDATE only holder/fencing/expiry fields
  - no DELETE/TRUNCATE and no table-wide UPDATE
- required BIGSERIAL sequences:
  - USAGE, SELECT
  - no UPDATE / setval authority

Run `app.t100_live_db_inspect` through the runtime credential. The rollout
must stop unless both the durable-schema checks and strategy/lease
column-level privilege checks pass.

## Phase 4 — clean durable state

Using the restricted runtime credential, run the read-only unresolved-state
gate. The rollout must stop if any of these exist:

- PREPARED order intent
- SUBMITTING order intent
- UNKNOWN order intent
- non-VERIFIED stop amendment
- REVIEW position checkpoint
- CLOSING position checkpoint

Required result:

`unresolved_mutations_absent=true`

## Phase 5 — current MEXC inventory completeness and reconciliation

Run the one-shot read-only dynamic readiness bundle in paper mode from a
trusted environment that can reach both MEXC and Render Postgres.

The bundle must establish:

- current position inventory completeness
- current TP/SL inventory completeness
- exact `stopOrderCount` agreement
- position-margin witness
- symbol-scoped redundancy when non-flat
- bookended safety-critical inventory stability
- account reconciliation
- durable schema/privilege verification
- no unresolved durable mutation state

It remains non-authorizing even when all dynamic checks pass.

## Phase 6 — private WebSocket pre-pilot contract

Run a bounded read-only private WebSocket observation.

For a tiny pilot, require:

- login succeeded
- filter was sent
- no unexpected private channels
- documented semantic parser contract is green in CI
- any naturally observed private events pass the semantic validator

A zero-event observation may satisfy the **pre-pilot transport/contract** gate.
It does not certify the full lifecycle.

Full autonomous promotion later requires real WS↔REST correlation for:

- position
- TP/SL plan order
- fill detail

## Phase 7 — full V40 strategy parity

Do not use the current T100_225 paper trader as proof of V40 parity.

The existing V40 historical audit certifies only entry/filter/reference
capacity arithmetic. Full strategy parity remains blocked until the frozen
archive is replayed chronologically with exact:

- candidate timing/identity
- admission decisions
- slot occupancy and one-symbol rule
- sizing and lot rounding
- exits/trailing chronology
- fees
- funding
- point-in-time contract specs
- final equity and drawdown
- zero future-data/forward-fill leakage

`app.t100_live_strategy_parity_gate.py` must return
`strategy_parity_verified=true`.

## Phase 8 — independent review and operator pilot decision

Before a real-money mutation permit can exist:

- independent review completed
- exact reviewed code revision pinned
- strategy parity verified
- protected soak verified
- restart recovery verified
- durable schema verified
- current account inventory/reconciliation verified
- pre-pilot WS contract verified
- no unresolved mutations
- explicit operator authorization for a tiny pilot
- live risk limits specified
- 1x leverage
- order API switch explicitly enabled
- kill switch explicitly disengaged only for the authorized pilot window

The typed `LiveMutationPermit` is scope-limited to `T100_TINY_PILOT`.

## Phase 9 — tiny pilot

The first pilot is an infrastructure validation, not performance validation.

Mandatory behavior:

1. persist entry intent PREPARED
2. transition PREPARED -> SUBMITTING before POST
3. exactly one MEXC order POST attempt
4. ambiguous outcome -> UNKNOWN; never blind retry
5. resolve by externalOid
6. verify complete order-scoped fills
7. verify exact open position and native TP/SL
8. persist immutable fill set
9. persist protected initial checkpoint
10. trailing stop amendments use the same SUBMITTING/UNKNOWN/VERIFIED
    exactly-once-attempt protocol

Any discrepancy engages fail-closed/manual-review behavior.

## Phase 10 — post-pilot promotion evidence

Do not promote to autonomous production merely because one tiny pilot
succeeds.

Collect and verify:

- WS↔REST position correlation
- WS↔REST TP/SL correlation
- WS↔REST fill correlation
- restart recovery with durable non-flat state
- stop amendment lifecycle
- normal close chain:
  stop ID -> finished TP/SL -> placeOrderId -> close fills
- double-collected final funding
- deterministic final accounting
- durable CLOSED checkpoint

Only after this evidence is reviewed should a separate autonomous-production
authorization be considered.
