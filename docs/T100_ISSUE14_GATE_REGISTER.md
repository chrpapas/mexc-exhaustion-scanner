# T100/V40 release gate register — issue #14

Owner: Codex engineering. Updated: 2026-11-02 (Europe/Zurich).
Code tested: `ff40c29064385b552912d35954f711a6c5550e24`.
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
| G1 Source/strategy parity | FAIL | Fresh original and filtered chronological replays completed. Both 100/200-bps baseline entries, gate decisions and every frozen metric match. Entry audit: 146 candidates, 7,154 prior bars, 648 exact reference notionals, zero discrepancies. | Independently implement/audit full exit/fill/funding paths and four-slot runtime parity; archive has no full exit/fill paths. Resolve broad-suite failures. Original replay rerun is not independent strategy implementation certification. |
| G2 Current account capabilities | BLOCKED | Existing account observation/risk/eligibility modules and offline tests; dated public filter only. Current process has no MEXC read credentials. | Secure read-only credential availability, fresh complete account/key/contract/collateral inventory, unrelated-position isolation. Do not infer current rights from public eligibility. |
| G3 Venue execution/stress | UNKNOWN | Capacity, cross-margin and stop-gap models covered by focused offline suite; historical replay is candle-based. | Observed L2/partial-fill/queue/gap/funding/liquidation/multi-slot conflict evidence and model calibration. Never infer executable capacity from turnover or enable leverage. |
| G4 Fenced coordinator/journal | FAIL | Reservation, attempt/outcome and protected-commit modules; offline contention/fault tests pass. Migration 031 remains unapplied. | Complete certified runtime transport integration, independent PostgreSQL transactions/privileges/crash proofs at candidate SHA; no local postgres/Docker available. Existing isolated CI workflow is the safe integration path. Production migration requires approval. |
| G5 Native protection/recovery | BLOCKED | Pure/mock native-stop, checkpoint, trailing and nonflat recovery tests pass. | Independent venue stop create/update/close semantics, no uncovered interval, orphan/ambiguity and 0–4-position lifecycle proofs. Actual mutations require explicit operator approval. Never adopt manual positions. |
| G6 Fault/restart/interference | FAIL | Focused suite passes reservation contention, UNKNOWN/no retry, stale fencing, drift and restart simulations. | End-to-end runtime crash injection at every durable/venue boundary, real isolated DB evidence, four-worker/account interference review. Unit simulations alone do not close this gate. |
| G7 Render/shadow soak | BLOCKED | Repository manifest starts `app.t100_200_scanner` and `app.t100_200_trader`; draft separate shadow starts `app.t100_live_shadow_runtime`, manual deploy. These are repository facts, not a running-build attestation. | Operator-approved deployment, actual running SHA, sustained uninterrupted soak, Discord delivery evidence. No deployment/service change authorized or performed; preserve kill latches. |
| G8 Independent release/security review | FAIL | Readiness fixes committed; focused CI suite passes; rollback/emergency runbooks exist. | Independent code/DDL/security/dependency review, clean applicable CI and disposition of broad-suite failures, verified rollback/incident drill. No reviewer approval claimed. |
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
