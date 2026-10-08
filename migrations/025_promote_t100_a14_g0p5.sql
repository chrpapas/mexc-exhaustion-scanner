-- v1.5.0: promote certified A14/G0.5 exit geometry in paper mode.
-- Existing open positions retain their stored 10%/1pp exit parameters.
-- New positions are stamped 14% activation / 0.5pp gap by application code.

WITH promoted AS (
    UPDATE t100_runtime
    SET strategy_id='t100_200_stage2_p15_a4_sl75_trail14_gap0p5_v2',
        run_id='t100_200_stage2_p15_a4_sl75_trail14_gap0p5_shadow_v2',
        updated_at=now()
    WHERE singleton=true
      AND strategy_id='t100_200_stage2_p15_a4_sl75_trail10_gap1_v1'
    RETURNING 1
)
INSERT INTO t100_events(event_type,payload)
SELECT
    'strategy_promotion',
    jsonb_build_object(
        'from_strategy','t100_200_stage2_p15_a4_sl75_trail10_gap1_v1',
        'to_strategy','t100_200_stage2_p15_a4_sl75_trail14_gap0p5_v2',
        'mode','paper',
        'transition','existing positions retain stored exit parameters; new positions use A14/G0.5'
    )
FROM promoted;
