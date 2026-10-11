-- Extend the existing position-tier constraint without changing legacy valid tiers.
-- Executed transactionally by app.db.Database.migrate().
DO $migration$
DECLARE
    old_definition text;
    old_expression text;
BEGIN
    SELECT pg_get_constraintdef(c.oid)
      INTO old_definition
      FROM pg_constraint c
     WHERE c.conrelid = 't100_positions'::regclass
       AND c.conname = 't100_positions_tier_check'
       AND c.contype = 'c';

    IF old_definition IS NULL OR left(old_definition, 7) <> 'CHECK (' THEN
        RAISE EXCEPTION 'Expected t100_positions_tier_check CHECK constraint not found';
    END IF;

    -- Keep the original predicate intact; allow only the additional HIGH_225 tier.
    old_expression := substring(old_definition FROM 7);
    EXECUTE 'ALTER TABLE t100_positions DROP CONSTRAINT t100_positions_tier_check';
    EXECUTE format(
        'ALTER TABLE t100_positions ADD CONSTRAINT t100_positions_tier_check CHECK ((%s) OR (tier = %L))',
        old_expression, 'HIGH_225'
    );
END
$migration$;
