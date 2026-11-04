# T100/V40 release gate register — issue #14

Owner: Codex engineering. Updated: 2026-11-04 (Europe/Zurich).
Initial checkpoint code tested: `ff40c29064385b552912d35954f711a6c5550e24`.
GitHub code commit: `270c3174da14d267881ab3edbfaba231322ef5b4`.
Its tree equals the tested local code tree exactly:
`4811284feefde60cb716bc628dec5394afae4949`. Commit IDs differ because
publication used the connected GitHub integration after terminal HTTPS
authentication failed.

Fetched handoff: `2669da62c6fc5bd7d7b25b1c681197914fedc629`.
Tracker: https://github.com/chrpapas/mexc-exhaustion-scanner/issues/14

**Decision: NOT READY for operator deployment review.** No release gate is
promoted by the historical profits or passing mock tests. `entry_allowed=false`,
`live_authorization=false`, `mexc_orders_sent=false`. BOXWAVE is untouched.
All rows below are owned by Codex engineering; independent review requires a
separate reviewer, and protected operations require the operator.

| ID | Status | Current evidence | Remaining acceptance work |
|---|---|---|---|
| G1 Source/strategy parity | FAIL | Fresh original and filtered chronological replays completed. Both 100/200-bps baseline entries, gate decisions and every frozen metric match. Entry audit: 146 candidates, 7,154 prior bars, 648 exact reference notionals, zero discrepancies. | Independently implement/audit full exit/fill/funding paths and four-slot runtime parity; archive has no full exit/fill paths. Broad-suite failures are resolved; independent full decision parity remains. Original replay rerun is not independent strategy implementation certification. |
| G2 Current account capabilities | BLOCKED | Keychain reads established; stable current account inventory and 98-candidate current public/private risk observations collected. | Complete contract/key capability and collateral certification; resolve unrelated-position/order isolation and cross-mode prerequisite without modifying the current account. Do not infer current rights from public eligibility. |
| G3 Venue execution/stress | UNKNOWN | Capacity, cross-margin and stop-gap models covered by focused offline suite; historical replay is candle-based. | Observed L2/partial-fill/queue/gap/funding/liquidation/multi-slot conflict evidence and model calibration. Never infer executable capacity from turnover or enable leverage. |
| G4 Fenced coordinator/journal | FAIL | Reservation, attempt/outcome and protected-commit modules; offline contention/fault tests pass. Migration 031 remains unapplied. | Complete certified runtime transport integration, full runtime transport/privilege certification beyond the isolated PostgreSQL proofs below. No local postgres/Docker available. Production migration requires approval. |
| G5 Native protection/recovery | BLOCKED | Pure/mock native-stop, checkpoint, trailing and nonflat recovery tests pass. | Independent venue stop create/update/close semantics, no uncovered interval, orphan/ambiguity and 0–4-position lifecycle proofs. Actual mutations require explicit operator approval. Never adopt manual positions. |
| G6 Fault/restart/interference | FAIL | Focused suite passes reservation contention, UNKNOWN/no retry, stale fencing, drift and restart simulations. | End-to-end runtime crash injection at every durable/venue boundary, real isolated DB evidence, four-worker/account interference review. Unit simulations alone do not close this gate. |
| G7 Render/shadow soak | BLOCKED | Repository manifest starts `app.t100_200_scanner` and `app.t100_200_trader`; draft separate shadow starts `app.t100_live_shadow_runtime`, manual deploy. Read-only Render inspection now identifies live deploy 9073ce99; recent samples remain kill-latched (see current-render-shadow.json). This is an older build, not this PR. | Operator-approved deployment, actual running SHA, sustained uninterrupted soak, Discord delivery evidence. No deployment/service change authorized or performed; preserve kill latches. |
| G8 Independent release/security review | FAIL | Readiness fixes committed; focused CI suite passes; rollback/emergency runbooks exist. | Independent code/DDL/security/dependency review, current-head CI, verified rollback/incident drill; broad-suite failures are resolved. No reviewer approval claimed. |
| G9 Real venue lifecycle pilot | BLOCKED | No orders sent. Adapter remains `NOT_IMPLEMENTED`. | BLOCKED_PENDING_EXPLICIT_OPERATOR_AUTHORIZATION. Separately reviewed loss-bounded restricted lifecycle plan and verified controls before any order/stop POST. Full four-slot production cannot be the first lifecycle test. |

## Evidence and reproduction

All artifacts are in [evidence/issue-14](evidence/issue-14/). `provenance.json`
records exact source/archive/filter hashes, simulator dependency hashes and
fresh output hashes. Replay outputs are research-only under
`/private/tmp/t100-issue14-{baseline,filtered}-20261102`; no original dataset,
simulator, production configuration or credential was edited.

- `frozen-entry-parity.json`: `python -m scripts.v40_historical_parity_audit ~/v40_parity_for_live.zip`.
- `fresh-baseline.log`, `fresh-filtered.log`: use `python -m scripts.v40_api_filtered_original_runner` with the handoff source/archive/manifest/root, a new output directory, `--execute`, and `--baseline` for baseline. Set paper mode, order API false and kill switch true. No network or orders.
- `fresh-replay-comparison.json`: `python -m scripts.v40_api_filtered_replay_compare` with those two output directories and pinned archive/manifest.
- `baseline-two-cost-frozen-fields.json`: all frozen entry, metric and gate fields at 100 and 200 bps; `PYTHONPATH=. python docs/evidence/issue-14/rebuild_provenance.py` after fresh outputs exist. The script records the current code SHA; do not reuse this run's SHA for later changes.
- `safety-suite.txt`: exact unittest command from `.github/workflows/t100-live-safety-offline.yml`: **1,604 tests PASS**.
- `readiness-regressions.txt`: pytest of the two changed comparator/parity-gate test modules: **10 tests PASS**, 41 subtests.
- `project-verification.txt`: `python scripts/verify_project.py`: PASS.
- `offline-tests.txt`: full pytest excluding only the two explicitly CI-only PostgreSQL modules. **Release-blocking failures remain**; see final summary in log. Integration modules were not run or silently counted as passed.

Fresh $2,000 HYBRID replay: original/filtered final equity at 100 bps per side
$14,476.81 / $13,356.21; at 200 bps $9,030.38 / $8,089.53. Original/filtered
high-envelope drawdown at 100 bps -54.8664% / -55.9209%; at 200 bps
-63.7195% / -66.2204%. Historical results only. Public filter observation is
2026-11-01, not current private-account permission or historical access.
Funding source emits WAL-without-WAL warning: source is opened read-only and
immutable, but missing prior uncheckpointed transactions cannot be excluded.
No completeness certificate is inferred from reproducing its outputs.

## Failure investigation and next safe work

The initial broad run lacked pytest-asyncio: add it to declared dev dependencies;
then rerun rather than reporting async tests as strategy failures. Remaining:

1. `test_t100_live_pilot_coordinator`: stale tests construct nonexistent
   `V40Admission(symbol=...)`; coordinator also refers to removed admission
   fields. The one-slot prototype is incompatible and must not be promoted
   or repaired into a four-slot authorization path. Explicit retirement or
   separate reviewed prototype disposition is required.
2. `test_cleanup_v1313` and two `test_worker_v125` cases: incomplete
   `SimpleNamespace` settings fixtures omit `snapshot_audit_enabled` used by
   worker. Fix fixtures against the current settings contract without
   changing production scanner semantics.
3. `test_persistence_v2_v156` and `test_reporting_cleanup_v155`: legacy
   research expectations differ from the current promoted strategy. Establish
   whether these should test a named historical strategy or current behavior;
   do not change strategy defaults to make assertions pass.

Code changes in this checkpoint strengthen replay comparison from symbols and
final equity alone to every frozen entry/metric field; reject boolean/float
parity counters; declare async test support; trigger safety CI for replay
script and dev-dependency changes. None changes admission or order execution.

## Operator decision memo

The candidate supports offline research and nonauthorizing development tests.
A read-only shadow deployment can be proposed only after applicable tests,
independent review and exact running-build/secret-scope preparation; none is
requested here. Secure read credentials and isolated CI database execution
are the next evidence prerequisites. Production deployment, DB migration,
credential/key permissions, exchange mutations and real funds remain outside
this authorization. Runbooks and emergency controls require independent
verification before any separately approved pilot or rollout.

## CI checkpoint after publication

At `5fa1d5d68f0b7063620c09cb518a045e770b789a`, all five PR checks
completed successfully, including the real PostgreSQL ephemeral integration:

- [Real PostgreSQL transaction/recovery integration](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41408388457/job/125155537682): PASS.
- [Focused offline safety suite](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41408388440/job/125155537862): PASS.
- [Dashboard validation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41408388447), [promotion validation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41408388475), [service split validation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41408388488): PASS.

This advances G4/G6 isolated database evidence, not full exchange-runtime
certification. No production database was accessed. The broad local run
still has 18 failures (including subtests), 2,001 passes and 517 passing
subtests. Independent review and the remaining gate actions are unresolved.

## Continued engineering — broad-suite failures resolved

2026-11-02: `offline-tests-followup.txt` records **2,005 passed, 520 passing
subtests**, with the two CI-only PostgreSQL modules excluded as before.
Scanner fixtures now declare the current audit setting. Persistence research
fixtures supply a valid ATR witness and assert the current promoted strategy;
production strategy defaults are unchanged. The unused one-slot coordinator
is explicitly retired: every submission raises before any supplier or gateway
call. Its obsolete happy-path tests are replaced by unconditional-rejection
coverage using admitted, skipped and missing admission objects. The focused
CI workflow now includes this retirement regression.

This supersedes the earlier broad-suite failure blocker, while retaining its
original log for provenance. G1/G4/G6/G8 remain open for the remaining runtime,
full-path parity and independent-review requirements. Secure credential
location has been requested for G2; no credentials were printed or changed.

## Current account evidence — Keychain access established

2026-11-02: existing `MEXC_API_KEY` and `MEXC_API_SECRET` Keychain service
entries were read directly into process memory, without secret logging or
credential changes. Signed access returned HTTP 200 / POSITIONS_READ_SUCCEEDED.
The GET-only completeness probe (with a transport hook rejecting non-GET)
verified stable current inventory, count reconciliation, margin witness and
symbol-scoped redundancy. See `current-private-access.json` and
`current-account-inventory.json`.

G2 credential availability is resolved. Account reconciliation remains BLOCKED:
one observed nonflat position, zero current TP/SL rows, one unrelated limit
order, and no verified durable T100 recovery provenance. No position or order
was adopted, canceled, closed or modified. Completeness is explicitly
non-atomic and does not certify protective stop execution or trading rights.
This supersedes earlier statements that current read credentials were unavailable.

## Continued recovery and independent replay audit

The real GET adapter now binds the completeness witness to a fingerprint of
its own critical positions/stops. Equal row counts from a different inventory
are rejected. Protected-attempt observation also rejects pre-existing margin
mode changes and boolean values masquerading as integer venue state codes.
Regression coverage includes a complete but different inventory, old-position
margin drift and malformed state values.

An independent CSV audit (`scripts/v40_replay_cashflow_audit.py`) recomputes
per-side fees/slippage and exit PnL, validates exit origins, rejects duplicate
open symbols and checks four-slot occupancy. It models opening trims/delayed
exits before admission and normal exits after admission. Both original and
filtered HYBRID runs pass at 100/200 bps. These are exported-trace checks, not
independent signal/exit-decision certification.

`independent-funding-equity.json` and `rebuild_funding_equity.py` independently
reconstruct funding from local SQLite data and remaining exported notionals,
using strictly earlier close marks for funding and final observed close marks
for open positions. Funding event counts match exactly (2,536/2,535 baseline,
2,266/2,266 filtered). Largest final-equity difference is below 0.000005 USDT.
The candle DB is read with WAL-aware read-only transaction semantics; the
funding DB uses the original immutable main-file snapshot with its documented
missing-WAL completeness limitation. This diagnostic script is supporting
research evidence; full decision parity and independent drawdown remain open.

