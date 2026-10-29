-- TEMPLATE ONLY. DO NOT RUN UNCHANGED.
-- Replace __T100_LIVE_RUNTIME_ROLE__ with the dedicated restricted runtime
-- PostgreSQL user created for the T100 live service.
--
-- This role is intentionally distinct from the migration/table-owner role.
-- It can mutate only the three durable state-machine tables and can append
-- immutable evidence. It receives no DELETE or TRUNCATE rights.

GRANT USAGE ON SCHEMA public TO __T100_LIVE_RUNTIME_ROLE__;

-- Start fail-closed.
REVOKE ALL PRIVILEGES ON TABLE
    t100_live_order_intents,
    t100_live_position_checkpoints,
    t100_live_position_checkpoint_revisions,
    t100_live_stop_amendments,
    t100_live_order_fill_sets,
    t100_live_order_fills,
    t100_live_position_funding_sets,
    t100_live_funding_events,
    t100_live_final_position_accounting,
    t100_live_vol30_parents,
    t100_live_vol30_children,
    t100_live_executor_leases
FROM __T100_LIVE_RUNTIME_ROLE__;

-- Durable mutable state machines: no DELETE/TRUNCATE.
GRANT SELECT, INSERT, UPDATE ON TABLE
    t100_live_order_intents,
    t100_live_position_checkpoints,
    t100_live_stop_amendments
TO __T100_LIVE_RUNTIME_ROLE__;

-- Immutable evidence: append + read only.
GRANT SELECT, INSERT ON TABLE
    t100_live_position_checkpoint_revisions,
    t100_live_order_fill_sets,
    t100_live_order_fills,
    t100_live_position_funding_sets,
    t100_live_funding_events,
    t100_live_final_position_accounting
TO __T100_LIVE_RUNTIME_ROLE__;

-- Research/V40 full-strategy order-state tables (migration 028).
-- Parent economic/identity fields are immutable after insertion. Only the
-- lifecycle status may be advanced through the application CAS protocol.
GRANT SELECT, INSERT ON TABLE
    t100_live_vol30_parents
TO __T100_LIVE_RUNTIME_ROLE__;
GRANT UPDATE (status) ON TABLE
    t100_live_vol30_parents
TO __T100_LIVE_RUNTIME_ROLE__;

-- Child identity, size and historical liquidity inputs are immutable.
-- Application logic may advance state, exchange ACK/fill and updated_at.
GRANT SELECT, INSERT ON TABLE
    t100_live_vol30_children
TO __T100_LIVE_RUNTIME_ROLE__;
GRANT UPDATE (state, filled_notional_usdt, exchange_order_id, updated_at)
    ON TABLE t100_live_vol30_children
TO __T100_LIVE_RUNTIME_ROLE__;

-- Leader fencing (migration 030): lease_name is immutable after creation.
GRANT SELECT, INSERT ON TABLE
    t100_live_executor_leases
TO __T100_LIVE_RUNTIME_ROLE__;
GRANT UPDATE (holder_id, fencing_token, lease_expires_at, updated_at)
    ON TABLE t100_live_executor_leases
TO __T100_LIVE_RUNTIME_ROLE__;

-- BIGSERIAL inserts need nextval() plus read access. UPDATE is deliberately
-- withheld so setval() is unavailable to the runtime role.
REVOKE ALL PRIVILEGES ON SEQUENCE
    t100_live_order_intents_id_seq,
    t100_live_position_checkpoint_revisions_revision_id_seq,
    t100_live_stop_amendments_id_seq,
    t100_live_vol30_children_id_seq
FROM __T100_LIVE_RUNTIME_ROLE__;

GRANT USAGE, SELECT ON SEQUENCE
    t100_live_order_intents_id_seq,
    t100_live_position_checkpoint_revisions_revision_id_seq,
    t100_live_stop_amendments_id_seq,
    t100_live_vol30_children_id_seq
TO __T100_LIVE_RUNTIME_ROLE__;

-- Verification is performed by app.t100_live_schema_gate and
-- app.t100_live_db_inspect under the dedicated runtime credential.
-- Both must pass. Column-level UPDATE access for VOL30/lease is deliberately
-- narrower than generic table-wide UPDATE permissions.
-- These are proposed grants, not permission to trade; an independent review
-- and in-database effective privilege test remain mandatory.
