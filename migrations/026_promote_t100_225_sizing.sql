-- v1.6.0: promote certified T100_225 sizing in paper mode.
-- Existing open positions keep their original fixed notional.
-- Legacy pending HIGH_200 signals remain supported at 50%; new HIGH_225 signals use 56.25%.

WITH promoted AS (
    UPDATE t100_runtime
    SET strategy_id='t100_225_stage2_p15_a4_sl75_trail14_gap0p5_v3',
        run_id='t100_225_stage2_p15_a4_sl75_trail14_gap0p5_shadow_v3',
        updated_at=now()
    WHERE singleton=true
      AND strategy_id='t100_200_stage2_p15_a4_sl75_trail14_gap0p5_v2'
    RETURNING 1
)
INSERT INTO t100_events(event_type,payload)
SELECT
    'strategy_promotion',
    jsonb_build_object(
        'from_strategy','t100_200_stage2_p15_a4_sl75_trail14_gap0p5_v2',
        'to_strategy','t100_225_stage2_p15_a4_sl75_trail14_gap0p5_v3',
        'mode','paper',
        'change','HIGH sizing 50% -> 56.25% of current equity',
        'transition','existing positions keep fixed notional; legacy pending HIGH_200 stays 50%; new HIGH_225 uses 56.25%'
    )
FROM promoted;