Current account risk matrix: all 98 historically filtered candidate contracts
were checked with two public and two private GETs each, with pacing and a
non-GET transport rejection hook. Public eligibility and private short-tier
observations were stable, but none corroborated cross mode. A separate
four-GET witness for candidate index 0 independently agreed on ISOLATED mode
across risk-limit and leverage endpoints. This is not a mode-change request,
future order permission, full-V40 size or liquidation certificate. No account
settings, manual positions or orders were modified. See the redacted matrix
and margin witness JSON files for exact observation timestamps and filter hash.

The funding reconstruction now has a synthetic regression proving strictly prior marks, exclusion of entry-time funding, and funding-before-same-bar-exit ordering at both cost levels. All audit and recovery regressions are included in focused CI. See `recovery-binding-tests.txt` for the final broad-suite result for this checkpoint.


## Wall-clock fencing, marked equity and current shadow checkpoint

The lease store now uses PostgreSQL `clock_timestamp()` for expiry checks and
renewals. Transaction-start `now()` could otherwise accept an expired lease
inside a long transaction. Malformed fencing tokens are rejected before store
access. A real isolated PostgreSQL regression expires a lease inside an open
transaction, rejects its old token and renewal, then verifies a new token.
The diagnostic fake was updated to recognize the new renewal predicate.
Local suite: **2,021 passed, 552 subtests**, excluding only the two explicitly
CI-only PostgreSQL modules; project verification and diff checks pass.
See `lease-wall-clock-tests.txt`. CI on the published head remains required.

`rebuild_marked_equity_drawdown.py` independently reconstructs all 63,853
half-hour bars from prescribed exported trades, prior funding marks and local
candles. Its output reproduces `independent-marked-equity-drawdown.json` exactly.
Across both cohorts and both cost levels, final equity differs by less than
0.000001 USDT, entry equity by less than 0.000001 USDT, and drawdowns by less
than 0.00005 percentage points (the frozen metric rounding precision).
The synthetic regression verifies high-price drawdown and funding before a
same-bar exit, excluding entry-time funding. This closes the independent
marked-equity arithmetic task, not full independent signal/exit-decision parity.
The funding source completeness limitation remains unchanged.
Reproduce with the evidence script's --data-root, --baseline and --filtered
arguments pointing to the original read-only sources and fresh output folders.

Read-only Render inspection found the existing manual-deploy T100 shadow on
`9073ce99d31ad86132db35335a54746d7d5a1ad3`, deploy
`dep-dbh0qhmc6qcc73cq3kkg` (live since 2026-10-28). The bounded 2026-11-02
12:52–13:13 UTC sample reports stable critical observations but
`OBSERVING_KILL_LATCHED`, `NATIVE_PROTECTION_LOST`, position observed and no
TP/SL observed. Entry and live authorization are false. This is neither this
PR's running-build attestation nor an uninterrupted successful protection soak.
No service, deployment, latch, account setting, position or order was changed.
G7 remains blocked; `current-render-shadow.json` preserves the bounded evidence.


## Database submission-boundary fault matrix

All five CI checks passed at `a9677c67d8d1f62bff13b8fb8b0b6d7b21ea0692`
(tree identical to local `a40680c9`), including the long-transaction lease
expiry regression on isolated PostgreSQL 16.

The next isolated PostgreSQL matrix injects failures at all three durable
staging writes (intent INSERT, intent transition, slot transition), and both
UNKNOWN-latch writes. It requires rollback to RESERVED/no intent before any
transport, or SUBMITTING/SUBMITTING after an ambiguous outcome; restaging must
remain blocked after SUBMITTING and UNKNOWN. Manual inventory is unchanged.
These tests use the existing disposable localhost CI guard and no venue client.
They strengthen DB-boundary evidence; real process/network crash recovery and
full runtime transport remain separate open G4/G6 requirements.


## Independent stop/trailing decision audit

`scripts/v40_exit_trigger_audit.py` uses no simulator imports. From the
prescribed entries it reconstructs prior-bar trailing state, adverse-first
75% pre-trail stops, 14% activation / 0.5% trailing gap, gap-adjusted buyback
prices and pending delayed-exit state. It rejects missing candles, lookahead
trailing exits, invented delayed exits and optimistic gap prices.

Both cost cases pass: baseline 108 positions each, 210 normal exit fills and
45 delayed fills across the two cases; filtered 98 positions each, 190 normal
exit fills and 38 delayed fills. No pending mandatory exits remain. See
`independent-{baseline,filtered}-exit-triggers.json`. Synthetic tests establish
adverse-first precedence, next-bar trail activation, partial/pending handling,
gap fills and missing-bar rejection. Broad suite: **2,025 passed, 554 subtests**
(`exit-trigger-tests.txt`); only the two guarded real-PostgreSQL modules excluded.

This closes the recorded stop/trailing trigger and price audit, not independent
entry selection, opening risk-trim decisions, lot/capacity quantity decisions
or full runtime parity. Those remain explicit G1 work. The reported remaining
notional uses the same 0.001-USDT exported-CSV reconciliation tolerance as the
cashflow audit, not an exchange dust waiver or proof of executable closing lots.

The preceding DB fault-matrix head `043a1ea059be94008e68608c0b25fafa49abc57e`
passed all five CI checks, including every injected real-PostgreSQL write fault.

## Prior-close risk-trim audit

The independent `scripts/v40_risk_trim_audit.py` computes frozen pooled-proxy
reduction and largest-exposure-first allocation from reconstructed prior-close
equity after funding. It imports no simulator code. All four traces pass:
baseline 100/200-bps plans/fills 0/0 and 1/1; filtered 0/0 and 2/2.
Every recorded trim is within its independently requested reduction; see
`independent-risk-trim-plans.json`. Synthetic tests verify headroom algebra,
ordering, nonpositive equity and malformed-input rejection. Full offline suite:
2,028 passed, 557 subtests (`risk-trim-tests.txt`). Exact capacity/lot fill
quantity certification remains open; this is not a current venue risk model.
Reproduce by invoking `rebuild_marked_equity_drawdown.audit` with the original
read-only data root, fresh baseline/filtered folders and `audit_trims=True`.

## Independent lot inventory and prior-bar capacity audit

`scripts/v40_lot_capacity_audit.py` independently verifies recorded contract
units, USDT fill notionals, minimum/step/maximum child-order partitions, exact
remaining lot inventory, and shared per-symbol/bar quota against the strictly
previous candle's raw amount. Source databases are opened read-only with WAL
visibility. Across four scenarios, 848 known-spec fills pass; 50 unknown-spec
fills have quota checks but explicitly no contract-lot certification. Filtered
runs have two unknown-spec fills per cost case. No overclose or subminimum
known-lot residual is accepted. The three open known positions per scenario
are reconciled inventory, not a claim of a flat terminal portfolio.

Artifacts: `independent-{baseline,filtered}-lot-capacity.json`. Synthetic tests
reject impossible child partitions, optimistic quota substitution and residual
dust. Full suite output is `lot-capacity-tests.txt`. The snapshot is the pinned
research `t100-cross-margin-solvency-v36/mexc_current_public_contract_specs.json`;
current specifications do not certify historical venue rules. This audits
recorded fills, not absent/zero-fill attempts or independent optimal sizing.
Full sizing-decision/runtime parity and actual L2 capacity remain open.

## Admission sizing from independently reconstructed portfolio state

The marked-equity audit can now run with `entry_specs` from the pinned snapshot.
At each recorded admission, it invokes the repository admission/lot policy
using freshly queried 49-bar history, independently reconstructed equity and
marked exposure, actual occupied slots and opening-fill quota usage. All 412
entries across four traces reproduce both requested pre-liquidity size and
final lot-adjusted size. Maximum final-size difference is 0.0000004883 USDT.
See `independent-reconstructed-entry-sizing.json`. A regression rejects a
prescribed admission when its causal history is missing; the existing admission
policy suite also passes. Full suite: `reconstructed-sizing-tests.txt`.

This specifically strengthens repository-policy parity against the original
simulator using independent portfolio state. It still starts from prescribed
admission events: skipped/rejected candidates, absent fills and full autonomous
runtime are not certified. Snapshot lots are research assumptions, not current
account permission. The previous lot-audit head 75672c3b passed all five CI checks.

## 2026-11-02 — GET-backed protected finalization and real PostgreSQL termination evidence

Development branch `codex/t100-readiness-issue-14`, tested head
`4688cfddcb6b7122e5e1195374bb5d6ff02f53f4`. This evidence
supersedes earlier claims that GET-to-disposable-PostgreSQL linkage was absent.

- Added `app/t100_live_four_slot_protected_get_evidence.py` and read-only
  `inspect_staged_four_slot_prepared_get_only`. For **already staged**
  SUBMITTING attempts, it gathers two full-account position/stop, exact-origin
  order, complete order-deal and completeness GET bookends. It rejects
  unresolved order origins, changed fills, duplicate/missing/partial deals,
  unmatched stops and drift, and **only prepares an offline bundle**.
  Nothing in this bridge retries a venue POST, grants a permit, persists an
  ACK, edits a stop or touches the production database.
- `tests/test_t100_live_four_slot_simulated_lifecycle.py` now tests staged
  journal -> verified GET bridge -> rollback-capable fake-DB atomic commit ->
  independently reconstructed four-position restart, with crashes, UNKNOWN,
  stale fencing and all seven injected commit write failures.
- `tests/test_t100_live_v40_postgres_integration.py` additionally consumes a
  **fresh mock PRIVATE GET-backed bundle** and writes it through the actual
  `PostgresProtectedV40Commit` under disposable PostgreSQL 16. Missing
  orders, unavailable or partial deal history, missing stops and GET timeouts
  never mutate the database. Exact persisted fill digest and duplicate-commit
  refusal are checked.
- One more CI-only test **terminates the actual PostgreSQL backend during
  checkpoint INSERT**, after earlier ACK/fill writes inside the transaction.
  It independently rereads from a fresh connection and verifies exact
  precommit state is restored. With the trigger removed, protected
  finalization can proceed without any duplicate simulated exchange order.
  This is stronger than a controlled SQL `RAISE EXCEPTION` alone.
- At the above head all FIVE GitHub Actions checks PASS:
  [isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538175660),
  [offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538175795),
  [scanner/trader split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538175602),
  [promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538175697)
  and [dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538175484).

**Scope:** This closes specific *offline connection-loss rollback* and
*mock-GET-to-real-isolated-DB* subtasks in G4/G6. It DOES NOT close G4/G5/G6,
nor certify venue POST/stop create/amend/close, distributed exchange/DB
atomicity, genuine process crash/restart on Render, full 0–4-slot executor,
account cross-collateral and current margin tiers, mandatory protected soak,
or independent reviewer sign-off. Migration 031 remains unapplied in
production; no service deployed. `LIVE_ADAPTER_STATUS` remains
`NOT_IMPLEMENTED`; `entry_allowed=false`, `live_authorization=false`,
`mexc_orders_sent=false`. BOXWAVE untouched.

**Next critical tasks:** true independent current account margin/collateral
and stop semantics verification (with separately authorized venue
lifecycle evidence), completed full four-slot runtime transport and
non-flat recovery, operator-approved **read-only** current-build Render soak,
and independent release review. Do not bypass these with passing CI.

### Complete fourth-slot SQL reservation -> stage -> GET -> commit -> restart witness

An additional disposable PostgreSQL 16 test now begins before the
SUBMITTING order state, using the **real** V40 four-slot reservation and
attempt journal instead of a manually pre-seeded SQL row:

1. DELETE only the test's seeded fourth-slot order/slot in ephemeral CI.
2. Inspect the three existing owned positions against actual restricted
   `t100_live_executor_runtime` PostgreSQL SELECT-only
   REPEATABLE READ snapshots and mock signed MEXC private GETs.
3. Apply the repository V40 planner, take a leader-fenced reservation,
   and atomically transition it to SUBMITTING in PostgreSQL.
