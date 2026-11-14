# V40 frozen four-slot exit batch integration — 2026-11-14

Status: **unreleased, pure policy only; not live execution**.

The existing single-position `t100_v40_unreleased_exit_policy.py`
validates the original SHORT SL75 / arm after +14% favorable move /
trail by 0.5 percentage points. This PR adds the missing per-candle
four-slot composition `t100_v40_unreleased_portfolio_exit_batch.py`.

## Exact behavior

- Requires one to four distinct position IDs and symbols.
- Requires a strictly complete, consecutive 30m candle and a real
  positive tick for **every** active slot. A missing or stale candle
  invalidates the whole batch; no partial work can reach any venue.
- For every slot the preexisting stop is checked against the adverse
  bar HIGH first. Any stop touch is marked **GET-only native-close
  reconciliation required**, never presumed a filled sell or proven PnL.
- Only when the previous stop has not been touched, updates the
  observed best favorable LOW for that slot. At 14% favorable, proposes
  the 0.5pp trailing stop level; the candidate never becomes active
  until independently signed exchange stop amend/readback and durable
  optimistic-concurrency checkpoint.
- Keeps unaffected surviving positions separate, including a possible
  stop trigger on one slot while three continue protected. A close
  does not release a slot until independent native-order finality
  evidence, fills and account inventory prove it.
- Never sends an MEXC POST, changes an account setting, reads/writes DB,
  acknowledges an outbox, or issues a live permit.

## Unresolved release dependencies

1. Historical archive `v40_parity_for_live.zip` was independently
   SHA-256-verified and contains the 146 candidates, 7,154 prior bars,
   108 scenario entries, gate decisions and metrics. It **does not**
   contain exact complete candle-by-candle portfolio exits, fill
   liquidity, funding and full equity transition ledger. Therefore
   full 7.238406x equity replay cannot be certified from this ZIP alone.
2. The existing live worker is deliberately a one-position $12.50
   canary; it cannot run this 1–4 slot projection as an autonomous
   four-slot portfolio without a new separately certified live
   reservation/order/fill/recovery coordinator and fresh account-wide
   reads on each transition.
3. The new batch contains no exchange stop amendment adapter.
   Official MEXC docs show position TP/SL placement and existing order
   observation, but do not by themselves certify safe in-place edit or
   full-quantity survivor lifecycle. No guess about the endpoint
   can substitute for actual exact identity/readback verification.
4. MEXC maintenance margin and stressed liquidation tiers need
   actual account/contract GET verification; the research five-times
   shock proxy is not exchange leverage.
5. The independent guardian's scope 003 sticky critical latch remains
   active. It must not be reset automatically or replaced by a hidden
   fifth scope; the trader must not POST while it is latched.

## Audit commands

The GitHub Actions workflow runs four-slot exit batch tests alongside
frozen one-position stop priority, per-position native close with
surviving positions, and durable bar-cursor tests.

The PR deliberately leaves the live worker code and guardian deployment
unchanged.
