from __future__ import annotations

# One canonical production strategy identifier shared by scanner, trader and reports.
CURRENT_STRATEGY_ID = "tp5_nostop_adv30_runner50_trail1_daily_core_persistence_skip_v2"

# Backward-compatible scanner aliases. These names historically described the
# report/trader exit rule, but scanner-side usage only controls admission.
LEGACY_SUBSCRIBER_V2_ID = "tp5_sl75_daily_core_persistence_skip_v2"
LEGACY_SUBSCRIBER_V1_ID = "tp5_sl75_daily_core_persistence_skip_v1"
LEGACY_DAILY_CORE_ID = "tp5_sl75_daily_core_skip_v1"