4. Observe a **simulated** fully filled fourth entry and exchange-native
   stop through two GET-only order/deal/position/stop bookends.
5. Commit the exact GET-derived protected batch as ACK, immutable fills,
   OPEN checkpoint and revision through the real database transaction.
6. Restart from the REAL restricted PostgreSQL reader and independently
   re-GET all four origins/positions/stops. The four managed positions must
   recover with `live_authorization=false`.

The test originally demonstrated a correct privileged-reader rejection
(`LEDGER_READ_OR_CONTRACT_FAILED`) when pointed at the migration admin
role. It now creates and uses the required restricted read-only CI role
before proceeding; no production role/grant was modified.

**Verified head:**
`6053339c9cb702df9b5b9206a6e044cf1911b6ed`.
At that SHA **5/5 workflows passed**:
[PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538891672),
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538891465),
[scanner/trader split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538891747),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538891760),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41538891570).

G4/G6 now have actual real-SQL full *simulated* lifecycle integration.
This is NOT proof of an actual MEXC order POST, protective stop create/update
semantics, exchange/database distributed atomicity, current account exposure
and collateral, production Render service recovery or real-money safety.
The live adapter is NOT_IMPLEMENTED, migration 031 is not deployed, and
independent release review is outstanding. G4/G5/G6/G7/G8/G9 still BLOCK.

## 2026-11-03 — Native-stop duplicate-mutation and protection hardening

Safety-hardening branch `codex/t100-readiness-issue-14`, reviewed tested
source SHA `c4aa4e88bfe83929b4f4873f2bcfe6fe77465acc`.

1. **Competing stop workers:** `StopAmendmentLifecycleCoordinator.mark_submitting`
   can NO LONGER report success when it merely sees an already-SUBMITTING
   state. A worker that loses the durable PREPARED -> SUBMITTING
   compare-and-swap must reconcile, never POST. Regression test synchronizes
   two workers at a stale PREPARED read and requires exactly ONE mocked stop
   mutation request across both, including subsequent restart.
2. **Unjournaled mutation bypass disabled:** `LiveMexcAdapter.tighten_native_short_stop`
   (old send-then-read shortcut) now unconditionally raises
   `LEGACY_STOP_MUTATION_REQUIRES_DURABLE_JOURNAL`, even with a valid
   test permit. The approved *future* transport interface remains
   `submit_native_short_stop_tightening`, invoked only by the durable
   stop executor. Prevents a second unauthorized stop-edit path.
3. **Before durable stop VERIFIED:** `StopUpdatePersistencePipeline`
   checks exactly one active, unfinished **SHORT-side** native SL row
   with effective protective volume equal to the full checkpoint contracts.
   Checks both pre/post when bookended; checks current row when restarting.
   Rejects missing/partial/zero volume, inactive/finished or wrong-side stop,
   preventing a misleading VERIFIED journal before checkpoint rejection.
4. **Checkpoint ownership bind:** the same pipeline requires an OPEN
   PositionCheckpoint matching the stop amendment's position ID, symbol,
   original native stop ID and original stop price **before** the amendment
   can be marked VERIFIED. Mismatched/REVIEW checkpoints or restarts leave
   SUBMITTING/UNKNOWN unresolved and do not persist a checkpoint.

All five workflows passed at the tested source SHA:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41543481729),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41543481691),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41543481709),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41543481686),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41543481690).

**Evidence boundary:** these are developer safeguards and in-memory concurrent
test cases; they do NOT prove actual native-stop create/amend/close behavior,
current MEXC credential rights, account collateral, stop execution under
gap/liquidation, or a four-slot live runner. They do not close G5/G6.
Live adapter certification remains NOT_IMPLEMENTED; manual positions are
untouched. No MEXC POST, Render deploy, production DB write, key change or
BOXWAVE modification. Production release and real trading remain blocked.

### 2026-11-03 — Direct stop-coordinator bypass is also fail-closed

Source SHA `abf9e503c9a6943136c3d228bc518efac64ca4cf`:
`StopAmendmentLifecycleCoordinator.reconcile_observed_transition` and
`reconcile_current_outcome` now require an explicit positive Decimal
`position_quantity` and insist on one active unfinished SHORT-side native
stop with effective protection equal to those contracts before any durable
journal `VERIFIED` transition. The higher-level no-POST stop pipeline
passes quantity from its immutable OPEN checkpoint and retains its own
separate provenance/coverage check. Even a future *direct* caller cannot
silently omit this evidence. Regression tests cover partial, wrong-side,
finished and untyped stops at SUBMITTING and UNKNOWN restart boundaries.

The exact SHA passed isolated PostgreSQL
https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41544066200
and offline safety
https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41544066205
plus scanner-trader separation
https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41544066199
and promotion
https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41544066209 .
This is still simulated/pure-Python journal verification: current venue
stop amendment shape, actual MEXC execution, multi-worker deployed fencing,
cross collateral and live authorization remain unverified and BLOCKED.

### 2026-11-03 — Legacy entry ACK must be backed by a full native stop

During a source audit, the old single-position `LiveOrderGateway` was
observed transitioning a newly HTTP-accepted order from SUBMITTING to ACK
*without independent native-stop verification*. This pathway is LEGACY
(one-position, not V40-parity) and remains blocked for production; hardening
its accounting boundary is nevertheless important before any release.

The development branch now:
- Retains `SUBMITTING` after an HTTP successful entry submission.
  The received order ID is **not** durable proof of a protected position.
- Keeps an accepted-but-unfilled order in its unresolved journal state;
  future fills must be checked, not mistaken for a protected ACK.
- Rejects invalid order IDs, non-short side, malformed/nonfinite and
  partial/nonfinal deal volumes, and nonpositive/malformed position IDs
  **before** persisting an ACK.
- For filled positions, requires exactly one native short-side, active,
  unfinished stop with matching symbol, position ID, **positive native
  stop ID**, bounded stop price and full effective contract quantity.
  Missing, duplicate, wrong-side, inactive, finished, undercovered, or
  nonnumeric stop records leave the intent SUBMITTING/UNKNOWN; a lookup
  alone never permits exchange POST retry.
- Only after the entire stop witness is accepted may a conditional
  durable SUBMITTING/UNKNOWN -> ACK CAS occur. This gateway still cannot
  certify account-wide completeness or exchange atomicity.

Safety/regression tests cover successful mock reconciliation, accepted
unfilled orders, invalid order metadata, missing stop identity, duplicate
stops, partial/wrong-side native SL and no retry across ambiguous states.

**Exact tested source** `0f378c9d0223a38467246e7e1ff6de7f8bad4fb3`:
5/5 workflows PASSED:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41596810170),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41596810265),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41596810159),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41596810164),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41596810218).

**No live approval inferred.** The legacy gateway remains NONPARITY,
and successful unit tests do not authorize sending orders. Full four-slot
venue transport, native stop creation/amend/close, cash/collateral
verification, Render restart and protected soak, and independent release
review remain BLOCKED. Manual position untouched. No real MEXC POST,
production DB change, Render deployment, credentials or BOXWAVE changes.

### 2026-11-03 — One-shot exchange POST possibility claim (unapplied migration 032)

**Engineering evidence only; no MEXC POST.** Introduced CREATE-ONLY,
unapplied `migrations/032_t100_live_v40_dispatch_claims.sql` and
`PostgresFourSlotDispatchClaims`. A claim can be inserted only if:
- a verified current leader fencing token matches the active lease and
  the exact reservation holder/fence;
- an existing `SUBMITTING` V40 slot and `SUBMITTING` order-intent row
  match the exact signal, symbol and `external_oid`;
- staged snapshot age is bounded before the claim; this is a
  refusal gate, **not** live exchange/account risk evidence;
- the account-wide V40 advisory TX lock is acquired before all reads;
- the unique immutable `signal_id`/origin claim is absent.

Once one transaction commits the `POST_POSSIBLE_UNRESOLVED` row, the
same or another worker and even a new leader can never claim it again.
A future order transport must treat the row as *possibly POSTed*, avoid
retries on absent/unknown GET, and require separately certified
exchange/account preflight, permits, native stop rules, and explicit
operator approval before any real mutation. This module has NO POST,
no MEXC credential usage, no order recovery retry, no claim release
or reset method, and always emits `entry_allowed=false` and
`live_authorization=false`.

Disposable PostgreSQL 16 tests verify:
1. exactly one persisted claim; restart sees a duplicate and can only
   reconcile,
2. two competing workers yield exactly one successful claim, 
3. mismatch, UNKNOWN journal, and stale leader block before INSERT,
4. terminating the database backend *after INSERT but before COMMIT*
   rolls back the uncommitted claim, leaves stage SUBMITTING, and sends
   no order, while a later fresh SQL-only claim can proceed.

**Exact tested head:** `7f53400e64c347cc2256dd5f21fd89edcce3c13f`.
All FIVE GitHub checks PASSED:
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41597556488),
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41597558722),
[scanner/trader split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41597556545),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41597556559),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41597556557).

**Important:** this makes the durable V40 POST-attempt ownership design
testable; it is NOT exchange order submission, not an exchange execution
certificate, and not independent enforcement of a current account risk
snapshot. G4/G5/G6/G7/G8/G9 remain OPEN. Migration 031 and 032 are
NOT approved for production deployment or runtime privileges. No real
orders, stop changes, Render deployment, production DB mutation,
credential change or BOXWAVE change.

### 2026-11-03 — Immutable dispatch claim + restricted restart proof bound to protected GET

**Development only.** The CREATE-ONLY V40 dispatch-claim migration 032
now includes a PostgreSQL BEFORE UPDATE OR DELETE trigger that rejects
ordinary record mutation with `T100_DISPATCH_CLAIM_IMMUTABLE`. A
committed `POST_POSSIBLE_UNRESOLVED` claim cannot accidentally be
revised/deleted by SQL DML, including privileged CI UPDATE/DELETE.
Database DDL/TRUNCATE permissions remain security-critical and must not
be granted to runtime roles. Migration 032 is still **UNAPPLIED** in
production.

New `PostgresFourSlotDispatchClaimReader` (SELECT-only; verified
`t100_live_executor_runtime` account, READ ONLY REPEATABLE READ, exact
database) checks the full immutable claim, order-intent/slot identity,
holder and original fencing token after restart. A missing, divergent
or unreadable claim gives NO POST authorization. A matched claim means
**RECONCILE ONLY**, never new order submission, including after UNKNOWN,
ACK or PROTECTED transitions. CI also rejects privileged/admin reader
context and mismatched claims.

New `observe_claimed_staged_protection_get_only` and signed-client
wrapper `inspect_claimed_four_slot_prepared_get_only` require the
restricted SQL claim evidence **before any venue GETs**. If no exact
committed claim exists, no exchange reads, checkpoint preparation,
durable ACK or POST occurs along this entry point. An exact claim
permits only *read-only* externalOid / order deals / position / native
stop bookends and an offline-only prepared bundle, never exchange
mutation or live authorization.

The complete integrated CI case now demonstrates:
- no claim -> zero MEXC-mock GETs and zero SQL ACK writes;
- durable SQL-only claim -> restricted PostgreSQL reader verifies
  immutable ownership -> mock PRIVATE GETs independently verify exact
  completed fill and protective stop -> `PreparedProtectedBatch`;
- the preexisting atomic protected DB writer persists the exact
  observed bundle, with committed claim still present;
- all outputs explicitly keep `entry_allowed=false`,
  `live_authorization=false`, and `mexc_orders_sent=false`.

**Exact tested source SHA**:
`7db1dbaf23c67d6d85857c90dadbc5c1136049fe`;
all 5 GitHub workflows PASS:
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599437208),
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599437230),
[scanner/trader split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599437192),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599437246),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599437389).

