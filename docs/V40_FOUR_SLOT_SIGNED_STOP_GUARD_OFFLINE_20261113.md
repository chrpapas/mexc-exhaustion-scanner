# Four-slot V40 live-protection audit — offline-only bridge (2026-11-13)

The original 7.238406× saved simulation can carry up to four simultaneously
owned short positions. Peak actual gross exposure/equity was 2.0901×.
The current dedicated Render guardian only recognizes exactly ONE position
with leverage 1×; promoting historical sizing without first extending its
provenance and native stop audit would declare correctly owned positions
"UNKNOWN OWNER", or fail to monitor all four.

This module accepts the independently bookended
`FourSlotSnapshotCoordinator` signed venue/ledger evidence and checks:

- 1 to 4 distinct position IDs, distinct symbols and native stop IDs.
- Matching identity and full volume across durable checkpoints,
  read-only recovery, signed MEXC positions and open MARKET SLs.
- The exact canonical venue digest used in the source bookend; callers
  cannot swap positions or stops after snapshot generation.
- Every position must be a live MEXC CROSS SHORT, with a positive
  observed integer leverage; it does **not approve** or change leverage.
- Every stop must be active/normal, MARKET, no-reverse, correct origin
  order, full-volume, and no higher than the durable expected stop.
- All closed/recovery data must have been independently verified;
  no unowned/manual positions may be adopted or counted.

Any mismatch halts the ENTIRE account. The result is nonauthorizing,
read-only and sends no MEXC order or DB mutation.

The risk model remains **separate and unfinished**. Confirmed native SL
presence is not proof of eventual execution, maintenance-tier, cross
collateral, mark-price liquidation or whole-history 7.24× viability.
No existing Render entrypoint imports this module; no guardian recovery
generation is created or reset by this PR.

Before real full V40 promotion, integrate the audited 0–4 slot guard with:
risk-tier/wallet checks and a fresh stop GET, durable bar cursors and actual
native stop amendment contract, engine order permits, dual-sided
reconciliation for partial exits and remaining contracts, and historical
margin/liquidation stress parity. The existing 12.50-USDT one-shot pilot
should never be mislabeled as historical V40.
