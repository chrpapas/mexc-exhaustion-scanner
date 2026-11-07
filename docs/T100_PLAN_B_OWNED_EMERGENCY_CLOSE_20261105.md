# T100/225 Plan B: T100-owned-only emergency close (2026-11-05)

**Engineering checkpoint; development branch only. NOT deployed, NOT authorized
for live trading. No MEXC orders or Render mutations performed by this work.**

## Boundaries

- Existing Render shadow worker retains its `NATIVE_PROTECTION_LOST`
  historical latch; do not clear it or use this work to silence it.
- Operator's manual MEXC positions remain solely operator-managed. The
  guardian refuses to adopt, amend, cancel, or target them with closes.
  They remain visible as **foreign positions** in the full inventory and
  must be included in a future account-wide liquidation/notional risk
  assessment. A shared account with manual exposure is **not automatically
  suitable** for new T100 entries.
- Dedicated $50 pilot subaccount and BOXWAVE are separate scopes. Never
  reuse a Keychain pilot key for the Render shadow account.
- The current paper trader remains paper-only:
  `LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED`; this limited emergency-close
  transport does **not** enable a T100 live entry or stop-attachment executor.

## Implemented on branch codex/t100-readiness-issue-14

1. `app/t100_live_owned_emergency_guardian.py`: validates durable scope,
   T100 entry `externalOid`, exact filled entry order identity, owned
   position identity/quantity, current market stop identity/coverage.
   Missing-stop emergency candidate needs **two independently certified,
   critical-field-consistent** full account snapshots. Fluctuating PnL is
   deliberately ignored; changes in manual position quantity are not.
2. Guarded close path: only exact managed position `symbol`+`positionId`
   can be forwarded, only after durable `PREPARED -> SUBMITTING`
   compare-and-swap. Any outcome uncertainty becomes `UNKNOWN`; no
   automatic retry, and an ACK is **not** marked a verified flat account.
3. `migrations/033_t100_owned_emergency_close_intents.sql`: review-only,
   CREATE-only unique `(scope_sha256,symbol,position_id)` one-shot journal
   and trigger preventing identity mutation, delete and state rewind.
   **Not applied to shared Render PostgreSQL.**
4. `app/t100_live_owned_close_pg_ledger.py`: parameterized durable
   INSERT-if-absent and state-transition CAS using only the 033 table.
   No role grants, secrets, DB connection or exchange POST.
5. `app/t100_live_pilot_close_only_mexc.py`: narrowly scoped injected
   `MexcTradeClient._private_post` close-only transport. Exact V4 working
   shape, **BTC_USDT short isolated one contract** only, explicit disabled
   default, exact owned position, deterministic single-use externalOid.
   Its in-process one-shot flag is defense in depth, **not** a substitute
   for the database ledger across restarts. It sends no order unless
   explicitly invoked behind the guarded path.
6. Tests: no-close-for-manual, missing-stop double GET, PnL drift,
   manual exposure drift, duplicate submissions, ambiguous POST, ledger
   failure, strict scope, exact close payload. PostgreSQL 17 CI performs
   actual 033 DDL and tests concurrency, immutable trigger and narrow
   role-denial of unrelated table privileges on disposable localhost.

## Still blocked for operational use

- Actual production integration must supply **independently verified
  account-wide complete inventory** and two separate signed GET reads;
  a caller-supplied `account_wide_complete=True` is not in itself
  exchange evidence. Do not trust arbitrary flags.
- The injected MEXC client is **not configured or connected** to an
  authenticated Render account in this branch.
- Migration 033 has not been applied; no restricted guardian role or
  isolated Render emergency-worker service exists.
- Real POST acknowledgement needs read-only order/fill and flat-inventory
  reconciliation after restart to mark VERIFIED; this component deliberately
  stops at `ACK_UNVERIFIED`.
- Exchange loss-direction stop closure in V5 remains subject to documented
  `triggerSide=1` anomaly and independent review.
- Funded CROSS/four-slot risk, separate infrastructure failover, native stop
  amendment and liquidation-safe emergency behavior remain unverified.
- Main trader's paper-only guard must not be disabled or patched around.

## Next engineering sequence

1. Implement a proper account-scope and inventory completeness
   certificate (signed REST-WS-REST, verified position/order counts and
   identity, not merely a boolean) before permitting any emergency POST.
2. Build read-only reconciliation of SUBMITTING/UNKNOWN close journals,
   exact externalOid / exchange close ID and independently flat result.
3. Review 033 and allocate an isolated least-privilege guardian role,
   then deploy an observer with mutation disabled and negative tests on
   the real dedicated account (not the manual-position Render account).
4. Only after measured proof and a separately reviewed operator approval,
   consider a single-position low-notional emergency-close drill.
5. Do not promote the T100 entry executor or 4-slot live mode until all
   independent risk and recovery gates pass.

## New 2026-11-05 read-only exchange integration checkpoint

Committed:

- `app/t100_live_guardian_inventory_witness.py`: uses
  `ReadOnlyMexcProbe` for two serial account-wide REST inventory
  cycles. Each cycle bookends `open_positions` and `stoporder/open_orders`
  with two `open_order_total_count` signed GETs, validates exact count
  categories, rejects malformed/duplicate positions and stops, and requires
  `stopOrderCount == len(open_stops)`. It compares critical identities,
  sides, quantities, stops and open-order counts across cycles while
  tolerating benign unrealized-PnL drift. It reports foreign/manual
  positions without taking ownership of them.