**Still NOT RELEASE-CERTIFIED:** old non-claim-bound offline
`PostgresProtectedV40Commit.persist` callsites exist and are not yet
enforced to use the immutable claim in the *same* DB transaction;
exchange venue POST/native stop semantics, 4-slot runner, margin tiers,
account collateral, crash/restart on Render and sustained protected
soak, independent reviewer approval and separately authorized live
pilot are all missing. This work must NOT be interpreted as exchange
atomicity or a real execution certificate. The manual position remains
unmanaged, and G4/G5/G6/G7/G8/G9 stay OPEN.

### 2026-11-03 — Dispatch claim independently checked in protected DB transaction

A new explicitly named `PostgresProtectedV40Commit.persist_claimed`
entry point requires **each** exact immutable V40 one-shot dispatch claim
(signal ID, externalOid, original holder/fencing token and
`POST_POSSIBLE_UNRESOLVED` state) inside the SAME PostgreSQL transaction
as durable ACK + immutable fill-set/fill + position checkpoint + slot
PROTECTED writes, under the shared advisory lock and current leader fence.
If the claim is absent, wrong-origin or wrong-fence, the transaction
fails BEFORE ACK/fill/checkpoint writes.

The older `persist` method is retained for historical **offline-only
fixtures** and is NOT an approved live runner entry point. It remains
possible to bypass claim checking through that legacy API in tests;
future release integration must remove/forbid all non-claim-bound
production callsites and review runtime privileges and credentials.
No live execution is wired to either method.

The real disposable PostgreSQL integration test now follows:
restricted read-only claim reader -> exact mock PRIVATE GET
order/deals/native stop bookends -> claim-bound same-transaction
DB ACK/protected commit. Another test supplies an otherwise valid
prepared bundle with NO claim or with a mismatched immutable claim;
both fail closed and preserve the original DB state.

Tested source SHA:
`0669a2f60fd5d3a53405251b13d700f7d4ca54cb`.
All 5 workflows PASSED:
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599870949),
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599870905),
[scanner/trader split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599871026),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599871045),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41599871069).

These are mocks of venue positions/fills/stops and real **ephemeral**
PostgreSQL. No MEXC POST, stop changes, production schema writes,
Render deployment, credentials, or BOXWAVE modifications. This advances
the G4/G6 database/attempt-provenance implementation but does NOT certify
real exchange behavior, current cross collateral, a four-slot live
executor or crash/restart in production. Keep all live gates BLOCKED.

### 2026-11-03 — Readiness false-green prevention: V40 claims, reservations, REVIEW and orphan ACK

**New read-only V40 state gate:** `app/t100_live_v40_state_gate.py`
checks for BOTH migrations 031 and 032, counts nonfinal reservations
(RESERVED/SUBMITTING/UNKNOWN/ACK/HALTED), unresolved one-shot claims,
PROTECTED reservations without an exact dispatch claim, and immutable
claim/slot/order-intent identity/ownership drift. Missing tables,
inaccessible or malformed rows, and any unresolved count block
`v40_unresolved_dispatch_state_absent`. An exact settled
PROTECTED/ACK/claim chain can be called `V40_DB_STATE_CLEAR_NONAUTHORIZING`,
but this DOES NOT certify native stops, venue atomicity, collateral,
current inventory, margin permissions or operator authorization.
The existing read-only `run_dynamic_readiness_bundle` now requires the
V40 state gate in addition to its older dynamic checks. An account can
never report `dynamic_readiness_verified` when 031/032 are absent or
V40 dispatch outcomes unresolved.

**Expanded legacy journal gate:** `assess_unresolved_live_state` now
blocks REVIEW order intents and ACK order intents without a matching
OPEN/non-CLOSED protected checkpoint (including exact externalOid and
exchange_order_id), in addition to PREPARED/SUBMITTING/UNKNOWN,
unresolved stop amendments and REVIEW/CLOSING checkpoints. An accepted
HTTP order or unprotected ACK cannot bypass this durability gate.

**Evidence:**
- Mocks test missing/partial V40 schema, real asyncpg.Record-like row
  shapes, invalid counts, read failures, all pending/ambiguous V40 states,
  and the combined bundle with otherwise clean old journals.
- Real disposable PostgreSQL 16 tests prove the new V40 gate follows
  SUBMITTING -> durable one-shot dispatch claim -> atomic protected
  V40 slot/ACK while remaining `live_authorization=false`.
- Additional real PostgreSQL test proves REVIEW and orphan ACK fail
  the legacy dynamic journal gate and that an atomic protected
  checkpoint can clear those particular journal blockers.
- No MEXC calls, live order settings, production migrations, Render
  deployment, manual account mutation or BOXWAVE changes.

**Exact tested SHA:** `631fc96ecde36e093518319bd993420a6ff091f0`;
5/5 workflows passed:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41646000123),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41646000141),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41646000127),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41646000102),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41646000267).

**Gating remains:** migration 031/032 unapplied production, active
account manual position unprotected, actual venue/native-stop execution
not certified, current account collateral and tier envelope unverified,
no deployed current-code protected Render soak, no independent release
signoff, no authorized tiny venue pilot. G2/G4/G5/G6/G7/G8/G9 OPEN.

### 2026-11-03 — V40 immutable claim runtime privilege audit and dynamic blocker

Added a read-only effective permissions/trigger audit:
`app/t100_live_v40_dispatch_privilege_audit.py`. It inspects the
actual `t100_live_executor_runtime` login inside a verified READ ONLY,
REPEATABLE READ transaction on `mexc_exhaustion` and checks:
- the V40 claim table is NOT owned by the runtime;
- runtime role has SELECT and INSERT but NOT UPDATE/DELETE/TRUNCATE/
  TRIGGER on `t100_live_v40_dispatch_claims`;
- runtime cannot CREATE objects in public schema or in database;
- the actual immutable UPDATE/DELETE-rejecting PostgreSQL trigger exists
  and is ENABLED on the intended claim table.

Malformed evidence, missing table, privileged/wrong login, absent
INSERT, excessive rights, disabled trigger or read failure blocks.
`run_dynamic_readiness_bundle` now requires the privilege audit to
PASS, in addition to state/schema/account checks; an operator/admin
connection or unconfigured runtime cannot be labeled dynamically ready.
The status remains *NONAUTHORIZING* regardless of a PASS.

Real disposable PostgreSQL 16 CI tests explicitly grant/revoke an unsafe
DELETE privilege and disable/restore the immutable trigger to verify
the report is blocked when either invariant is violated. These
test-only changes run only on CI's local ephemeral PostgreSQL
container. No live role grant, schema change or deployment occurred.

**Verified tested SHA:**
`468e688585eef1b5bc1cf772be67e068c09f7a06`.
All 5 GitHub checks PASS:
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647114828),
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647114740),
[split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647114691),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647114696),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647114697).

**Unresolved:** migration 032 is unapplied in prod and existing pinned
runtime grants do not include V40 claim table; the current production
role is not certified for V40. The new permission audit is a future
safety gate, not license to apply grants now. Actual MEXC native
protection and execution, 4-slot runner, cross collateral and tiers,
Render protected soak and independent release review remain blocked.

### 2026-11-03 — Disable non-claim-bound protected DB commit entry point

**Public legacy bypass closed in development branch:** 
`PostgresProtectedV40Commit.persist` now unconditionally raises
`V40_UNCLAIMED_PROTECTED_COMMIT_API_FORBIDDEN`. The only development
claim-bound public commit method is `persist_claimed`, which rechecks
the exact durable one-shot dispatch claim inside the same SQL transaction
as ACK/fills/position checkpoint/revision/protected slot writes.

Historical simulation and disposable CI tests use the explicitly named
`persist_offline_unclaimed` fixture method; it fails closed if
`T100_EXECUTION_MODE` is not paper, the live-order API flag is enabled,
or `DATABASE_URL` / `MEXC_API_KEY` / `MEXC_API_SECRET` are present.
A mandatory offline regression test scans application modules to
prevent new `app/t100_live_*.py` callsites from using that fixture method,
and tests the public forbidden API leaves journal/DB unchanged.

The first CI run found a historical shared reservation test still using
the now-blocked public method. That test was migrated to the explicit
offline-only fixture path, **without** weakening the refusal guard.

**Verified code SHA**:
`5a78846d647886b176722d84657749a84c158394`.
All FIVE GitHub Actions checks passed:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647951064),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647950882),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647951093),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647951132),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41647950873).

**Limitations**: Python's internal `_persist` is not a cryptographic
capability and remains callable by intentionally malicious code.
Source review, pinned build provenance, runtime DB privileges and CI
enforcement still matter. Passing tests do NOT approve a real exchange
POST, grant collateral certification, deploy migrations 031/032, prove
native-stop behavior or allow live trading. BOXWAVE and existing manual
positions untouched.

### 2026-11-03 — Non-authorizing four-slot restart supervisor and opt-in CLI

**New modules:** `app/t100_live_four_slot_restart_triage.py` and
`app/t100_live_four_slot_restart_supervisor.py`. A single restricted
PostgreSQL read-only REPEATABLE READ join inventories active V40 slot
reservations, corresponding intent states and immutable dispatch-claim
provenance (zero to four slots, no claim ownership drift). It refuses
missing or malformed DB evidence, wrong role/database, duplicate symbols
and excess active slots. A pending RESERVED/SUBMITTING/UNKNOWN/ACK/HALTED
attempt is **RECONCILE ONLY** with no MEXC GET by this supervisor and
no order retry—even when a durable claim is absent.

Only when all attempts are settled may the supervisor invoke the
existing signed PRIVATE GET-only complete-inventory/native-stop/owned
checkpoint audit. A missing native stop, unsupported inventory shape,
external manual position or inconsistent durable ownership remains a
non-authorizing blocker. Returned public summaries exclude order IDs,
origin strings, symbols, margin, balances and secrets. Every result
continues to assert no order, DB mutation, permit or authorization.

Added explicit opt-in `--restart-preflight` to existing local
`app.t100_live_four_slot_readonly_cli`. It reuses paper-mode, live-order
disabled and kill-engaged guards and restricted DB credentials. The
default CLI behavior is unchanged. Nothing starts automatically on
Render; migrations 031/032 and runtime role/grants are still NOT
production-authorized or applied.

Offline tests cover pending claimed/unclaimed/UNKNOWN, manual positions,
missing native stops, wrong role, invalid controls and sanitized output.
Real disposable PostgreSQL 16 tests cover a claimed and unclaimed
SUBMITTING attempt blocking all normal GETs, plus a fully
claim-bound-protected ACK transition followed by a four-position GET-only
restart audit. The first settled-path integration fixture omitted required
position completeness fields; the test was corrected to the already
verified synthetic MEXC shapes **without weakening any guard**.

**Exact tested source SHA:** `429ec1a8fccea8edf8ae716feb218d9555140b4f`,
5/5 GitHub checks PASSED:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258081),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258149),
[scanner/trader split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258157),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258155),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41658258074).

Detailed requirements, safe output and external observed status:
`docs/T100_V40_RESTART_READONLY_CANDIDATE.md`.
Latest old Render shadow 2026-11-03 14:54:38 UTC was still
`OBSERVING_KILL_LATCHED` / `NATIVE_PROTECTION_LOST`, with position
observed and no TP/SL row. No new deployment, real exchange order,
stop or manual-position modification, production DB/role grants,
credential changes or BOXWAVE modifications.

**This advances only G4/G6 read-only recovery preparation.** It is
NOT a complete four-slot live order executor, real MEXC native-stop
certificate, current cross-collateral/risk-tier certificate, validated
protected Render soak, independently reviewed release or operator-approved
tiny live pilot. All these gates remain BLOCKED.

### 2026-11-03 — Two-phase restart SQL preflight and released-claim ambiguity

Opt-in V40 read-only CLI now performs an **initial** restricted
PostgreSQL triage BEFORE importing/constructing the general MEXC
client. An ambiguous or invalid durable order state returns a redacted
blocker with NO exchange client created and NO exchange GET. On clean
evidence the client may be constructed, but the supervisor performs a
SECOND independent restricted SQL triage immediately before GET to
detect a newly staged attempt. Both checks require paper mode,
live-order API disabled and kill switch engaged, and neither writes DB
or modifies the venue.

