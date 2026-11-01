# T100/V40 — Codex engineering handoff to release readiness
**Date:** 2026-11-02
**Status:** engineering work authorization; **NOT** authorization to deploy, grant order permissions, enter live mode, or transmit orders.
**Repository:** `chrpapas/mexc-exhaustion-scanner`, feature branch `feature/t100-candidate-private-tier-observer-20261030`
**Local research root:** `~/trader-backtest`
**Original simulator (read-only):** `~/Downloads/t100_chronological_contract_lots_v39_1/contract_replay_v40_scale.py`
**Original simulator pinned SHA-256:** `bf8ba6a08332abdb5bdddd38356249a08b8c3e85d4d695a426bbd51b3d51867c`
**Frozen V40 parity ZIP:** `~/v40_parity_for_live.zip` pinned SHA-256 `2918f4589e8d564ca55b6f24aa2e9a868b5d214653ad7d92acaf1729fd1e4cb5`
**Current public MEXC filter (LOCAL ONLY):** `~/trader-backtest/v40_api_prefilter_20261101.json`; current-public, not historical or account permission.
**Separate BOXWAVE project:** strictly out of scope. No code, data, credentials, alert or deployment changes there.

## Goal and operating method

Own the **engineering work through a reviewable release candidate** for full autonomous 4-slot V40 execution on MEXC/Render. Make code changes, run local tests and backtests, investigate failures, iterate, commit and report precise evidence without operator copy-pasting every terminal output. Use Codex local/project access with approvals and sandboxing; keep source-controlled checkpoints. This document does **not** give this ChatGPT conversation remote terminal access or start an always-on agent on its own.

**Work autonomously on code, static analysis, mocks, read-only data checks, paper/shadow execution and tests.** Pause for explicit operator approval before any real-account mutation, production deployment, security/credential or API-key permission change, migrations on production, service configuration, full-account exposure, actual exchange POST/order/stop mutation, or actions affecting unrelated positions.

"100% engineering ready" means every applicable **pre-release gate below has evidence and independent review**. **It does not mean real-world profitability, zero chance of loss, or certified live exchange execution.** Full production certification may require a separately approved limited-risk venue test and/or controlled release with rollback. Never present unsupported gates as passed. Do not flip a global flag or disable guards to reach 100%.

## Frozen strategy (do not silently change)

- T100 V40 `V32_VOL30_SKIP_H4_S100_CAP5_V39_HYBRID`: four slots; one open position per symbol.
- Candidate history is 49 consecutive, strictly prior 30m bars; ATR7 floor 4%, HIGH tier >=5.7%, VOL30_SKIP for prior-24h close volatility >=30%.
- LOW pre-clip target 25% account equity / slot; HIGH pre-clip target 62.5% account equity / slot; up to 250% target gross exposure before turnover, stress, contract and account limits.
- H4 refers to four 30m bars of **stressed exit-capacity sizing**, *not* permission for an entry campaign over 120 minutes. Preserve same-candle historical decision semantics; actual venue fills require a distinct model.
- Historical proxy assumes 5x for shock budget. **This is NOT permission to set 5x leverage live.**
- Model initial SL75 and trailing activation around 14% with a 0.5 percentage-point gap; model stops are not guaranteed protective exchange fills.
- Preserve exact source, parity and data provenance for all changes; deviations must be documented and separately validated.

## Verified evidence at handoff (historical, not live executable)

| Control | Original | Public-API-filtered |
|---|---:|---:|
| Frozen candidate signal count | 146 | 128 |
| Distinct candidate contracts | 111 | 98 eligible, 13 excluded |
| Entries | 108 | 98 |
| Unique traded contracts | 85 | 76 |
| Final equity at $2k, 100 bps/side, HYBRID | $14,476.81 / 7.2384x | $13,356.21 / 6.6781x |
| Modeled high-DD at 100 bps/side | -54.8664% | -55.9209% |
| Final equity at $2k, 200 bps/side, HYBRID | $9,030.38 / 4.5152x | $8,089.53 / 4.04477x |
| Modeled high-DD at 200 bps/side | -63.7195% | -66.2204% |

Filtered original chronological replay reconciled to frozen baseline at 100 bps. 200 bps figures from local original/filtered metrics CSV, not separately certified as executable fills. Filter is **current** public contract-country metadata only, not point-in-time historical permissions or private API-key trading rights. Backtest profits and deep drawdowns are **not** forecasts.

Local outputs:
- `~/trader-backtest/v40_api_baseline_replay_20261101`
- `~/trader-backtest/v40_api_filtered_replay_v3_20261101`

## Release-readiness work packages and strict acceptance gates