- The returned `GuardianInventoryWitness` is **always nonauthorizing**:
  `position_completeness_certified=False` and
  `emergency_close_authorized=False`. MEXC publishes a useful open-order
  total endpoint but **does not publish an independently reconcilable
  complete open-position total** for the GET in this workflow.
  Double GETs alone cannot provide that missing information. The witness
  is not convertible to the guardian's POST gate.
- `app/t100_live_owned_close_pg_ledger.py:read_state`: read-only
  ownership-identity-bound recovery of `PREPARED/SUBMITTING/UNKNOWN/VERIFIED`
  from the immutable migration 033 ledger. PostgreSQL 17 CI tests actual
  state persistence and wrong-ownership rejection.
- `app/t100_live_owned_close_reconciliation.py`: if journal state is
  `SUBMITTING` or `UNKNOWN`, read **exact owned**
  `GET /api/v1/private/order/external/{symbol}/{external_oid}`
  and `GET /api/v1/private/order/get/{orderId}` with matching
  externalOid, symbol, position ID, full volume, market CLOSE_SHORT,
  isolated hedge mode, filled state and zero error. Then checks the
  double REST inventory witness. **No POST, no ledger transition,
  no automatic retry.** If exact owned close filled but the position is
  absent from the responses, output
  `MATCHED_FILLED_CLOSE_OWNED_ABSENT_UNCERTIFIED_CENSUS` instead of
  inventing exchange account flatness. Other/manual positions are
  observed and never selected for mutation.
- `tests/test_t100_live_guardian_witness_reconciliation.py`: negative
  regression cases for fake/missing OID, wrong order ID, partial fill,
  wrong symbol/position, wrong order type, count drift, duplicate
  inventory, unstable manual exposure and uncertain journal state.
  Mocked GET transport asserts zero POST calls.

**Validation:** GitHub T100 live safety full-offline suite passed on
commit `1e2e90f46efaec055201ae007a916f108f34b5dc`; PostgreSQL
integration on the prior implementation commit passed. The new
collector/reconciler tests are offline mocks, not a test of MEXC account
completeness in the venue itself.

**Operational status unchanged:** current Render shadow service is on a
separate branch with its existing safety latch. No new guardian service
deployed, migration 033 not applied, no real exchange orders and no
production credential/DB changes. This is an important *implemented
read-only integration*, not proof of full production live readiness.

**Next unresolved safety gate:** establish venue-backed independent
position-inventory completeness (for example, account-private WS position
reconciliation and exact historical position/fill identities) and post-close
verified-flat semantics; then wire only those independently audited
attestations into the guardian's mutation gate. Review the restricted
database role and separate disabled-entry service before any deployment.

## Owned-position historical finality and race-safe observer (same day)

Current MEXC documentation confirms:
- `GET /api/v1/private/position/open_positions` has an exact `positionId`
  filter but no independent account-wide position total.
- `GET /api/v1/private/position/list/history_positions` returns paginated
  historical position records with `totalCount`, `totalPage`,
  `currentPage`, `state`, `holdVol`, `closeVol` and immutable `positionId`.
- `GET /api/v1/private/stoporder/list/orders` reports terminal TP/SL
  events, but a single page must **not** be considered exhaustive, and
  the V5 pilot revealed a contradictory `triggerSide` label.

New components:
- `app/t100_live_readonly_probe.py` exact positionId GET and strict
  historical position page access.
- `app/t100_live_owned_position_finality.py` bounded, metadata-checked
  full history pagination twice; demands exact position ID, SHORT,
  ISOLATED, state CLOSED, holdVol ZERO, closeVol equal to owned size,
  and twice-absent exact positionId GET. It rejects duplicate/missing
  historical IDs, changing page totals and partial closes.
- `app/t100_live_owned_close_reconciliation.py` now combines the
  previously verified exact `externalOid` market close fill with
  independently certified historical position closure. This can return
  `EXACT_OWNED_CLOSE_AND_CLOSED_POSITION_HISTORY_VERIFIED`. This is
  **specific owned-position finality**, not account-flatness or live
  authorization. It performs no ledger transition without an audited
  persistence coordinator.
- `app/t100_live_guardian_inventory_witness.py` now verifies *actual
  market-stop semantics* of the owned row rather than counting a matching
  stop ID as protection. It still cannot attest account-wide completeness
  and cannot authorize a close.
- `app/t100_live_guardian_observer.py` is a read-only diagnostic entry
  point (library callable, **not** a deployed Render worker). It checks
  durable entry ownership, double GET inventory and exact stop semantics.
  For a missing stop, it inspects the known ID in a terminal history
  page; observed terminal execution is classified as a possible
  in-flight transition, **not** a signal to submit a second market close.
  No single-page negative can certify that no stop triggered.

Safety limitations:
- A historical closed-position row plus a verified filled close can
  certify the **exact managed position** closed, even when unrelated
  manual positions remain open. Do not use this as a four-slot or
  account-wide risk clearance.
- Lossy GET access, stale history, unknown pagination and trigger-side
  mismatch remain explicit observation uncertainties.
- A **separate, reviewed, production read-only Render service** with
  correct narrowly scoped credentials and durable checkpoint wiring
  is still necessary. Code alone is not a running guardian.
- 033 is review-only and has not been applied to Render. Existing
  shadow worker, manual positions and BOXWAVE remain untouched.
- No live order placed, no MEXC mutation, no live-trading approval.
