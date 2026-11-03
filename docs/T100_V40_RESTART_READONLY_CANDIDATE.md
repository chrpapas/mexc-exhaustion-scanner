# T100/V40 — Read-only restart candidate (development, not deployed)

**Status (2026-11-03): TESTED IN CI; NOT APPROVED FOR RENDER DEPLOYMENT OR MEXC ORDER EXECUTION.**

This is a narrow, optional, single-cycle diagnostic for the future
four-position T100/V40 runtime. It does not implement order submission,
stop create/amend/close, automatic recovery, risk liquidation controls,
account permission changes, or a production deployment. The existing
Render shadow worker is running an earlier SHA, remains kill-latched and
must not be replaced without an explicit operator-controlled rollback
plan and independent review.

## Verified chain

1. `PostgresFourSlotRestartTriage` reads every non-RELEASED V40
   reservation, matching order intent and immutable one-shot dispatch
   claim in ONE restricted **read-only REPEATABLE READ** PostgreSQL
   transaction. It rejects wrong account role, database, excessive or
   duplicate active slots and claim/intent/slot provenance drift.
2. For RESERVED/SUBMITTING/UNKNOWN/ACK/HALTED, output is
   `RESTART_PENDING_ATTEMPT_EXACT_ORIGIN_RECONCILE_ONLY`.
   No MEXC GETs are attempted by this supervisor and **no retry** is
   authorized. Even an absent dispatch claim does not prove no POST was
   sent. Exact-origin manual/GET-only recovery is an independently gated
   later process; never infer an order cancellation from absent GET rows.
3. When there are no pending attempts, the existing
   `run_readonly_four_slot_audit` can inspect complete exchange
   positions/current native stops and recover independently committed
   nonflat checkpoints via signed **PRIVATE GET only** and restricted
   SELECTs. That is still a NONATOMIC observation, not authority to
   send orders. Drift, missing native stops, incomplete inventory and
   manual position ambiguity all block the cycle.
4. The `safe_summary` and supervisor response intentionally omit
   symbols, externalOids, position/order IDs, balances, leverage,
   secrets and prices. Every status retains `entry_allowed=false`,
   `live_authorization=false`, `mexc_orders_sent=false`,
   `database_writes_sent=false` and `order_retry_allowed=false`.

An explicitly opt-in local diagnostic path is available via
`python -m app.t100_live_four_slot_readonly_cli --restart-preflight`.
The unchanged default CLI remains available; neither path starts
automatically on Render. The opt-in mode requires all three operator
guards in the calling environment:

- `T100_EXECUTION_MODE=paper`
- `T100_LIVE_ORDER_API_ENABLED=false`
- `T100_LIVE_KILL_SWITCH=true`

It uses locally supplied exchange credentials; a **GET-only API key is
strongly recommended**, but the code cannot certify the key's venue
permissions. It prompts privately for the restricted DB password and
connection URL; the expected database role is pinned in the source. Do not
paste credentials or connection strings into tickets or chat. Do not
run against current production until migrations 031/032, runtime role
privileges and the deployment plan have been separately approved;
missing schemas/permissions are intentionally blocked.

## CI evidence

- Pure offline tests cover 0–4 slot bookkeeping, unresolved or unknown
  orders with and without dispatch claims, duplicate slots, stale
  provenance, wrong DB role/transaction, paper/kill restrictions,
  missing native stops and manual-position isolation.
- Isolated PostgreSQL 16 tests cover actual restricted SQL states:
  SUBMITTING without claim, committed claim still SUBMITTING,
  UNKNOWN and inconsistent claim, claim-bound protected ACK, and
  post-ACK restart GET-only observation of four protected positions.
- CLI fake-connector tests prove that the opt-in flag uses only the
  restricted triage pool and refuses all exchange GETs if pending;
  invalid controls refuse before secret prompts or database calls.

All five GitHub checks passed at code SHA
`429ec1a8fccea8edf8ae716feb218d9555140b4f`:
- Offline safety: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258081
- Disposable PostgreSQL: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258149
- Scanner/trader split: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258157
- Promotion: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258155
- Dashboard: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258074

**Limitations and dependencies:** MEXC order POST semantics and native
stop creation/amend execution are unverified; no distributed
exchange/database atomicity can be guaranteed by this read-only flow.
Account-wide cross margin/collateral/tier evidence and the maximum
loss under gaps/liquidation are not certified. Migrations 031/032 are
not deployed on production, and the restricted runtime grants need
independent review. Production Render still runs an older code SHA.
Until an operator-approved read-only isolated rollout, protected soak,
code/security review and separately authorized tiny live venue pilot,
**all live entry and deployment authorization gates remain BLOCKED**.

## Last external observation

On 2026-11-03 at 14:54:38 UTC, the older Render shadow still logged
`OBSERVING_KILL_LATCHED`, `position_observed=True`,
`tpsl_observed=False`, `kill_reason=NATIVE_PROTECTION_LOST`,
`entry_allowed=false` and `live_authorization=false`.
This is an account risk warning, not a permission to adopt/manage the
user's existing manual position. Protection must be checked directly
at MEXC by the account operator. The software did not amend it.

No actual orders, production DB/role changes, Render deployments,
credential changes, or BOXWAVE changes were performed while preparing
this development artifact.
