-- T100 guardian OBSERVER account enrollment (review-only / no production writes).
-- This table deliberately has NO enrollment rows and NO GRANT.
-- Enrollment requires a separate, audited manual action after confirming
-- the exact T100 entry order belongs to the dedicated MEXC account.
-- Never auto-create a mapping from a single observed exchange position.
CREATE TABLE public.t100_guardian_account_enrollments (
    scope_sha256 char(64) NOT NULL CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    position_id text NOT NULL CHECK (position_id ~ '^[1-9][0-9]*$'),
    symbol text NOT NULL CHECK (symbol ~ '^[A-Z0-9]+_USDT$'),
    external_oid text NOT NULL CHECK (external_oid ~ '^t100[A-Za-z0-9_]{8,60}$'),
    exchange_order_id text NOT NULL CHECK (exchange_order_id ~ '^[1-9][0-9]*$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_sha256, position_id),
    UNIQUE (position_id),
    UNIQUE (scope_sha256, external_oid)
);
REVOKE ALL ON public.t100_guardian_account_enrollments FROM PUBLIC;

-- Allow insertion only by a separately approved migration/enrollment operator.
-- Even owner changes are disallowed by triggers unless the schema itself is
-- deliberately and separately migrated by a privileged administrator.
CREATE FUNCTION public.t100_guardian_enrollment_no_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'T100_GUARDIAN_ENROLLMENT_IMMUTABLE';
END;
$$;
CREATE TRIGGER t100_guardian_enrollment_no_mutation_trigger
BEFORE UPDATE OR DELETE ON public.t100_guardian_account_enrollments
FOR EACH ROW EXECUTE FUNCTION public.t100_guardian_enrollment_no_mutation();

-- NO MEXC actions, NO account keys, NO secrets, NO database role grants.
