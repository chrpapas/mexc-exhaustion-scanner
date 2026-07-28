CREATE TABLE IF NOT EXISTS contracts (
    symbol text PRIMARY KEY,
    base_coin text NOT NULL,
    quote_coin text NOT NULL,
    settle_coin text NOT NULL,
    state integer NOT NULL,
    is_hidden boolean NOT NULL,
    api_allowed boolean NOT NULL,
    metadata jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS ticker_snapshots (
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    observed_at timestamptz NOT NULL,
    last_price double precision NOT NULL,
    bid1 double precision,
    ask1 double precision,
    spread_pct double precision,
    amount24 double precision NOT NULL,
    volume24 double precision NOT NULL,
    hold_vol double precision,
    low24 double precision,
    high24 double precision,
    rise_fall_rate double precision NOT NULL,
    index_price double precision,
    fair_price double precision,
    funding_rate double precision,
    PRIMARY KEY (symbol, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_ticker_snapshots_time ON ticker_snapshots(observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_ticker_snapshots_symbol_time ON ticker_snapshots(symbol, observed_at DESC);

CREATE TABLE IF NOT EXISTS candles (
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    interval text NOT NULL,
    open_time timestamptz NOT NULL,
    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume double precision NOT NULL,
    amount double precision NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);
CREATE INDEX IF NOT EXISTS ix_candles_symbol_interval_time
    ON candles(symbol, interval, open_time DESC);

CREATE TABLE IF NOT EXISTS funding_rates (
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    settle_time timestamptz NOT NULL,
    funding_rate double precision NOT NULL,
    PRIMARY KEY (symbol, settle_time)
);

CREATE TABLE IF NOT EXISTS run_signals (
    id bigserial PRIMARY KEY,
    symbol text NOT NULL REFERENCES contracts(symbol) ON DELETE CASCADE,
    signaled_at timestamptz NOT NULL,
    level text NOT NULL CHECK (level IN ('watch', 'candidate')),
    score integer NOT NULL,
    features jsonb NOT NULL,
    reasons jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (symbol, signaled_at, level)
);
CREATE INDEX IF NOT EXISTS ix_run_signals_symbol_time ON run_signals(symbol, signaled_at DESC);

CREATE TABLE IF NOT EXISTS worker_heartbeat (
    worker_name text PRIMARY KEY,
    last_seen_at timestamptz NOT NULL,
    status jsonb NOT NULL
);
