# T100/V40 release gate register — issue #14

Owner: Codex engineering. Updated: 2026-11-02 (Europe/Zurich).
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
