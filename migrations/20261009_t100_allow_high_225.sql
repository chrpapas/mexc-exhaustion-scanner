-- Extend the existing stage-tier constraint without changing any previously accepted tiers.
-- The frozen T100_225 strategy emits HIGH_225, while older schemas reject it.
DO $migration$
DECLARE
    existing_definition text;
    existing_expression text;
BEGIN
    SELECT pg_get_constraintdef(c.oid)
      INTO existing_definition
      FROM pg_constraint c
     WHERE c.conrelid = 't100_stage_state'::regclass
       AND c.conname = 't100_stage_state_t100_tier_check'
       AND c.contype = 'c';

    IF existing_definition IS NULL THEN
        RAISE EXCEPTION 'Expected t100_stage_state_t100_tier_check constraint not found';
    END IF;

    -- pg_get_constraintdef yields CHECK (<expression>); retain the entire old
    -- predicate and extend it with the new frozen tier.
    existing_expression := CASE WHEN left(existing_definition, 7) = 'CHECK (' AND right(existing_definition, 1) = ')' THEN substring(existing_definition FROM 8 FOR length(existing_definition) - 8) ELSE NULL END;
    IF existing_expression IS NULL THEN
        RAISE EXCEPTION 'Unexpected tier check definition: %', existing_definition;
    END IF;

    EXECUTE 'ALTER TABLE t100_stage_state DROP CONSTRAINT t100_stage_state_t100_tier_check';
    EXECUTE format(
        'ALTER TABLE t100_stage_state ADD CONSTRAINT t100_stage_state_t100_tier_check CHECK ((%s) OR t100_tier = %L)',
        existing_expression,
        'HIGH_225'
    );
END
$migration$;
