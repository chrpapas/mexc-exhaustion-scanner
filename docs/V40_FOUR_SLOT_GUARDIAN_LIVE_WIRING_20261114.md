# V40 independent guardian 2–4 active SHORTs (READ-ONLY)

This change connects the already-reviewed 1–4 slot account and native-stop
identity checks to the actual dedicated guardian observation loop.

Previously, the live guardian could report healthy only for FLAT or
exactly ONE 1x CROSS SHORT. A legitimately managed second SHORT would
always produce a critical alert even when every native stop and durable
checkpoint was individually correct. That prevented a real multi-slot
strategy from ever being monitored.

Now after the same signed 2× exchange inventory/hedge snapshots,
full-account completeness and immutable PostgreSQL read, the guardian
also inspects 2–4 exact owned CROSS SHORTs via the existing offline-vetted
`audit_frozen_four_slot_native_stops` policy. Every exact native
MARKET stop must be present and full-volume, with corresponding entry
order, checkpoint, position and immutable provenance, and no foreign or
duplicate positions. The extra ledger read must match the coordinator's
earlier digest.

A positive observation maps to the existing protected-owned status
in the durable incident outbox, while the heartbeat and JSON now include
the exact protected-position count 1–4. Invalid source credentials,
missing stops, concurrent changes, unowned positions or a fifth
position remain CRITICAL and durably latched. The OLD single-position
1x policy is unchanged for exactly ONE position.

**Important limitations:** This is a GET-only monitoring enhancement.
It neither certifies live leverage/margin or liquidation risk nor
permits entry. The portfolio executor, native SL75 trailing amendments,
exit reconciliation and actual four-slot POST authorization are still
separate. The guardian may not be used as a surrogate for them.

No MEXC POST, SQL write, migration or leverage change occurs in this code.
