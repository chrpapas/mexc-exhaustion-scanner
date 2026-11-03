-- CREATE-ONLY, NOT APPLIED. Frozen V40 four-slot preparation journal.
-- Separate from the old VOL30 H4 ENTRY campaign parent/child tables.
-- This migration MUST NOT run in the current live-migration CLI (027-030).
-- Operator approval, backup, role grants, recovery tests and migration pinning
-- are prerequisites for a future database rollout. NO MEXC mutations here.
CREATE TABLE public.t100_live_v40_slot_reservations (
    signal_id BIGINT PRIMARY KEY CHECK (signal_id > 0),
    symbol TEXT NOT NULL CHECK (symbol ~ '^[A-Z0-9]+_USDT$'),
    external_oid TEXT NOT NULL UNIQUE,
    modeled_notional_usdt NUMERIC NOT NULL CHECK (modeled_notional_usdt > 0),
    ledger_sha256 TEXT NOT NULL CHECK (length(ledger_sha256) = 64),
    inventory_sha256 TEXT NOT NULL CHECK (length(inventory_sha256) = 64),
    snapshot_observed_at TIMESTAMPTZ NOT NULL,
    holder_id TEXT NOT NULL CHECK (length(holder_id) > 0),
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    state TEXT NOT NULL DEFAULT 'RESERVED' CHECK (
        state IN ('RESERVED','SUBMITTING','UNKNOWN','ACK',
                  'PROTECTED','HALTED','RELEASED')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Prevent a second signal entering an owned, pending or unresolved symbol.
-- RELEASED must only follow independently verified closed-position accounting
-- and a clean exchange/account-wide recheck; this module cannot release.
CREATE UNIQUE INDEX t100_live_v40_one_active_symbol
ON public.t100_live_v40_slot_reservations(symbol)
WHERE state <> 'RELEASED';

CREATE INDEX t100_live_v40_pending_resolution
ON public.t100_live_v40_slot_reservations(state, created_at)
WHERE state IN ('SUBMITTING','UNKNOWN','ACK','HALTED');

-- The four-slot ceiling cannot be enforced by a row-local CHECK.
-- The reservation coordinator takes an account-wide advisory transaction
-- lock FIRST, then verifies current leader fencing, ledger, all active
-- checkpoints + reservations, and finally inserts exactly one reservation.
-- Every future writer to this table MUST use that same lock discipline.
