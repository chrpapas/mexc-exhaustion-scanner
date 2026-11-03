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