The restart supervisor now verifies its triage object contract:
zero to four slots, coherent counts/statuses, unique immutable
reconciliation-only actions, absent POST/DB mutations and false
manual-adoption and authorization flags. Fabricated/contradictory
triage records block before GET.

The restricted read-only RR snapshot now additionally inspects
RELEASED reservations: if an order intent remains unresolved or a
one-shot claim is inconsistent (origin, holder, fencing or lifecycle),
the restart is blocked instead of quietly treating the released slot
as nonexistent. This is a safety refusal, NOT an authorization or
proof of exchange settlement. Real disposable PostgreSQL tests verify
both a released unfinished order and an intentionally wrong-origin
claim are detected.

**Verified code SHA**:
`8054b576c874f93c625f55d188d273634db24ba1`.
5/5 GitHub checks PASSED:
[safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41661435766),
[PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41661435869),
[scanner/trader split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41661435801),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41661435912),
[dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41661435821).

No production deployment/DB migration/grant, live MEXC POST/stop
action, credential change, manual-position adoption or BOXWAVE changes.
Full four-slot live executor, current MEXC native-stop behavior,
collateral and margin-tier certification, Render current-code soak,
independent release review and separately approved limited pilot
remain BLOCKED.

### 2026-11-03 — Separate pending-order exact-origin GET-only witness

Source milestone `8883a52aa613fe80fd2e5a15b95db7ff4bcb7b31`
introduces `app/t100_live_four_slot_pending_origin_get.py` and a
separate explicitly opt-in
`python -m app.t100_live_four_slot_readonly_cli --pending-origin-audit`.
The normal read-only restart supervisor **still blocks** any pending
order attempt before constructing a MEXC client. This separate manual
diagnostic is only for determining whether an unresolved immutable
order origin is observable, never for deciding that a POST may be
retried or that a position is protected.

The operator must enable paper mode, disable the live-order API and
engage the kill switch. The CLI performs a restricted, read-only
REPEATABLE READ V40 triage BEFORE constructing the GET client, a
second SQL triage before GET, two independent signed PRIVATE GET
`/order/external/{symbol}/{externalOid}` rounds (no new endpoint),
and a third restricted SQL triage AFTER the venue GET window. If any
SQL state changes or the order identity/side/payload changes, the
result is discarded and no retry is offered. At most four active V40
slots are inspected; RESERVED slots trigger no order GET.

Two matching order reads are `ORDERS_SEEN_UNPROTECTED_NONAUTHORIZING`
rather than an ACK/fill/stop certificate. Two absent order reads
remain `ALL_ABSENT_STILL_UNKNOWN_NO_RETRY`; a missing exchange order
is NEVER evidence that a previous POST failed. Mixed, malformed,
wrong-side, changing, read-failed and inaccessible/mismatched states
all fail closed. Only aggregate counts/statuses are emitted: no
symbols, externalOid, position/order IDs, prices, collateral,
credentials or raw MEXC payloads.

Offline tests verify order present/absent/mixed, bookend drift,
wrong-side/identity, failed exchange GET, invalid SQL triage,
reserved-only, no retry, no mutation and no leaked identifiers.
Real disposable PostgreSQL tests prove that pending order origins can
be observed read-only before and after a durable dispatch claim; no
DB ACK/checkpoint, trade, stop or ledger mutation occurs. CLI tests
prove missing/invalid preclient SQL prevents client construction,
drift after client creation blocks BEFORE GET, and journal changes
DURING GET invalidate the observation. This module is not an
automatic recovery executor and is not wired to Render.

All FIVE code CI checks PASSED at tested SHA:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41680751212),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41680751110),
[scanner/trader split](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41680751364),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41680751306),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41680751307).

Detailed runbook: `docs/T100_V40_PENDING_ORIGIN_GET_ONLY.md`.
Migrations 031/032 have NOT been deployed to production and runtime
permissions remain unapproved. The actual MEXC native stop execution
lifecycle, account collateral/margin-tier risk, complete four-slot
order executor, independent review and protected Render soak remain
BLOCKED. No MEXC POST, real order, stop modification, production DB
write, Render redeploy, credential change, or BOXWAVE modification.

### 2026-11-03 — Settled four-slot GET restart also needs final SQL bookend

The existing `run_four_slot_restart_readonly_cycle` now checks the
restricted PostgreSQL triage BOTH before and after the owned-position,
native-stop and account GET observations. It rejects mismatched,
unreadable or changed SQL snapshots as
`RESTART_DB_CHANGED_DURING_GET_NO_RETRY` (redacted, nonauthorizing).
The pre-client database gate and second pre-GET gate remain; this
third read also catches journal drift occurring *during* venue GETs.

Offline regression tests inject a new pending slot after the pre-GET
SQL snapshot and a DB outage on post-GET read. The real disposable
PostgreSQL 16 test commits an exact claim-bound PROTECTED fourth slot,
changes the durable slot and intent to UNKNOWN during a synthetic
GET window, and confirms that the supervisor does NOT report a clean
restart. All outputs remain `entry_allowed=false`,
`live_authorization=false`, no order retry or DB/venue mutation.

**Code SHA tested:** `bda38851866f3e906df1d0bc3f550f0fa87d8154`.
All 5 workflows PASS:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41681561124),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41681560496),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41681560546),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41681560685),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41681561039).

No Render deployment or live exchange activity occurred. This is
read-only observability hardening, NOT native stop/exchange atomicity
certification; real-money gates remain BLOCKED.

### 2026-11-03 — Current MEXC API order direction and native market SL contract audit

**Verified documentation change:** MEXC official Futures API update log
dated 2026-10-23 added required `sideType` (`buy`/`sell`) and
`positionType` direction to `POST /api/v1/private/order/create`.
Existing T100 SHORT_SELL adapter omitted both fields. Its currently
**unexecuted** request payload now explicitly sets `side=3`,
`sideType=sell`, `positionType=short`, retaining hedge
`positionMode=1`, CROSS `openType=2`, immutable externalOid,
reference stopLossPrice and the existing 1x tiny-pilot restriction.
This is API shape correction in development, NOT venue POST certification.

**Unverified stop amendment compatibility:** The current official
`/private/stoporder/change_price` documentation covers limit-order
TP/SL by `orderId`; the old `change_plan_price` reference
uses `stopPlanOrderId`. The mapping of a *position* TP/SL current
`id` to the latter legacy endpoint has not been independently
certified. Therefore the concrete adapter's legacy
`submit_native_short_stop_tightening` transport now requires
**independent** `LIVE_NATIVE_STOP_AMENDMENT_STATUS=CERTIFIED`,
pinned to `UNVERIFIED_CURRENT_DOCS` in ordinary production source.
Even a hypothetically issued tiny pilot permit alone cannot send
this amendment through the adapter. The repository-wide
`LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED` remains unchanged.

**New independent GET-only native stop-shape audit:**
`assess_native_short_market_stop_row` checks exact short position
ownership and stop identity, active/nonfinal status, MARKET SL
`stopLossType=0` (not limit `1`), `stopLossReverse=2`,
`lossTrend=1`, a positive bounded short stop price, and effective
whole-position volume (SAME and SEPARATE modes). Wrong/missing
type, wrong side, reverse, trigger mismatch, limit SL, undercoverage
and stop ID drift fail closed. **Even success emits
`stop_execution_certified=false` and `live_authorization=false`**:
one static GET row cannot prove trigger execution, price, slippage,
exchange atomicity, account-wide collateral or liquidation timing.

Authoritative current source links and differences:
`docs/T100_MEXC_2026_API_CONTRACT_DELTA.md`.
CI-tested source SHA `23cb3bb62f54ac945c4372ac6c209220ab177633`.
All five workflows PASS:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41683705427),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41683705298),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41683705367),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41683705245),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41683705385).

This is **not** certification of real SL placement/amend/cancel.
MEXC order access and native-stop endpoint/response contract,
account-wide CROSS collateral and risk schedule, protected Render
soak, full four-slot POST execution, independent reviewer and
explicit tiny pilot release are all BLOCKED. Existing manual position
is untouched. No MEXC POST/stop edit, production DB mutation,
Render redeploy, credential or BOXWAVE change occurred.

The engineering progress estimate is **70%** (subjective);
live trading remains **NOT AUTHORIZED**, not 70% safe.

### 2026-11-03 — Strict current native-market SL volume shape (offline only)

At current native-market stop-shape audit level, reject missing/unknown
`volType` or `profitLossVolType` before accepting a full-position SL
quantity. The older generic `effective_protective_quantity` parser is
still used by legacy snapshot/stop recovery tests that omit these MEXC
fields; changing it globally prematurely caused many offline fixture
failures, so that broad change was reversed. The strict new native
witness now independently requires documented volume modes. Follow-up:
migrate and independently re-certify the **legacy shared parser and all
dependent fixtures** before relying on its permissive mode behavior as
an actual live protection certificate.

Also reject Unicode-digit and malformed stop IDs without parser
exceptions. New native-stop audit regressions cover unknown volume
mode, absent volume mode, malformed identity and nonauthorizing outputs.

**CI-tested code SHA:** `a807bc5a2d06200d60093cddb73985a4b023254e`.
All five checks PASS:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41685582860),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41685582869),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41685582884),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41685582983),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41685582964).

This safety fix is observational only and adds NO live authority. Global
`LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED` and independent native stop
amendment contract remain BLOCKED. Production Render, exchange orders,
stop mutations, account positions, production DB and credentials are
unchanged. BOXWAVE remains isolated. Engineering completion remains
approximately **70% subjective**, with zero live-order authorization.

### 2026-11-03 — Four-slot staged outcome and offline checkpoint now require MARKET native SL

The prior four-slot staged-attempt observer and protected-fill preparation
could accept an apparently active volume-complete native stop without
distinguishing `stopLossType=0` MARKET SL from `stopLossType=1` LIMIT SL.
The independent strict current MEXC GET-row witness is now enforced twice:

1. `assess_staged_protected_attempts` rejects non-market/unknown native
   stop type, reversal, wrong trigger and invalid or unknown volume-mode
   semantics for every newly observed position. It does not mark the
   staged attempt fully protected or issue any live authority on failure.
2. `prepare_protected_entries` independently rejects the same unsupported
   native SL evidence before preparing any offline ACK/fill/checkpoint
   bundle, even if an earlier generic witness is positive.

Both stages remain GET-only / in-memory and **NONAUTHORIZING**. They
cannot prove that a future native SL triggers, fills, survives volatility,
precedes liquidation, or atomically protects an entry. Historical/manual
positions and legacy snapshot parser remain separate, noncertified
readiness blockers.

New regressions cover LIMIT SL, missing stop type, reversal, wrong
trigger, unknown volume semantics, plus staged bookended GET preparation.
Only in-memory venue fixtures were used; there was no venue mutation.

**CI-tested code SHA:** `4c75d243fdb8ee20665165f18793a501fd18c6c7`.
All FIVE workflows PASS:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692036603),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692037014),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692036721),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692036898),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692036647).

Additional current official API caveat: MEXC
`POST /api/v1/private/stoporder/place` notes that a pre-existing
non-final TP/SL at the same price can return its previous ID and update
quantity **asynchronously**. Therefore a successful POST/returned ID or
single GET cannot certify an immediately protected owned position.
The independent private-stop placement/identity, amendment/cancel
semantics, stop lifecycle and exchange gap execution are still BLOCKED.
Reference:
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position

No real order, stop modification, production DB mutation, credential
change, Render deployment or BOXWAVE modification. The engineering
estimate remains approximately 70%; live trading remains blocked.

### 2026-11-03 — Exact SQL dispatch-claim bookend around protected GET

The claim-bound staged protected-GET observer previously inspected
the restricted, immutable V40 dispatch claim only **before** its two
venue GET bookends. A valid slot/intent journal state could change
during the exchange read window, yet the aggregate claim result
(count=1, status=PRESENT) would remain identical. That observation
must not result in even an offline prepared checkpoint.

