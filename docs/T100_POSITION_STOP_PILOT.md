# Position-origin stop pilot — offline review candidate, 2026-11-04

This separate lifecycle is implemented in `app/t100_live_position_stop_pilot.py`.
It has **no POST transport, credential loader, entry submitter or production
integration**. `dispatch` always raises. The global production gates remain
unchanged. Its request proposals are review artifacts, not executable permits.

## Evidence boundary

The operator-reported $50 subaccount experiment established authenticated access,
one accepted minimum BTC isolated 1x short and its close, followed by observed
flat inventory. Its embedded stop was order-attached, missing `stopLossType`,
and eventually canceled without triggering. None certifies position-native
placement, market-stop execution, amendment or complete deal-history recovery.
The new code rejects this shape as protection. No new venue evidence was obtained.

## Implemented lifecycle

A journal is bound to an account fingerprint, exact entry external ID, position
ID and a hard limit of one BTC contract. Supplied snapshots require a filled entry origin,
exact short position identity, isolated mode, 1x leverage, matching volume,
coherent account-wide order counts and fresh timestamps. They cannot adopt an
unrelated manual position. The GET-only adapter consumes `ReadOnlyMexcProbe`
using the caller's already authenticated client; it never accesses credentials.
Inventory is bookended around origin, close-order and bounded history reads.
Bookends are non-atomic observations, not exchange atomicity certification.

Placement proposes `/api/v1/private/stoporder/place` with position identity,
explicit volume, market SL, no reversal and latest-price trigger. Existing stops
block placement to avoid duplicate/asynchronous same-price updates. Reconciliation
requires exact stop ID and price, position-origin `orderId=0`, explicit full volume,
active/untriggered state and the existing strict native-market-stop audit.

Amendment proposes `change_plan_price` for the observed stop ID, only tightens
the price, and journals the old/new request. The exact new price and unchanged
native semantics must subsequently be observed. A POST acknowledgement alone
has no state-transition API and cannot establish protection.

Close proposals retain protection until signed GET shows flat inventory and
the exact close-origin filled volume. Cancellation is restricted to the known
stop after flatness; terminal identity/state must reconcile before completion.
Automatic cancellation after close uses the same terminal evidence requirement.
Missing history or partial close remains unresolved. An executed stop is never
misclassified as canceled or certified by this workflow.

SQLite FULL synchronous transactions latch `*_UNKNOWN` before a proposal could
leave the process. Restart and concurrent callers cannot stage that request
again. Unknown placement/amendment permits only fresh, separately scoped close
planning or GET reconciliation. Transport failure leaves the durable unknown
state unchanged. Diagnostics contain state/revision and false certification
flags, not account/order/position identifiers or private exception bodies.
Keep this local journal private, retain it across restarts, and do not delete or
replace it to retry. Local journal storage is trusted; this is not a hardened
multi-host production ledger.

A review approval expires within 60 seconds and binds account plus exact request
hash/revision. The phrase explicitly says REVIEW_ONLY. It never authorizes a
real POST; a separately reviewed supervised executor is still required.

## Official contract research and unresolved points

- [Position placement](https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position): documents market SL and positional quantity; may reuse an existing same-price ID and asynchronously change volume. The response prose and example differ. Limit-price fields are marked required despite market types. The candidate deliberately leaves those ambiguous fields unresolved; payload acceptance is unverified.
- [Current stop rows](https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-current-take-profitstop-loss-order-list): distinguishes position origin, quantity modes, trigger/state and SL type. Missing fields remain blockers; the sample is not a default-value guarantee.
- [Planned stop amendment](https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/modify-tpsl-prices-on-a-tpsl-planned-order): documents `stopPlanOrderId`; actual position-stop ID mapping and retained market semantics need venue observation.
- [Stop cancellation](https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/cancel-tpsl-planned-orders): documents a list of stop IDs; exact accepted wire envelope and cleanup timing remain unverified.

## Exact next supervised verification — NOT authorized now

1. Independently review the candidate, official parameter ambiguities, account
   binding and a concrete maximum-loss/time budget. Review a separate executor
   that cannot reach other accounts; approve one exact experiment explicitly.
2. On the dedicated flat subaccount, reconfirm zero inventory/orders and current
   minimum contract size. If separately approved, open one minimum isolated 1x
   short with an explicit emergency-close/time limit. This creates an uncovered
   interval: do not describe entry plus later stop placement as atomic protection.
3. Place one position-origin market SL by its actual position ID. Persist the
   attempt first. Obtain signed GET evidence of exact identity, price, quantity,
   market type, no reversal and active state. Missing/ambiguous fields stop the
   experiment and require the separately approved emergency-close procedure.
