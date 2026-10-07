-- v1.4.0: frozen T100_200 Stage2 strategy shadow-production state.
-- The legacy Min15 scanner/trader tables remain untouched for research continuity.

CREATE TABLE IF NOT EXISTS t100_scanner_state (
    symbol text PRIMARY KEY REFERENCES contracts(symbol) ON DELETE CASCADE,
    started_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('run_watch','exhaustion_watch','breakdown_watch','confirmed_short')),
    peak_price double precision NOT NULL CHECK (peak_price > 0),
    peak_at timestamptz NOT NULL,
    last_run_score integer NOT NULL DEFAULT 0,
    last_exhaustion_score integer NOT NULL DEFAULT 0,
    broken_level double precision,
    breakdown_at timestamptz,
    breakdown_atr7 double precision,
    confirmed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS t100_stage_state (
    symbol text PRIMARY KEY REFERENCES contracts(symbol) ON DELETE CASCADE,
    episode_seq bigint NOT NULL DEFAULT 1,
    previous_p2_at timestamptz NOT NULL,
    previous_feature_at timestamptz NOT NULL,
    stage_no integer NOT NULL CHECK (stage_no >= 1),
    stage1_p2_at timestamptz NOT NULL,
    stage1_feature_at timestamptz NOT NULL,
    stage1_entry_price double precision NOT NULL CHECK (stage1_entry_price > 0),
    stage1_return_24h double precision,
    stage1_atr7_pct double precision,
    stage1_strict365 boolean NOT NULL DEFAULT false,
    stage1_eligible boolean NOT NULL DEFAULT false,
    t100_tier text CHECK (t100_tier IS NULL OR t100_tier IN ('LOW_100','HIGH_200')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS t100_p2_signals (
    id bigserial PRIMARY KEY,
    strategy_id text NOT NULL,
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    p2_at timestamptz NOT NULL,
    feature_at timestamptz NOT NULL,
    entry_price double precision NOT NULL CHECK (entry_price > 0),
    scanner_risk_tier text NOT NULL CHECK (scanner_risk_tier IN ('standard','high_risk')),
    physical_episode_seq bigint NOT NULL,
    stage_no integer NOT NULL CHECK (stage_no >= 1),
    reset_reason text,
    stage1_return_24h double precision,
    stage1_atr7_pct double precision,
    stage1_strict365 boolean NOT NULL DEFAULT false,
    stage1_eligible boolean NOT NULL DEFAULT false,
    t100_tier text CHECK (t100_tier IS NULL OR t100_tier IN ('LOW_100','HIGH_200')),
    eligible_stage2 boolean NOT NULL DEFAULT false,
    trade_decision text,
    trade_decision_reason text,
    features jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(symbol, p2_at)
);
CREATE INDEX IF NOT EXISTS ix_t100_p2_stage2 ON t100_p2_signals(p2_at, id) WHERE eligible_stage2;
CREATE INDEX IF NOT EXISTS ix_t100_p2_symbol ON t100_p2_signals(symbol, p2_at DESC);

CREATE TABLE IF NOT EXISTS t100_runtime (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    strategy_id text NOT NULL,
    run_id text NOT NULL,
    starting_equity_usdt double precision NOT NULL CHECK (starting_equity_usdt > 0),
    realized_equity_usdt double precision NOT NULL,
    last_eval_at timestamptz,
    last_report_local_date date,
    started_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t100_positions (
    id bigserial PRIMARY KEY,
    signal_id bigint NOT NULL UNIQUE REFERENCES t100_p2_signals(id) ON DELETE RESTRICT,
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE RESTRICT,
    slot_no integer NOT NULL CHECK (slot_no BETWEEN 1 AND 4),
    tier text NOT NULL CHECK (tier IN ('LOW_100','HIGH_200')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    opened_at timestamptz NOT NULL,
    entry_price double precision NOT NULL CHECK (entry_price > 0),
    notional_usdt double precision NOT NULL CHECK (notional_usdt > 0),
    quantity double precision NOT NULL CHECK (quantity > 0),
    entry_fee_usdt double precision NOT NULL DEFAULT 0,
    entry_slippage_usdt double precision NOT NULL DEFAULT 0,
    stop_pct double precision NOT NULL DEFAULT 75,
    trail_activation_pct double precision NOT NULL DEFAULT 10,
    trail_gap_pct double precision NOT NULL DEFAULT 1,
    trail_active boolean NOT NULL DEFAULT false,
    best_profit_pct double precision NOT NULL DEFAULT 0,
    current_price double precision NOT NULL CHECK (current_price > 0),
    current_return_pct double precision NOT NULL DEFAULT 0,
    mae_pct double precision NOT NULL DEFAULT 0,
    mfe_pct double precision NOT NULL DEFAULT 0,
    funding_pnl_usdt double precision NOT NULL DEFAULT 0,
    closed_at timestamptz,
    exit_price double precision,
    exit_reason text,
    gross_pnl_usdt double precision,
    exit_fee_usdt double precision NOT NULL DEFAULT 0,
    exit_slippage_usdt double precision NOT NULL DEFAULT 0,
    net_pnl_usdt double precision,
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_t100_open_symbol ON t100_positions(symbol) WHERE status='open';
CREATE UNIQUE INDEX IF NOT EXISTS ux_t100_open_slot ON t100_positions(slot_no) WHERE status='open';
CREATE INDEX IF NOT EXISTS ix_t100_positions_status ON t100_positions(status, opened_at);

CREATE TABLE IF NOT EXISTS t100_funding_applied (
    position_id bigint NOT NULL REFERENCES t100_positions(id) ON DELETE CASCADE,
    settle_time timestamptz NOT NULL,
    funding_rate double precision NOT NULL,
    position_value_usdt double precision NOT NULL,
    pnl_usdt double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(position_id, settle_time)
);

CREATE TABLE IF NOT EXISTS t100_events (
    id bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    event_type text NOT NULL,
    symbol text,
    signal_id bigint,
    position_id bigint,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_t100_events_time ON t100_events(occurred_at DESC);