`PostgresFourSlotDispatchClaimReader.inspect` now returns only a
SHA-256 fingerprint of exact validated claim/slot/intent SELECT fields
in addition to its nonauthorizing status and counts. No raw IDs,
symbols, holder IDs, exchange data or credentials are logged.
`observe_claimed_staged_protection_get_only` requires a valid 64-char
fingerprint before GET, and repeats the restricted SQL SELECT **after**
both venue GET rounds and protected bundle preparation. Missing,
changed or unreadable post-GET SQL evidence discards the entire
prepared bundle and reports a redacted NO_RETRY blocker.

Offline tests inject same-summary SQL digest drift, post-GET reader
outage and missing first fingerprint. Real disposable PostgreSQL
integration verifies that SUBMITTING -> UNKNOWN/ACK/PROTECTED journal
changes alter the digest even while counts/status remain compatible.
No database writes occur in the read-only observer. These SQL
bookends are not atomic with MEXC; they only detect observed drift,
not ABA or unobserved changes.

**CI-tested code SHA:** `4155c93c6f56a1ccadda7e3b48859035301b9b23`.
All five checks PASS:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692809243),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692809640),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692809652),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692809480),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41692809316).

No exchange POST, native stop amendment, production DB update, Render
deployment, credential changes or BOXWAVE changes. Live execution,
native stop lifecycle, account collateral/liquidation evidence and
independent production release review remain **BLOCKED**.

### 2026-11-03 — Position-origin stop guards and legacy trailing recovery tightened

Current MEXC official GET TP/SL documentation distinguishes a
**position-origin** stop with `orderId=0` from an order-attached
TP/SL. It also documents `triggerSide=0` for untriggered
and `errorCode=0` for normal rows. The strict
`assess_native_short_market_stop_row` now rejects missing,
nonzero, malformed and unexpected values for these fields;
it continues to require active, full-volume short MARKET SL
(`stopLossType=0`), no reversal, latest-price trigger, exact
position identity and nonlooser price. Its result remains
observational, never exchange execution certification.

The four-slot staged-outcome and offline checkpoint path inherit
this stricter gate. Additionally, the legacy no-POST
`StopAmendmentLifecycleCoordinator` and
`StopUpdatePersistencePipeline` independently require the strict
native row before journal VERIFIED and checkpoint persistence. Tests
cover rejected limit SL, order-attached SL, already-triggered and
error rows, reversal, wrong trigger, unknown quantity mode,
and crash/restart UNKNOWN recovery. Shared manual-account fixtures
now carry explicit documented MEXC fields; other manual positions
remain untouched.

**Code SHA tested:** `eeb092321cb354907138ef7a5884568a95f6f43d`.
All five checks PASS:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696241266),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696241153),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696241233),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696241194),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696241256).

**Current docs correction:** MEXC *does* now document
`POST /api/v1/private/stoporder/change_plan_price` for
`stopPlanOrderId`. The independent gate remains pinned blocked
because actual live position-TP/SL `id` mapping, amendment
POST/GET/terminal-history behavior, stop activation timing and
execution/gap reliability have not been independently witnessed.
The direct legacy checkpoint writer and remaining shared/generic
volume parser paths still need audited upgrade before live certification.
A successful entry order with embedded `stopLossPrice` is NOT
presumed to create an independently verified `orderId=0`
position stop.

No order, native stop edit, production DB or Render mutation,
key change or BOXWAVE change. Approximately 70% subjective
engineering progress remains; **LIVE_AUTHORIZATION = FALSE**.

### 2026-11-03 — Direct durable checkpoint writer independently requires native MARKET stop

A remaining bypass was identified after tightening the V40
staged-outcome and legacy amendment reconciliation paths:
`CheckpointPersistenceCoordinator.persist_initial_protected` and
`persist_verified_stop` directly accepted their legacy checkpoint
journal's volume/state shape without independently verifying MEXC
position-origin and MARKET SL semantics. A caller could otherwise
persist a checkpoint from a normal order-attached or LIMIT SL row.

The **final checkpoint writer boundary** now runs the strict GET-only
`assess_native_short_market_stop_row` using the normalized checkpoint
position ID, quantity, symbol and stop price **before either writer
mutation**. Limit SL, nonzero/missing orderId, wrong trigger, abnormal
error/status, wrong reversal, missing volume mode and undercovered stop
all fail closed with no checkpoint write. Regression tests exercise
both initial and amended checkpoint persistence; the legacy simulated
entry-recovery fixture was normalized to documented current venue
stop fields.

**CI-tested code SHA:** `6675e554527852faaa599dce9e06e228923a868e`.
All five workflows PASS:
[offline safety](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696775504),
[isolated PostgreSQL](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696775522),
[scanner/trader separation](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696775533),
[promotion](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696775491),
[daily dashboard](https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41696775469).

This does NOT prove exchange stop activation, trigger fill, no exposure
gap, cross-margin account safety, or venue/SQL atomicity. Real order
placement, current position-stop amendment ID mapping, full
four-slot mutation and recovery executor, independent signed private
GET position/stop lifecycle, production privileges/soak and reviewer
signoff remain BLOCKED.

All changes are branch-only. No MEXC POST/stop change, no production
DB write, no Render deployment, no credential change, no BOXWAVE change.
Engineering completion estimate remains ~70% (subjective);
`LIVE_ADAPTER_STATUS=NOT_IMPLEMENTED`,
`LIVE_NATIVE_STOP_AMENDMENT_STATUS=UNVERIFIED_CURRENT_DOCS`.

### 2026-11-03 — ACTUAL signed MEXC GET probe on Render (not a POST test)

The existing `mexc-t100-live-shadow` Render worker (`srv-dbf5798m7kps73fqcuog`)
was redeployed on its own read-only feature branch with a strictly
redacted first-cycle diagnostic. The diagnostic consumes the already
configured signed `GET /api/v1/private/position/open_positions`,
`GET /api/v1/private/stoporder/open_orders` and
`GET /api/v1/private/stoporder/list/orders` page-1 results in the
process; it emits fixed aggregate counts only, never credentials,
position IDs, symbols, order IDs, balances, prices or volumes.

- Read-only feature commit:
  `a9692afbd0cedd314054427e37ce1aac70f75577`.
- Render deploy: `dep-dbl2tk61egvs739d9qqg`, confirmed LIVE at
  `2026-11-03T18:59:03.911Z`.
- Signed GET diagnostic timestamp: `2026-11-03T18:59:10.626Z`.
- Current account: **1 open position, 0 active TP/SL**.
- Finished TP/SL history page 1: **4** observed rows, **2**
  with `orderId=0` (position-origin), **2** with a positive
  attached order ID, **4** with positive native TP/SL `id`.
- **2** historical TP/SL rows link to the currently observed
  position ID. **3** show `stopLossType=0` (market SL), **0**
  show limit SL, **1** has unknown/missing stop-loss type.
- **4** finished rows have nonzero `errorCode`, **0** normal;
  all 4 have `triggerSide=0` and `isFinished=1`.
  These are historical, apparently canceled/terminal rows;
  `errorCode` needs context, do not infer current stop errors.
- Runtime immediately relatched `NATIVE_PROTECTION_LOST`, with
  `entry_allowed=false`, `live_authorization=false`,
  and stable REST/WS/REST continuity. No exchange POST or
  account mutation happened.

**Verified with real exchange evidence:** read permission and current
position/stop counts; current GET historical TP/SL row shape; position
vs attached origin distinction; native stop IDs exist in historical rows.

**Still NOT verified:** whether a position-origin `GET id` is accepted
as `stopPlanOrderId` by the real
`POST /api/v1/private/stoporder/change_plan_price`; amendment
roundtrip and uncertain POST recovery; stop placement/activation, SL
trigger/filled execution, slippage/gap/liquidation protection.
There is currently **no active stop to amend**, and the credential is
used in read-only/paper mode (previous operator evidence says trading
permission disabled). MEXC official documentation states its API
has no sandbox environment. An actual POST test would change real
financial exposure and requires an approved, bounded test position,
a properly permissioned credential and explicit operational safeguards.
No attempt to bypass the read-only gate was made.

The Render PostgreSQL read-only connector query failed because its
external database access was blocked by SSL/IP allowlist constraints;
**no SQL result** was obtained and no DB mutation attempted.

Sources: current MEXC TP/SL GET and amendment documents:
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/get-take-profitstop-loss-order-list
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/modify-tpsl-prices-on-a-tpsl-planned-order
and official sandbox statement:
https://www.mexc.com/mexc-api .

**Progress remains NOT LIVE READY.** Do not equate GET evidence
with live order/cancel/stop execution certification. BOXWAVE
and main trading workers were not changed.


### 2026-11-03 — T100 isolated $50 subaccount live pilot POST rejected: code 2030

Operator-reported local signed API evidence:
- Prior local read-only GET gates: USDT equity/available 50/50;
  positions=0, current TP/SL=0, open orders=0; hedge mode and
  minimum BTC_USDT isolated 1x plumbing candidate passed.
- One explicitly operator-authorized real `order/create` POST
  returned `success=false`, `code=2030`.
- Official current MEXC Futures error-code reference identifies
  **2030 = external order ID too long, maximum 32 characters**.
  Inspection of the actual local `t100_first_live_pilot_v2.py`
  confirmed the open ID prefix plus 20 hex characters was
  **35 characters**; the close ID generator would have been
  **36 characters**. This is a local order-payload bug,
  NOT evidence of revoked API trading permission.
- Script's post-rejection signed GET inventory showed 0 positions,
  0 active TP/SL and 0 other open orders. No close POST was sent.
  Native stop placement and amendment remain **NOT VERIFIED**.
- Corrected `t100_first_live_pilot_v3.py` (local operator artifact,
  not deployed) uses **26-character** external IDs for both
  opening and closing; validates ID ASCII/length before ANY
  `order/create` HTTP request; adds a strictly read-only,
  human-confirmed `--reconcile-2030` flow to archive the old
  crash-safety journal only after two fresh empty inventory
  bookends and a final post-confirmation GET. **10 offline
  isolated tests pass.** No new real order has been sent.
- Neither T100 production, existing main-account shadow creds,
  Render trading worker, nor BOXWAVE were modified in this
  corrective local-workflow step.

Do NOT automatically retry a rejected trade or clear the one-shot
marker without authenticated fresh inventory and operator review.
No API permission, stop placement, stop amendment, or position
closure execution has been proved by this failed request.

Official reference:
https://www.mexc.io/api-docs/futures/error-code


### 2026-11-03 — Isolated $50 T100 BTC one-contract live pilot: accepted entry, closed, stop type UNVERIFIED

Operator-provided terminal output for v3 (local one-shot pilot):
- Operator entered the exact real-funds authorization phrase.
- The actual MEXC `POST /api/v1/private/order/create` **acknowledged** opening
  one BTC_USDT isolated 1x short (the previous 2030 externalOid problem fixed).
- Subsequent signed `GET /api/v1/private/stoporder/open_orders` returned
  a stop row for which the pilot's strict
  `stop_report` test `stopLossType == 0` **did not pass**:
  `PILOT CHECK FAILED: TP/SL field stopLossType is not in required state`.
  Because the original v3 diagnostic printed no private row values,
  whether this field was **missing, null, 1 (limit) or another value
  is NOT KNOWN**. Do not infer it was a harmless absent default,
  and do not downgrade `stopLossType` validation to accept null.
- Fail-closed cleanup sent **one close-short market POST**, accepted
  by MEXC; signed GET subsequently showed **0 positions**.
- Final signed GET inventory: 0 positions, 0 current TP/SL, 0
  other open orders. This is **observed flat** only, not
  complete order/deal history reconciliation.
- `STOP ACTIVATION VERIFICATION: NOT VERIFIED`.
  `STOP AMENDMENT VERIFICATION: NOT VERIFIED`.
  The stop modification POST was NOT reached. No safe T100 live
  authorization is implied by successful entry/close.