4. Amend that exact stop once to a reviewed tighter, non-triggering price; verify
   the same identity and all semantics by signed GET. No blind retries.
5. Close the exact position, reconcile its filled close order/deals and flatness,
   then reconcile automatic cancellation or cancel only its surviving owned stop.
   Preserve terminal history and the journal. Market-stop triggering/execution
   requires a distinct expressly approved experiment; cancellation does not prove it.

No production enablement follows from a successful experiment. Repeated/gap
execution, recovery, collateral and independent release review remain required.

## Protection-gap decision and recovery limits

**BLOCKED: acceptable protection-gap risk has not been demonstrated.** There is
no atomic entry-plus-position-stop contract here. Exposure starts at the first
entry fill, before its position ID is known, and lasts until active full-volume
native protection is verified. Timeouts, a crash, missing GET fields, placement
rejection or delayed asynchronous updates can extend that interval indefinitely.
An emergency market close can also fail or fill partially; it cannot guarantee
a maximum loss. The $50 balance and 1x isolated setting alone do not certify a
loss cap or a tolerable uncovered interval. No entry proposal/transport is exposed.
A future executor must have a durable pre-entry intent, independent watchdog,
measured fill-to-protection deadline, approved loss budget and tested close/recovery
path. Until independently reviewed and explicitly approved, stop at this blocker.

The official placement page literally says total stop volume must be **less than**
closable volume. That conflicts with this experiment's required full protective
quantity. Obtain venue clarification/evidence for equality and the chosen
`volType=1` full-sized partial-stop request; do not reduce quantity merely to get
acceptance. Required limit-price fields and the contradictory response example
also need resolution before a request becomes executable.

Supplied POST outcomes are journaled only as ACK/REJECTED/AMBIGUOUS; none clears
UNKNOWN or permits a retry. No raw response/message is persisted. Emergency close
can be planned from NEW (unprotected) or unknown stop states, retaining superseded
attempts. A close attempt itself cannot be blindly repeated. Unknown stop identity,
partial fills, spontaneous stop execution and absent terminal history remain
fail-closed reconciliation blockers requiring separate supervised investigation.
No cancellation is proposed before confirmed flatness. Replacing a journal or
using multiple journal files defeats local idempotency and is forbidden by the
operating procedure; this prototype does not provide account-wide distributed
fencing or exactly-once venue delivery. GET evidence is caller-bound, not signed
or tamper-proof inside this offline harness.

## Further contract and hard-limit review

The official order endpoint exposes market, IOC and FOK order types, but does
not provide an upper execution-price cap for a sell opening order in its request
schema. A sell limit constrains the minimum sale price, not its maximum; switching
to IOC/FOK alone therefore does not prove a strict maximum opening notional.
The previous v3 market-entry precheck compares quoted notional only. A subsequent
fill-price check detects a breach after exposure exists; it cannot prevent it.
Do not silently reinterpret the operator's 12.50 USDT hard ceiling as a quote
estimate. A documented enforceable price envelope or explicit operator revision
of that constraint is required before executable entry approval.

Public GET during this review returned BTC_USDT contractSize=0.0001, minVol=1,
volUnit=1, priceUnit=0.1, state=0; last/bid=78254.8 and ask=78254.9 USDT.
One-contract reference notional was 7.82548 USDT. This is transient public
market evidence, not a fill guarantee or private account permission.

The offline request is now aligned with MEXC's documented **entire-position**
mode: `volType=2`, `profitLossVolType=SAME`, explicit request `vol=1`,
and `stopLossType=0` (market). Earlier `volType=1` was the documented
*partial* TP/SL mode and is now rejected during GET placement/amendment
reconciliation even if its amount happens to equal the current position.
A whole-position GET may report positive explicit quantity equal to the
observed one-contract position, zero, or omit `vol`; a nonzero mismatched,
negative or malformed volume fails closed. Position identity, SHORT side,
market-stop type and current state remain mandatory.

**This resolves the mode mismatch in our request; it does NOT establish
acceptance by the venue.** The placement page still says total stop quantity
must be strictly less than closable quantity while the 5004 exchange error
description rejects quantity *greater than* closable quantity. Equality
remains undocumented/untested; never reduce stop quantity to work around
a rejection. The mandatory limit-order price fields even for market type
are also unresolved. This candidate has no POST transport and is
review-only; actual MEXC acceptance and effective protection remain
unverified, as does the uncovered entry-to-native-stop interval.

References:
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/place-order
https://www.mexc.io/api-docs/futures/account-and-trading-endpoints/place-tpsl-order-by-position
