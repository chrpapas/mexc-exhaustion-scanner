-- DEVELOPMENT ONLY. CREATE-ONLY V40 one-shot dispatch-claim fence.
-- NEVER apply automatically to production; migration 031 also remains unapplied.
-- This records the irreversible possibility of a venue POST. It does not
-- authorize or transmit orders, verify a fill/stop, or allow a retry.
-- A crash after insertion is indistinguishable from a successfully sent POST.
-- Therefore there is NO reset/release/retry transition.
CREATE TABLE public.t100_live_v40_dispatch_claims (
    signal_id BIGINT PRIMARY KEY REFERENCES public.t100_live_v40_slot_reservations(signal_id),
    external_oid TEXT NOT NULL UNIQUE,
    holder_id TEXT NOT NULL CHECK (length(holder_id) > 0),
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    state TEXT NOT NULL DEFAULT 'POST_POSSIBLE_UNRESOLVED'
      CHECK (state = 'POST_POSSIBLE_UNRESOLVED'),
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
-- Only a single leader-fenced worker wins INSERT ON CONFLICT DO NOTHING.
-- A duplicate call, another worker, or a restart may ONLY reconcile by
-- exact externalOid; do not infer from a missing order that POST was absent.
-- The claim must be taken AFTER durable SUBMITTING and a separately checked
-- current MEXC/account preflight; this schema does not implement that preflight.
-- No INSERT/UPDATE/DELETE grants are included for production roles.
