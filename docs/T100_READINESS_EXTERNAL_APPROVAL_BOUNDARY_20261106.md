# T100/V40 2026-11-06 engineering handoff — external authorization boundary

**Branch:** `codex/t100-readiness-issue-14`.
**Current operator mandate:** continue autonomous engineering/review; STOP at
separate deployment, credential, real-order and risk-authorization boundaries.
**Source of authority:** GitHub issue #14 explicitly states *NO LIVE ORDERS*
and requires separate explicit operator authorization for deployment/live.

## Verified engineering changes in this checkpoint

- Separate read-only guardian worker and a distinct review-only Render
  Blueprint, with auto-deploy off and disabled flags. No Render creation,
  service replacement or staging to the existing manual-position account.
- Review-only migration 035 creates a guardian incident state and transactional
  alert outbox. It has **no grants** and does not touch production. A sticky
  critical incident cannot be automatically cleared by healthy observations.
- Dedicated `GuardianIncidentOutbox` uses a PostgreSQL session singleton,
  compares persistent status and stages Discord incidents atomically.
  Unacknowledged events survive process restarts; delivery is **at least
  once**, not exactly once.
- A **separate** durable guardian notification runner uses a distinct,
  restricted incident-writer PostgreSQL role. The account checkpoint reader
  still requires a read-only transaction and a SELECT-only role. The incident
  writer is rejected if it has permission to modify T100 position checkpoints,
  trading intents, stop amendments, emergency close intents or account
  enrollments. Its network POST is only to a strictly validated Discord
  webhook; no MEXC POST transport is imported.
- The review-only Blueprint now selects the durable worker, with
  `T100_GUARDIAN_DURABLE_ALERTS_ENABLED=false` by default and all secrets
  marked `sync: false`.
- Offline mocked incidents cover state changes, secret-safe error logging,
  missed Discord ACKs, latching, duplicate suppression and account/role
  separation. PostgreSQL 17 CI runs migration 035 against a disposable
  localhost instance and verifies immutable latch, restricted role, pending
  event persistence across connections, and advisory singleton.

## Last verified tests

- Commit `78ec338a12c1010fcd54e3273eff49b6bd4755d0`:
  T100 live safety full-offline **2,501 passed, 23 skipped**; all nine
  GitHub workflows completed successfully, including disposable PostgreSQL
  17 integration for the guardian alert outbox.
- Green CI proves repository logic for tested mocks, not exchange atomicity,
  real Render uptime, Discord delivery to the operator, risk limits or
  end-to-end production trading safety.

## Specifically known real-exchange evidence

- Independent V5 pilot confirmed exact native-order generated CLOSE_SHORT
  execution and exact position terminal historical closure using dedicated
  T100 subaccount signed GETs. Manual MEXC positions were not managed.
- MEXC returned bare historical position arrays without totalCount/page
  metadata. Positive exact matches are useful but missing rows cannot prove
  history completeness or account-wide flatness.
- MEXC's finished stop `triggerSide=1` remains **contradictory** with
  the documented stop-loss side, so stop-loss trigger-side certification is
  still FALSE. No further real orders are justified only to change a label.

## Critical release gate register

| Gate | Status | Evidence or blocker |
| --- | --- | --- |
| Native-stop real pilot closed | PARTIAL PASS | Exact filled close and historical position; TP/SL label conflict unresolved |
| Manual-position isolation | OFFLINE PASS | Tested account/position/entry-ID scoped logic; no manual adoption |
| Durable owned close-intent fence | OFFLINE PASS | PostgreSQL 17 migration 033; not applied to live |
| Dedicated guardian read-only code | OFFLINE PASS | GET-only MEXC and SELECT-only checkpoint role |
| Guardian incident durable outbox | OFFLINE PASS | PostgreSQL 17 migration 035 and webhook tests |
| Guardian account binding | BLOCKED EXTERNALLY | Migration 034 not applied; no approved enrollment record |
| Guardian dedicated credentials | BLOCKED EXTERNALLY | View-only MEXC API key, restricted PG roles and TLS CA not provided |
| Guardian Render operation | BLOCKED EXTERNALLY | Separate service not created, no uninterrupted soak, no real delivery verification |
| Main T100 live-entry adapter certification | BLOCKED | `LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED`, two permit contracts, endpoint/atomicity unresolved |
| Current account risk, cross collateral and 4-slot | BLOCKED | No independently certified current exposure/collateral, 4-slot conditions |
| Crash/restart nonflat recovery in real venue | PARTIAL | Extensive offline tests and narrow V5, not supervised full live runtime |
| Final independent review and limited-risk authorization | BLOCKED | Requires separately approved operator action and current evidence |

**Do not promote any gate based on a percentage or a green CI badge.**
The previous 92% figure was a rough engineering estimate, not a measured
release certification, and should not be used to override these blockers.

## Minimal operator action needed next

1. Explicit approval to create a **NEW observation-only** Render service
   and apply narrowly scoped migrations/roles to the intended dedicated
   T100 database. This is not permission for live orders or to modify
   the existing `mexc-t100-live-shadow` service.
2. Independently provision a dedicated MEXC **VIEW-ONLY Futures API key**
   on that exact subaccount and store it only in the new Render service's
   private environment; do not send credentials to chat, GitHub or issue.
3. Independently verify the exact T100-owned checkpoint and enrollment
   before inserting migration 034's immutable account association.
   **Do not enroll manual positions**. If no owned open checkpoint exists,
   observer remains idle/not certified; do not fake it from V5 history.
4. After observer deployment and restart/Discord-soak evidence, separately
   authorize any real-order pilot or promotion to active trading.
   Issue #14 still controls the no-orders boundary.

**BOXWAVE remains independent. Manual positions remain under operator control.**