1. **Repo/source truth and non-regression:** inventory all live vs shadow vs prototype modules; identify exactly which code Render executes; preserve original simulator and frozen ZIP. Confirm baseline replay metrics, current-public-filter replay, single-bar timing, 4-slot sizing/occupancy, exits, fees and funding. No leakage/forward data. Any proposed live deviation has explicit evidence and label.
2. **Account-specific MEXC contract and capability matrix:** read-only corroborate up-to-date per-symbol API order eligibility, margin modes, leverage/risk tiers, min/max lots, current country restrictions, private API-key capability (without enabling it), open orders, unrelated manual positions and account completeness. Distinguish UNKNOWN from DENIED; historical permission remains unknown absent dated evidence.
3. **Execution and venue realism:** backtest/forward paper against actual observed bid/ask/depth, queue/partial fills, slips, fees, funding, gap past stop, liquidation/margin cross-shocks, lot/volume and max-order constraints, four-slot conflict. Do not equate order-book snapshots with executable fills or leverage with capacity.
4. **Four-slot coordinator and durable journal:** build single leader-fenced multi-slot coordinator with account-wide reservations, durable `PREPARED→SUBMITTING→ACK/UNKNOWN`, stable origin order ID, no POST retry on UNKNOWN, complete reconciliation. Validate database schema/privilege/migration through reviewed isolated integration; **no production migration without separate approval**.
5. **Native protected position lifecycle:** stop create/verify/update/cancel/close with independent MEXC state witness, no period of uncovered position, durable checkpoints, monotonic trailing, orphan/fill ambiguity resolution, cross-account conflict and collateral exposure gating. Never adopt user's manual positions.
6. **Crash, restart and fault injection:** independently reproduce failures between every journal/POST/fill/stop/DB state transition, restart and duplicate worker, WS/REST disagreement, stale reads and fencing takeover. No double orders; unverified positions remain fail-closed with actionable incident/Discord alerts.
7. **Read-only Render integration and shadow soak:** after approved read-only deployment, verify the actual running build, automatic discovery, observations, continuous process/Discord health and sustained uninterrupted soak, with incident logs, provenance and reconcilable replay results. Existing shadow kill latches must not be silently cleared.
8. **Independent security and release review:** review all code/DDL, dependency risks, secret exposure, emergency stop/flatten/rollback procedures, alert delivery, runbooks and prepared live configuration. Require third-party/independent review where feasible, not just agent self-attestation. Deliver an evidence bundle with exact commits, test outputs, unresolved issues and rollback controls.
9. **Venue POST-dependent validation (separate phase):** where only actual order/stop lifecycle can certify exchange behavior, mark `BLOCKED_PENDING_EXPLICIT_OPERATOR_AUTHORIZATION`. Propose a **small, separately approved, loss-bounded** real-venue test with max-loss/kill controls and manual override. Do not execute tests involving funds until approved. Full four-slot production must not be the first live lifecycle test.

## Agent operating instructions

- Keep a single **gate register** with ID, owner, status `PASS|FAIL|BLOCKED|UNKNOWN`, exact code SHA, evidence path/date, reproducer and remaining action.
- Prioritize resolving concrete release blockers, not endless alternate backtest sweeps; fix issues as found, run tests, commit in reviewable increments, and open a PR for review.
- Don't provide fabricated percentage. A progress percentage, if requested, must explicitly name its denominator and separately report **release-critical blocked gates**.
- You may read local market datasets, run paper/backtest/CI and edit development branches autonomously inside your authorized workspace. No automatic trades, production DB writes, MEXC API-key updates, Render production deploys, or Discord webhook reconfiguration.
- Keep operator secrets in existing secure local environment and never print, commit, copy into issue bodies or paste into chat. Minimize terminal logs with symbols/positions/identifiers, and avoid exposing irrelevant private account information.
- When blocked, document exact gate, safe diagnostic, minimum human approval/credential needed, and a proposed next action. Do not bypass a gate to claim completion.

## Deliverables before asking the operator to deploy

- Reviewed release candidate with clean CI and reproducible deterministic four-slot full-strategy parity tests.
- Evidence register with each engineering gate PASS/FAIL/BLOCKED and explicit date/provenance.
- Account-specific contract/stop/WS/private-read capability matrix, with uncertainty where no genuine evidence exists.
- Protected order/stop/recovery drill report and no unresolved safety-critical failures.
- Read-only Render shadow evidence and Discord alert test.
- Staged production plan with sandbox/restricted pilot gate and verified emergency controls, rollback and owner approvals.
- One brief decision memo: what can safely deploy in read-only mode, what needs limited real-money validation, what is **not** authorized.

## First Codex task

Read this file and `docs/T100_FULL_V40_PRODUCTIONIZATION_PLAN_20261028.md`, `docs/T100_FULL_V40_LIVE_TARGET_20261027.md`, and `docs/T100_LIVE_READINESS_EVIDENCE.md`. Inspect current branch, safety gate code and existing CI. Reconstruct a **source-verified** release gate register; identify top blockers and implement the highest-impact *safe development-only* engineering task. Run local tests; commit with an evidence note. Continue iterating across the engineering gates without waiting for operator terminal copy-paste; pause only when you need explicit external permissions or a protected operator decision.

**HARD BLOCK:** `entry_allowed=false`, `live_authorization=false`, `mexc_orders_sent=false` until separate approval and independently verified scope-specific live permits. Never infer live consent from "go", "continue", GitHub CI success, or this handoff.
