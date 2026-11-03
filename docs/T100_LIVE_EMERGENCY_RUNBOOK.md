# T100 live emergency response runbook

**Status:** review draft. This document does not authorize live trading.

This runbook is for a future supervised T100 live pilot on MEXC. The current
Render shadow remains read-only. When exchange state is uncertain, the default
response is **stop autonomous mutation and reconcile first**.

## 1. Events that require immediate operator attention

Treat any of the following as an incident:

- native protection missing or no longer linked to the observed position
- live kill latch trips for any reason
- order submission outcome is UNKNOWN
- stop amendment outcome is UNKNOWN
- durable checkpoint differs from exchange state
- account inventory completeness/reconciliation fails
- unrelated manual/open order appears
- leader lease is lost or fencing token is stale
- MEXC private reads fail repeatedly
- clock skew or stale-book gate fails
- Discord reports a position/protection transition that was not expected
- liquidation, ADL, execution-failed stop state, or other nonstandard close

## 2. First response

1. **Do not restart or redeploy the live executor merely to clear a latch.**
2. **Do not retry an order or stop mutation whose outcome is unknown.**
3. Keep the read-only shadow/diagnostic path available when possible.
4. Open the MEXC Futures UI and inspect:
   - current positions
   - current TP/SL protection
   - current regular/plan/trailing orders
   - recent order and TP/SL history
5. If there is an open position, establish whether exchange-native protection
   is currently present and appropriate before doing anything else.

A restart is not a recovery procedure. Recovery means reconciling the durable
journal/checkpoint with exchange state.

## 3. Unknown entry-order outcome

If a POST timed out or returned an ambiguous result:

1. Query the durable intent by externalOid.
2. Query MEXC by the same externalOid.
3. If an order is found, reconcile its immutable orderId and order-specific
   fills. **Never submit another entry.**
4. If no order is found, leave the intent UNKNOWN and keep live mutation
   blocked. Absence in one read is not proof that the POST failed.
5. Escalate for operator review before any new entry attempt.

## 4. Unknown stop-amendment outcome

If a native stop price amendment is ambiguous:

1. Keep the durable amendment in SUBMITTING/UNKNOWN.
2. Read current TP/SL state.
3. If the same stop ID is active at the intended tighter price, verify it.
4. If MEXC replaced the stop ID, require:
   - one active replacement stop at or tighter than the intended price, and
   - finished history proving the previous stop was canceled untriggered.
5. If the old stop is unchanged, the outcome remains unresolved.
6. If no protective stop is present, treat this as a protection incident.
7. **Never blindly send the amendment again.**

## 5. Open real position without verified native protection

This is the highest-priority incident.

1. Stop all autonomous entry/stop mutations.
2. Use the MEXC UI to inspect the exact current position and current TP/SL.
3. If the position should remain open, restore an appropriate exchange-native
   stop manually in the UI and verify it is visible under current TP/SL.
4. If protection cannot be established promptly or the position should not
   remain exposed, close the position manually from the MEXC UI using the
   exchange's normal close-position flow.
5. After the manual action, verify:
   - position inventory
   - current TP/SL inventory
   - open regular/plan/trailing order counts
6. Keep the live executor blocked until durable state is reconciled.

The bot must not attempt an automated emergency flatten until that path has
its own independently reviewed journal, idempotency, fill-completeness and
post-close reconciliation evidence.

## 6. Leader/fencing incident

If another instance starts or a fencing token becomes stale:

1. Disable further mutations from the stale instance.
2. Do not attempt to reclaim leadership by repeatedly restarting workers.
3. Reconcile current exchange state and unresolved mutation journals.
4. Allow takeover only after the old lease has expired or been explicitly
   released.
5. The new holder must receive a strictly newer fencing token before any POST.

## 7. Manual intervention detected

If a manual order, position change, or unrelated order appears:

1. Freeze autonomous mutation.
2. Reconcile the entire current account inventory.
3. Do not import the manual action as a strategy action by inference.
4. If the manual action changes the managed position, move the checkpoint to a
   nonrecoverable review state until explicitly reconciled.

## 8. Nonstandard close: liquidation, ADL, invalid/failed stop

Do not classify these as a normal strategy stop.

Require separate review of:
- terminal TP/SL/order state and error code
- position disappearance/remaining quantity
- order-specific fills
- fees and funding
- final account inventory

Do not write a normal CLOSED strategy checkpoint unless the immutable close
provenance and final accounting chain is satisfied.

## 9. Discord / communication failure

If Discord is unavailable:

- future live mutation must remain blocked when alerts are required
- do not weaken the alert requirement to keep trading
- use the MEXC UI and Render logs for immediate incident inspection
- restore alerting and verify a test notification before re-arming

Never paste API keys, API secrets, signed headers, or Discord webhook tokens
into chat, tickets, screenshots, or incident notes.

## 10. Evidence to retain after an incident

Retain only the minimum redacted evidence needed for review:

- UTC incident start/end
- kill reason
- durable intent/amendment/checkpoint states
- order/position/stop identifiers in the protected database, not public logs
- redacted account inventory counts/status
- whether native protection was continuously present
- whether any exchange mutation had UNKNOWN outcome
- final reconciliation result
- operator action taken

## 11. Re-arm checklist

Do not re-arm until all are true:

- exchange state is stable and current inventory completeness passes
- account is reconciled
- every managed position has verified native protection
- no unresolved entry intents
- no unresolved stop amendments
- no stale leader/fencing token
- Discord alerting works
- clock and market-data freshness gates pass
- durable checkpoint/revision history matches exchange state
- if an incident closed a position, final fills/funding/accounting reconcile
- the original kill reason is understood and reviewed
- explicit operator approval is given for the next supervised pilot action

A one-way kill latch should be cleared only by starting a newly reviewed live
session after reconciliation; clearing a latch is never itself proof of safety.