- The v3 one-shot local journal remains in
  `FLAT_BUT_STOP_LIFECYCLE_NOT_VERIFIED`; **DO NOT rearm or retry
  `--live`** until read-only history forensics have been reviewed.

A new standalone **GET-only local forensic** artifact
`t100_pilot_stop_forensics_readonly.py` was prepared (not deployed):
it looks up the exact accepted entry by the local one-shot
`externalOid`, reconciles `positionId` with paginated
`GET /api/v1/private/stoporder/list/orders` (`is_finished=1`),
prints only redacted field-state categories for matching TP/SL rows,
and rechecks flat account inventory before/after. It has no POST
capability; five isolated local unit tests passed. In particular,
`stopLossType` is reported as `0`, `1`, `MISSING`, `NULL`,
or invalid **without assuming missing means market**.

MEXC current GET response documentation lists
`stopLossType=0` market / `=1` limit, but its published
example omits this field even though it is in the schema.
This possible response-shape gap needs actual signed history evidence
before code-level safety classification changes. Historical terminal
rows alone cannot prove preceding stop activation, execution or
amendment identity.

Official docs:
- https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/get-current-take-profitstop-loss-order-list
- https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/get-order-by-external-id
- https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/get-take-profitstop-loss-order-list

The production T100 runtime, other accounts and BOXWAVE were not
touched. **No additional live order is authorized**.


### 2026-11-03 — REAL MEXC signed GET post-pilot forensic classification (T100-only subaccount)

Operator ran `t100_pilot_stop_forensics_readonly.py` locally on the
separate $50 T100/225 BTC pilot subaccount. Two authenticated
inventory bookends observed the account **flat**. This is venue
evidence (operator-supplied sanitized status), not synthetic replay.

- Pilot journal: `FLAT_BUT_STOP_LIFECYCLE_NOT_VERIFIED`; historical
  entry `state=3`, positive deal volume, positive embedded SL price,
  `lossTrend=1`.
- Finished TP/SL history: one page/one row, linked to the **actual**
  pilot position. History completeness **NOT certified**.
- Historical row:
  `orderId>0` => **ORDER_ATTACHED**, *not* position-origin;
  `volType=1` partial-TP/SL mode;
  `profitLossVolType=SEPARATE`; positive `vol` and
  `stopLossVol`; positive `stopLossPrice`;
  `lossTrend=1`; `stopLossReverse=2`;
  **`stopLossType=MISSING`**, **`stopLossOrderPrice=MISSING`**.
- Terminal row: **`state=2` CANCELED**,
  **`isFinished=1`**, **`triggerSide=0` UNTRIGGERED**,
  and `errorCode` nonzero (actual numeric value redacted).
  This row was never observed to trigger or execute an SL.
- The earlier live attempt failed before `stop_report` would have
  checked position origin because `stopLossType` was missing.
  This missing field must **not** be silently inferred as
  `market`, even though official MEXC GET example also omits
  `stopLossType`. The discovered *order-attached* origin would
  independently fail position-native certification.
- This **disproves** treating `order/create` with embedded
  `stopLossPrice` as sufficient evidence of a position-origin
  `orderId=0` market SL or as an automatically safe
  `change_plan_price.stopPlanOrderId` target.
- MEXC documents `POST /api/v1/private/stoporder/place` as
  the separate **place TP/SL by position** endpoint, requiring
  `positionId` and other parameters, including
  `stopLossType=0` for market stop. A subsequent position-native
  POST/GET witness, amendment GET, cancellation/close, and failure
  reconciliation is still necessary. **Do not retry v3** unchanged.
- No further live trades, stop amendments, production credential
  changes or BOXWAVE actions were undertaken.

References:
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/get-current-take-profitstop-loss-order-list
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/modify-tpsl-prices-on-a-tpsl-planned-order

**LIVE TRADING UNAUTHORIZED** until separate, supervised,
financially limited end-to-end verification; maintain strict
failure-on-missing-field policy and separate main-account/BOXWAVE
identities.


## 2026-11-04 — Separate position-origin stop pilot review candidate

Recovered the requested remote branch at fdd59146, including API delta and
operator-reported BTC pilot/forensics. Older local branches were retained;
untracked virtualenv/package metadata preserved. No credentials accessed.

Implemented `app/t100_live_position_stop_pilot.py`: a separate non-production,
GET-only reconciliation and durable review-request lifecycle. It proposes the
position placement endpoint, strict native identity/type/quantity verification,
journaled tighter amendment, exact close-fill/flatness reconciliation and
owned-stop cancellation/terminal history. Unknown attempts survive process
restart, cannot be resubmitted, and block ambiguous progress. Per-request review
scope/expiry and SQLite transactional serialization are tested. Dispatch always
raises; neither production authorization gate was changed. This is an offline
review candidate, NOT an operational or venue-certified execution adapter.

Official documents were rechecked on November 4. Placement response shape,
market-order limit-price fields, same-price asynchronous updates, amendment
mapping and actual lifecycle behavior remain unverified. The embedded entry
stop's ORDER_ATTACHED / MISSING market type / CANCELED evidence is retained as
negative protection evidence, never silently promoted to position protection.

See `T100_POSITION_STOP_PILOT.md` for architecture, limitations and the exact
next separately approved supervised experiment. Tests exercise full simulated
placement/amend/close/cancel, automatic cancellation, crashes, concurrent staging,
GET redaction, wrong account, missing native type, partial closes, stale snapshots
and tighter-only amendments. G5/G9 remain BLOCKED for actual venue verification;
G4/G6 gain only offline pilot mechanics, not production readiness.


Validation review found an inherited test defect: `ProtectedEntryTests.run`
was an async helper overriding unittest's runner, producing unawaited-coroutine
warnings instead of executing those tests. Renamed the helper to `reconcile`,
then corrected its stale happy-path stop fixture to include the strict native
market fields required by the current checkpoint code. All ten reconciliation
tests now actually execute; production validation was not relaxed.

Final local validation for this candidate: **2,190 passed, 912 subtests passed,
no warnings**, excluding only the two explicitly CI-only PostgreSQL modules.
See `evidence/issue-14/position-stop-pilot-tests-20261104.txt`. New pilot static
checks, project verification and diff checks pass. GitHub CI is required on the
published commit; no local result is substituted for its PostgreSQL checks.

Final offline checkpoint (November 4): **2,189 passed, 912 subtests passed**.
Command: `.t100-venv/bin/python -m pytest -q
--ignore=tests/test_t100_live_shared_postgres_integration.py
--ignore=tests/test_t100_live_v40_postgres_integration.py`.
Evidence: `evidence/issue-14/position-stop-pilot-offline-tests.txt`.
The exact focused safety workflow unittest command passed **1,790 tests**.
Ruff for the new pilot/tests, project verification and diff whitespace checks
passed. A blanket pytest invocation also attempted the two CI-only PostgreSQL
modules: all 40 database cases failed at their explicit isolated-CI guard; these
are NOT counted as passing database evidence. No guard was bypassed and no
production database was contacted. GitHub CI for this local commit is pending
publication; workflow now triggers on the requested development branch.

Follow-up review added durable redacted ACK/rejection/ambiguous outcome events,
request hashes/timestamps, emergency-close planning before the first stop,
preservation of superseded unknown stop attempts, stale-revision rejection
inside the reconciliation transaction, and rejection of ephemeral journals.
Regression tests cover rejected and malformed POST responses, old-price amendment
observations, no duplicate close, restart recovery and concurrent staging.

**Protection-gap decision: BLOCKED.** The full-quantity placement prose is
ambiguous (strictly less than closable quantity), market limit-price fields are
unresolved, and no bounded loss/time guarantee exists between entry fill and
verified position stop. Emergency close is only a proposed recovery action and
may fail. No automated live entry is acceptable on present evidence. Independent
review, venue contract clarification, pre-entry journaling/watchdog/deadline,
approved loss budget, separately reviewed executor and explicit operator approval
are required before a supervised experiment. See the design's recovery limits;
stop execution and partial-fill recovery remain unverified. G5/G9 stay BLOCKED.
No real exchange POST, Keychain access, production change or merge occurred.

## Dedicated Keychain GET preflight — 2026-11-04 follow-up

Fetched development branch; local checkpoint was two commits ahead of fetched
remote. Inspected Downloads v3 pilot and GET-only forensics code without running
the old live pilot or changing its journal. Rechecked official placement,
current-stop-list and amendment documentation. Full-quantity equality, required
market limit-price fields and safe attached/native stop coexistence remain
unresolved. A separate entry followed by native stop placement leaves exposure;
no safe live sequence has been established. No live approval requested.

Added `scripts/t100_position_pilot_preflight.py`, a dedicated GET-only runner
with fixed Keychain service/accounts, captured subprocess output, fixed HTTPS
host, redirects/proxy environment disabled and no mutation method. It requires
two empty account inventories with strict counts; errors redact bodies/secrets.
Sandbox Keychain access failed. Narrowly escalated local execution retrieved
credentials internally, but the first signed inventory GET returned HTTP 403.
**Current account inventory is UNVERIFIED.** No conclusion about account flatness
or the cause of 403 is inferred. Exchange testing halted; no POST was sent.
Previous pilot journal remains unchanged. Next prerequisite: resolve authorized
signed GET access, then establish flat inventory and reviewed full-volume stop
contract/protection-gap controls before presenting one concrete approval request.

Offline validation: 2,191 passed, 912 subtests passed, excluding the two guarded
CI-only PostgreSQL modules. Those integration tests were not run this follow-up;
GitHub CI for the local change is unverified. Production remains blocked.

## Corrected preflight domain and successful signed GET — 2026-11-04

The new preflight incorrectly used retired `contract.mexc.com`; the previously
working Downloads client uses `api.mexc.com`. MEXC's official domain-update
announcement retires the former on January 19, 2026:
https://www.mexc.com/announcements/article/futures-api-access-domain-update-17827791532974
Corrected only the dedicated preflight host. No production configuration changed.
The earlier 403s do not establish credential/IP/permission failure.

Actual signed GET observation at Unix timestamp 1793777069: two complete passes
of positions, current stops and account-wide outstanding counts returned empty
positions/stops and strict integer zero counts. Status:
EMPTY_INVENTORY_OBSERVED_TWICE. This is non-atomic observation, not future
flatness or live authorization. Credentials stayed internal; no POST was sent.
A mock transport regression verifies the current host, no redirects/environment
proxies and all six GET calls. Prior journals were not modified.

Read-only access blocker is resolved. Full-volume stop contract ambiguities and
entry-to-protection gap remain unresolved; G5/G9 stay blocked. The operator's
required first-POST approval is still outstanding and has not been bypassed.

Domain-fix validation: 2,192 offline tests and 912 subtests passed, excluding
only the two guarded PostgreSQL CI modules. Ruff and diff checks passed.
Current-change GitHub CI is unverified; no new database integration evidence.

## Continued offline review — quantity modes and hard notional ceiling

Reject changed volType/profitLossVolType on position-stop reconciliation, even
when the generic full-coverage audit would compute the same protective quantity.
An amendment changing to whole-position or SEPARATE mode remains AMEND_UNKNOWN;
regressions verify no state advancement. This preserves the exact requested
semantics rather than inferring successful amendment from quantity alone.

Public BTC contract/ticker GET showed 0.0001 BTC per contract and approximately
7.82548 USDT for one contract at observation. The former market-entry runner
checks quoted notional, not a guaranteed fill ceiling. IOC/FOK sell limits also
do not cap the maximum sell price. The strict 12.50 opening-notional ceiling
therefore needs a documented enforceable envelope or explicit operator revision;
no constraint was relaxed. See the pilot design for the detailed blocker.
No private mutation, journal reset, production edit or new live approval occurred.
Validation: 2,193 offline tests and 914 subtests passed; the two guarded
PostgreSQL CI modules remain excluded. Ruff and diff checks passed. Current
GitHub CI remains unverified. Live execution remains blocked.

