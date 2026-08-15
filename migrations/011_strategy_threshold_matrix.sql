-- v1.1.5: strategy-choice matrix across -100/-200/-300/-400 adverse thresholds.
-- Add intermediate adverse thresholds and backfill all first-breach timestamps over
-- the fixed 7-day analytics window so historical rows remain comparable.

ALTER TABLE shadow_trades
    ADD COLUMN IF NOT EXISTS adverse_200_breach_at timestamptz,
    ADD COLUMN IF NOT EXISTS adverse_300_breach_at timestamptz;

UPDATE shadow_trades st
SET isolated_100_breach_at = COALESCE(
        (
            SELECT min(c.open_time + interval '15 minutes')
            FROM candles c
            WHERE c.symbol = st.symbol
              AND c.interval = 'Min15'
              AND c.open_time >= st.confirmed_at - interval '15 minutes'
              AND c.open_time < st.confirmed_at + interval '168 hours'
              AND c.high >= st.entry_price * 2.0
        ),
        st.isolated_100_breach_at
    ),
    adverse_200_breach_at = COALESCE(
        (
            SELECT min(c.open_time + interval '15 minutes')
            FROM candles c
            WHERE c.symbol = st.symbol
              AND c.interval = 'Min15'
              AND c.open_time >= st.confirmed_at - interval '15 minutes'
              AND c.open_time < st.confirmed_at + interval '168 hours'
              AND c.high >= st.entry_price * 3.0
        ),
        st.adverse_200_breach_at
    ),
    adverse_300_breach_at = COALESCE(
        (
            SELECT min(c.open_time + interval '15 minutes')
            FROM candles c
            WHERE c.symbol = st.symbol
              AND c.interval = 'Min15'
              AND c.open_time >= st.confirmed_at - interval '15 minutes'
              AND c.open_time < st.confirmed_at + interval '168 hours'
              AND c.high >= st.entry_price * 4.0
        ),
        st.adverse_300_breach_at
    ),
    cross_400_breach_at = COALESCE(
        (
            SELECT min(c.open_time + interval '15 minutes')
            FROM candles c
            WHERE c.symbol = st.symbol
              AND c.interval = 'Min15'
              AND c.open_time >= st.confirmed_at - interval '15 minutes'
              AND c.open_time < st.confirmed_at + interval '168 hours'
              AND c.high >= st.entry_price * 5.0
        ),
        st.cross_400_breach_at
    );
