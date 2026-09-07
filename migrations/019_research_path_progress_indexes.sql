-- v1.3.53: keep optional research-path catch-up bounded as 15m history grows.
-- The primary key is (episode_id, candle_close_at ASC); this explicit DESC index
-- matches the DISTINCT ON newest-row lookup used by sync_research_signal_paths.
CREATE INDEX IF NOT EXISTS ix_research_signal_path_episode_time_desc
    ON research_signal_path_15m(episode_id, candle_close_at DESC);

-- TP5 observations are sparse, so index only rows capable of ending no-timeout
-- path collection. This supports first-TP5 lookup without scanning all path rows.
CREATE INDEX IF NOT EXISTS ix_research_signal_path_target5_episode_time
    ON research_signal_path_15m(episode_id, candle_close_at)
    WHERE favorable_return_pct >= 0.05;