## Cleanup ownership and SQLite fault review — 2026-11-04

Cancellation proposals now revalidate symbol, position-native orderId=0 and
strict active/untriggered/nonfinal state in addition to exact stop/position IDs.
Terminal cancellation evidence also rejects an order-attached origin. Drift
cannot advance the journal or produce a cleanup proposal for an unexpected row.

Actual local SQLite trigger fault injection covers failed staging and failed
GET-reconciliation writes. Staging failure rolls back to NEW/revision zero;
a failed observation write survives reopen as PLACE_UNKNOWN and blocks retry.
Once storage is restored, GET evidence can reconcile the existing attempt.
These are local transaction/reopen tests, not OS power-loss or real-venue proofs.

Full offline suite: 2,196 passed, 920 subtests passed, excluding the two guarded
CI-only PostgreSQL modules. New pilot suite: 20 tests, 21 subtests passed.
Ruff, project verification and diff checks passed. GitHub CI for this local
checkpoint is unverified. No exchange POST, journal reset, production change
or new credential access occurred. Contract, gap and hard-notional blockers
remain open; first-POST approval remains required.

## Published development checkpoint — verified GitHub CI 2026-11-04

Published `9f2c184da6db36d861d417ec8d637885db0e6ef0` through the connected
GitHub integration after terminal push lacked HTTPS authentication. Its tree
`62932097de6b37a738a9e6e527f1909f5d9d0bcd` exactly equals local `a835a9a9`.
Publication used a non-force branch-head lease; no main merge or deploy occurred.

All seven workflow runs completed successfully:
- Isolated PostgreSQL PR: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41793723502
- Isolated PostgreSQL push: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41793716547
- Offline safety/full suite PR: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41793723458
- Offline safety/full suite push: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41793716470
- Service split: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41793723435
- Promotion: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41793723475
- Dashboard: https://github.com/chrpapas/mexc-exhaustion-scanner/actions/runs/41793723423

The development branch now runs full offline pytest and disposable PostgreSQL
integration on pushes. This supersedes the prior unverified CI status for the
published tree, not native-stop venue certification or full-strategy approval.

## Journal rearm review — 2026-11-04

Found and fixed implicit reinitialization of an existing SQLite journal with
its singleton state row missing. Existing empty/damaged-state journals now
halt with NO_REARM instead of inserting NEW. Symlink journal paths are rejected.
Three regressions cover deleted state after staging, an existing empty file and
a symlink. Whole-file deletion/replacement remains outside the local trusted
journal threat model and is not made safe by these checks.

Validation: 2,199 offline tests and 920 subtests passed; two guarded PostgreSQL
modules excluded. Focused pilot: 23 tests and 21 subtests passed. Ruff and diff
checks passed. No exchange mutation or production change occurred.

## Placement acknowledgement identity and disk recovery — 2026-11-04

The pilot now persists a valid scalar placement-response stop ID and binds
subsequent GET reconciliation to it across restart. A caller cannot substitute
a different matching-looking stop. Conflicting acknowledgement IDs latch an
investigation condition that blocks protection certification; emergency-close
planning remains available. Missing, boolean, zero, list or object IDs remain
ambiguous rather than guessed from the contradictory documentation example.
No acknowledgement alone certifies protection or clears UNKNOWN.

An initial full run was interrupted by local disk exhaustion (temporary-file
creation returned ENOSPC); that run is not passing evidence. After the operator
freed storage, a temporary-file write succeeded with about 6 GiB available.
Fresh full offline run: 2,202 passed, 920 subtests passed, excluding the two
guarded PostgreSQL CI modules. Focused pilot: 26 tests, 21 subtests passed.
Ruff, project verification and diff checks passed. No exchange POST was sent.

### 2026-11-04 — Bind market CLOSE acknowledgement identity to signed GET

After placement-ack identity hardening in `7eab6dc`, independent review
found that `CLOSE_UNKNOWN` was still willing to reconcile a GET order
with an ID different from an already acknowledged `order/create` ID,
provided externalOid and other fields matched.

The offline-only `PositionStopPilot` now persists a valid positive
`data.orderId` from a close-order `success=true, code=0`
acknowledgement (MEXC current `/private/order/create` response shape).
It never stores raw response bodies, timestamp or exchange messages.
A conflicting second acknowledgement latches investigation across restart.
An exact externalOid/filled-volume GET cannot mark `FLAT_OBSERVED`
if its order ID differs from the persisted close ACK; malformed ACK
data stays ambiguous and is not guessed.

Added regressions for wrong GET order after restart, conflicting
close acknowledgements and malformed/private response objects.
This is **offline request/GET reconciliation only**:
no transport, live order or stop execution authorization,
no Keychain access, no account mutations, no Render deployment.
The broader unprotected entry-to-native-SL gap and the documented
full-stop quantity constraint remain unresolved.

Official close-entry envelope:
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-order


### 2026-11-04 — Independent signed CLOSE deal-history witness (offline; NO live orders)

The position-stop pilot's `CLOSE_UNKNOWN -> FLAT_OBSERVED` transition
now requires independently authenticated
`GET /api/v1/private/order/deal_details/{orderId}` trade rows,
not merely filled `order/external` state and an empty inventory.

- Every deal row must have a positive unique trade `id`, match the
  exact close `orderId`, `BTC_USDT`, `side=2` (close short), a
  finite positive price and quantity. Aggregate deal volume must equal
  exactly the one-contract close. Empty, partial, duplicated, foreign,
  invalid or excessive rows fail closed.
- The GET-only recovery bridge now reads the **same close order and
  deal history twice** across independent position/stop/order
  inventory bookends; any drift keeps `CLOSE_UNKNOWN`.
- If an acknowledged close POST ID exists, the prior durable
  `CLOSE_ACK_GET_IDENTITY_MISMATCH` gate remains binding; matching
  trade details cannot replace that requirement.
- No assertion of exchange-side deal-page completeness, crash recovery
  from a partially settled close, timing guarantees or atomicity is made.
- Three dedicated regressions cover bad/partial/duplicate deals,
  matching two-read execution and second-read drift. Existing
  close-reconciliation fixtures now supply explicit trade records.
  No new MEXC POST, Keychain access, Render deployment, or BOXWAVE change.

Exact code and tests: `8e6c99fe55eb2a0f8d47b580e0c3586f7b44d749`,
`50e2ca4d728e2ededdfcf2ca5bc8dc2c1284290d`.
At exact test SHA `50e2ca4`, GitHub Actions passed
all seven branch/PR workflows including real disposable PostgreSQL;
full offline suite: **2,208 tests plus 929 subtests passed**.
Source:
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-trade-records-by-order-id

**Still blocked:** actual position-origin STOP POST/GET acceptance, market SL
trigger+fill, ambiguity in `stoporder/place` full-volume contract
("less than closable quantity"), entry-to-native-stop uncovered gap,
timed emergency close, venue POST uncertain outcomes and real protected
runtime. There is deliberately no executable post transport or
production release permission; do NOT construe green CI as certification.


### 2026-11-04 — Reject stale native-stop placement review by exchange entry-fill updateTime

The previous offline position-native pilot allowed a `PLACE` request
proposal against any otherwise valid live short, even an entry
filled long ago. This could normalize an indefinite uncovered
interval in the next supervised test. Now:

- `PLACE` proposal and durable stage require the MEXC signed
  external-order `updateTime` as a strictly positive Unix
  **millisecond integer**. Missing/string/boolean values, a clock
  >2 seconds ahead, and filled entries older than the conservative
  **15-second review-age ceiling** are refused.
- A successful review stage records the signed entry update time
  and review deadline in the private local SQLite journal. Its
  redacted diagnostics indicate whether that deadline has passed,
  while `protection_gap_acceptable=false`, `entry_allowed=false`,
  `post_transport_available=false` and `live_authorization=false`
  remain unchanged.
- An aged/invalid entry does **not** disable `CLOSE` proposal
  eligibility: the emergency exit remains available for separate,
  operator-authorized execution.
- Regressions cover missing/bool/string/zero/stale/future
  signed entry timestamps and persisted journal deadline state.
- This gate does **not** establish that a position was protected
  within 15 seconds. In particular it does not measure receipt of
  the first live fill, network delays to POST, placement success,
  async quantity update, independent watchdog availability, or
  actual stop activation. The position remains uncovered until
  a native short MARKET SL is seen in authenticated GET. The
  hard notional and full-volume TP/SL contract ambiguities remain.

Code checkpoint: `dca9461d149b6630c590fb6f0f5c90c774c6da31`;
tests: `dd3ac21d0aa2e81a1d19d16ce7671b77e57cfb81`.
GitHub CI for exact test SHA: full offline suite
**2,210 tests + 935 subtests passed**; offline safety, split,
promotion, dashboard and isolated PostgreSQL branch/PR workflows
all green. No exchange POST, production change, Keychain read,
or BOXWAVE change was made.

Official order GET timestamp reference:
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-order-by-external-id

### 2026-11-04 — Concrete STOP full-position mode and true fill-age repair

MEXC official `POST /api/v1/private/stoporder/place` names
`volType=1` **partial** and `volType=2` **position** TP/SL.
The development-only position-stop pilot previously selected 1 for
an entire-position market SL. Now it explicitly proposes
`volType=2, profitLossVolType=SAME, vol=1, stopLossType=0`.
Authenticated GET reconciliation strictly requires position origin,
whole-position mode, same quantity semantics, matching ownership and
native MARKET stop. A returned `vol` may be omitted/zero for
whole-position semantics, or positive and **exactly equal** to the
observed one-contract position; malformed/negative/mismatched
volumes are rejected. Other protections (active state, trigger,
no reverse, price, explicit stop type) stay mandatory.

The MEXC docs still say total TP/SL quantity must be *less than*
closable quantity, while code 5004 describes rejection when
*greater than* closable. Full-volume equality is NOT certified
without an actual authorized venue experiment. No forced partial
stop, double placement, unsupported fallback, or order POST was
introduced. Limit-price parameter prose also conflicts with
the stated market-stop type and remains unresolved.

A second real safety issue was corrected: review age based on
`order/external.updateTime` could be falsely fresh after a late
order update. The pilot's signed GET snapshot now includes
two matching copies of entry `order/deal_details/{orderId}`
and validates unique trade IDs, exact order/symbol/side, positive
price, exact volume=1, and *each* millisecond timestamp.
It computes the earliest signed actual fill time (and fails
conservatively if the order update is earlier). Missing/partial,
duplicate, mismatched, stale or future timestamps reject STOP
placement review. The durable journal retains the earliest
fill/deadline and refuses to classify protection after that
15-second review deadline. A separately authorized emergency
CLOSE proposal remains available. This does **not**
automatically close the position or prove a bounded exchange
fill-to-stop activation gap; the pilot has **no POST transport**.

Exact code and tests: `0a300e275036d7508ea3da581c5e50d80f8a51e1`,
`3511506c74afd4dcac4b557098c8c40255f08960`,
`9f61272ac424b55d8fcfb7bf85113d07c7bb77fd`,
`def2860dee78a0aa3a771bfd792b5af0d2af3ea0`,
`43e167305ed6811bc6f28b549862f15d2f146730`.
At the final executable-code SHA `43e1673`, **all 7 CI workflows
passed**; full offline suite **2,218 tests plus 953 subtests**.
No credentials were accessed here, no live orders were placed,
and main-account production/Render/BOXWAVE were unchanged.

Exact next unresolved real decision: authorize one supervised
single-contract $50-subaccount venue trial (with an approved
finite exposure budget, independently reviewed immediate-close
and watchdog path) to observe actual `stoporder/place` acceptance
of full quantity, current `orderId=0, volType=2,
stopLossType=0` stop, followed by `change_plan_price`
identity+quantity verification. At every failure/unknown outcome,
do not retry blindly; reconcile GET and invoke the separately
approved close. This is a proposal, not current trade authorization.

Sources:
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position
https://www.mexc.com/api-docs/futures/error-code
https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-trade-records-by-order-id
