# T100/V40 — operator-reported real MEXC pilot evidence (2026-11-04)

**Scope:** one BTC_USDT contract, dedicated MEXC API-only pilot subaccount, isolated 1x; NOT production four-slot V40 or CROSS-margin certification.

**Provenance:** redacted terminal outputs supplied by operator in ChatGPT on 2026-11-04. This note records those outputs; it is NOT an independent replay of signed exchange responses, cryptographic attestation, or review of local pilot code/journal. Do not infer account identity/order IDs from this document. No new exchange requests or orders were made to produce this note.

## Reported order/position and fee evidence

- Preflight observed flat account twice before one market short.
- Quoted reference notional for one contract: 7.83999 USDT (not a hard fill-price bound).
- Entry ACK true; position observed; full-position MARKET SL observed active via signed GET; close ACK true; flatness after close and zero remaining native stops reported.
- Subsequent GET-only order audit: entry order FILLED, one-contract SHORT; close order FILLED, one-contract CLOSE; 1 execution each.
- Entry average fill: 78,391.2 USDT/BTC; close average fill: 78,391.3 USDT/BTC.
- Entry fee: 0.006271296 USDT; close fee: 0.006271304 USDT; reported close realized P&L: -0.00001 USDT.
- Simple summed result excluding funding/other unverified charges: -0.0125526 USDT.
- Read-only audit found account flat in two inventory GET snapshots.

## Reported finished native-stop history (GET-only)

- First entry fill: **2026-11-04 10:06:48.000 UTC**.
- One matching finished position-native stop record.
- Whole-position MARKET SL semantics retained in returned history: true.
- Stop createTime: **2026-11-04 10:06:49.000 UTC**.
- Stop updateTime: **2026-11-04 10:06:51.000 UTC**.
- Terminal state: **CANCELED**, isFinished=1; triggerSide: **NOT_TRIGGERED**.
- First-fill-to-stop-**creation** timestamp delta: **1,000 ms**.
- History scan ended at a short page and agreed across two reads. A short page does NOT independently certify global history completeness.
- The report labels stop completion **CANCELED_AFTER_CLOSE_CANDIDATE**. This is not independently established chronological cancellation after the closing fill, because this audit did not provide the close-fill timestamp alongside stop updateTime.

## Interpretation and limits

- This is valuable first real venue evidence for **accepted position-native full-position MARKET SL placement plus contemporaneous GET observation** and eventual canceled final stop state under the tested account/contract.
- **It does not prove the stop became active 1,000 ms after entry.** createTime is not independent active-protection timestamp; API/network/GET delays and position-unprotected interval remain unmeasured.
- There is **no evidence of a stop-loss trigger or market execution**, nor of successful stop amendment.
- POST timeout, crash, network partition, partial fills, independent close/watchdog, quantity changes, cross margin, account risk/collateral, four-slot coordination, funding and Render runtime remain **UNVERIFIED**.
- A successful isolated pilot is not authorization to deploy, change credentials or grant full-account live trading. Keep entry_allowed=false, live_authorization=false, production POST disabled.
- The earlier initial stop-history GET failed with application code 602, which was resolved after including request query parameters in the MEXC signature; final audit succeeded without mutation.

## Next controlled verification gate

1. Preserve/redact the local pilot journals and order/deal/stop evidence. Do not publish keys, raw private payloads, order IDs, account fingerprints or subaccount names.
2. Independently review a scoped and durable watchdog/emergency-close workflow, including the still-unbounded entry-to-active-stop gap, unknown POST outcomes, and worst-case margin/liquidation behavior.
3. Only after separate scoped approval, attempt one supervised tighter-position-stop amendment with GET identity and quantity verification, then a separately approved stop-trigger/fill experiment.
4. Keep all production gates closed pending documented tests and review.
