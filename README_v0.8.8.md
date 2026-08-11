# v0.8.8 — performance by execution risk

Adds full STANDARD vs HIGH+EXTREME performance statistics to Discord performance reports.

For each 24h, 48h and 72h horizon the report now includes:
- sample count
- win rate
- average short return
- summed short return

No database migration is required.

Replace:
- app/performance.py
- app/notifier.py
- pyproject.toml
