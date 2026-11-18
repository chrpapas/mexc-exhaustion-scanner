-- UNRELEASED V40 portfolio migration. NEVER auto-run on production.
-- Independently review + backup + explicit operator approval are required.
-- This table records exact signed/public contract terms used by the
-- fenced, atomic RESERVED -> SUBMITTING transition BEFORE any MEXC POST.
-- It does not certify exchange fills, liquidation or stop protection.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='15s';

CREATE TABLE public.t100_live_v40_entry_contract_specs (
    signal_id bigint PRIMARY KEY
        REFERENCES public.t100_live_v40_slot_reservations(signal_id),
    external_oid text NOT NULL UNIQUE CHECK(length(external_oid)>0),
    symbol text NOT NULL CHECK(symbol ~ '^[A-Z0-9]+_USDT$'),
    contract_size numeric NOT NULL CHECK(contract_size>0),
    volume_step numeric NOT NULL CHECK(volume_step>0),
    price_tick numeric NOT NULL CHECK(price_tick>0),
    reference_price numeric NOT NULL CHECK(reference_price>0),
    notional_usdt numeric NOT NULL CHECK(notional_usdt>0),
    volume_contracts numeric NOT NULL CHECK(volume_contracts>0),
    stop_price numeric NOT NULL CHECK(stop_price>0),
    ledger_sha256 text NOT NULL CHECK(ledger_sha256 ~ '^[a-f0-9]{64}$'),
    inventory_sha256 text NOT NULL CHECK(inventory_sha256 ~ '^[a-f0-9]{64}$'),
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT v40_entry_spec_notional_exact CHECK(
        notional_usdt = reference_price * contract_size * volume_contracts
    ),
    CONSTRAINT v40_entry_spec_lot_exact CHECK(
        mod(volume_contracts,volume_step)=0
    ),
    CONSTRAINT v40_entry_spec_stop_sl75_exact CHECK(
        stop_price = ceil((reference_price * 1.75)/price_tick) * price_tick
    )
);

CREATE FUNCTION public.t100_v40_reject_entry_contract_spec_mutation()
RETURNS trigger LANGUAGE plpgsql AS $v40_spec_immutable$
BEGIN
    RAISE EXCEPTION 'V40_ENTRY_CONTRACT_SPEC_IMMUTABLE' USING ERRCODE='P0001';
END;
$v40_spec_immutable$;

CREATE TRIGGER t100_v40_entry_contract_spec_immutable
BEFORE UPDATE OR DELETE ON public.t100_live_v40_entry_contract_specs
FOR EACH ROW EXECUTE FUNCTION public.t100_v40_reject_entry_contract_spec_mutation();

REVOKE ALL ON public.t100_live_v40_entry_contract_specs FROM PUBLIC;
-- Only the dedicated runtime role may INSERT once and SELECT exact prior
-- contract terms. No UPDATE, DELETE, TRUNCATE, DDL or ownership permission.
-- This grant is still subject to separate production review and migration.
GRANT SELECT, INSERT ON public.t100_live_v40_entry_contract_specs
  TO t100_live_executor_runtime;

COMMENT ON TABLE public.t100_live_v40_entry_contract_specs IS
  'UNRELEASED: immutable at-entry V40 contract economics; no live certification';
COMMIT;
