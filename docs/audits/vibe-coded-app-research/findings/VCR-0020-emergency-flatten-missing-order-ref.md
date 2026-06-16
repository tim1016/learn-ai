---
id: VCR-0020
severity: P0
status: open
area: halt-state-machine
canonical_file: PythonDataService/app/engine/live/run.py::cmd_emergency_flatten
reference: ADR 0008 § 1 (order_ref invariant) + PRD §12.6 (#502 emergency flatten)
first_seen: 2026-06-16
last_seen: 2026-06-16
lens: halt-pause-stop-flatten-poison
dedupe_with_F: VCR-0009 (cancel-first asymmetry — separate sibling bug)
confidence: high
---

## What

`cmd_emergency_flatten` constructs `IbkrOrderSpec` objects for the
liquidation loop but **does not stamp `order_ref` on the spec**. Phase 5A
(`aae1cf2c`) added a hard invariant in `place_paper_order`:

```
OrderRefusedError: ADR 0008: place_paper_order requires spec.order_ref.
Build a deterministic {bot_order_namespace}:{intent_id} token via
app.engine.live.order_identity.build_order_ref and stamp it on the
IbkrOrderSpec before calling this function.
```

So `live run emergency-flatten --confirm --account ...` **fails on every
single liquidation attempt** in production. The CLI's documented "panic"
surface — the one explicit fallback when the bot is poisoned and an
operator needs to flatten the account — is broken since Phase 5A landed.

Observed live during the 2026-06-16 HITL VCR-0002 receipt run when the
operator needed to clean up the net-short from VCR-0019: the
`emergency-flatten` CLI ran cancel-first successfully, then failed on the
first place_order with the OrderRefusedError above. Manual cleanup
fell back to a hand-crafted `POST /api/broker/orders` with a manually-
typed `order_ref`.

## Where

- `PythonDataService/app/engine/live/run.py::cmd_emergency_flatten` —
  inside `_flatten()`, the `IbkrOrderSpec` construction at the
  `client_order_id="emergency-flatten-..."` line does NOT call
  `build_order_ref(...)`. Compare to `_recovery_flatten` (run.py:413-507)
  which **also doesn't stamp order_ref** but happens to work in test
  fixtures because those fixtures use `FakeBroker` with
  `requires_durable_submit=False`.
- `PythonDataService/app/broker/ibkr/orders.py::place_paper_order` —
  the Phase 5A invariant that rejects empty `spec.order_ref`.

## Why this severity

P0 because this is the **operator's documented escape hatch when
everything else is on fire**. The bot can be poisoned, the engine can
refuse to start, the cockpit's Stop/Pause/Resume can all be unavailable —
and the contractually-promised cleanup path is `live run emergency-flatten
--confirm --account <DU>`. Today that contractually-promised path
**always exits 3** in production with `OrderRefusedError`. The operator
either has to hand-craft API calls (what we did) or use TWS manually —
both fragile under stress.

Combined with VCR-0019 (recovery_flatten putting the account net-short
on shutdown), the bot today has a meaningful chance of leaving a position
open at the broker that the operator cannot quickly clean up via the
documented surface.

## Trading impact

- **Today (paper):** the receipt-run cleanup needed manual `/api/broker/orders`
  calls with a hand-typed `order_ref` because the CLI rejected itself.
- **Tomorrow (live mode):** if a poisoned bot leaves a position and the
  operator hits the panic button, the panic button doesn't work. They
  have to either know to invoke the raw broker endpoint with a manually-
  constructed order_ref token (most operators won't), or open TWS and
  trade manually (slow, error-prone, and breaks the bot's audit
  invariants if the operator misses a sibling order).

## Reproduction

```bash
# 1. Have a non-empty paper position on DUM* (any symbol).
# 2. Run the documented panic surface:
cd PythonDataService
IBKR_HOST=127.0.0.1 IBKR_PORT=4002 IBKR_MODE=paper IBKR_READONLY=false \
  .venv/bin/python -m app.engine.live.run emergency-flatten \
    --account DUM284968 --confirm --run-dir <any-existing-run-dir>
# 3. Observe:
#    [EMERGENCY-FLATTEN] runtime error: OrderRefusedError: ADR 0008:
#      place_paper_order requires spec.order_ref.
#    [EMERGENCY-FLATTEN] FAILURE: OrderRefusedError: ADR 0008: ...
# 4. Confirm the position at IBKR is unchanged (no liquidation orders
#    actually fired).
```

## Suggested resolution (NOT auto-applied)

1. **In `cmd_emergency_flatten._flatten`**: for each symbol being
   liquidated, construct a deterministic `order_ref` BEFORE calling
   `broker.place_order(spec)`. The token format already used elsewhere is
   `{bot_order_namespace}:{intent_id}`. For emergency flatten where there
   is no `bot_order_namespace` (the path runs outside an engine session),
   use a synthetic namespace like
   `learn-ai/emergency-flatten/{strategy_instance_id_or_account}/v1` and
   mint a one-shot `intent_id` per liquidation. The 60-character cap from
   VCR-0002's receipt still holds.
2. **In `_recovery_flatten`** (same gap): same fix. Make the order_ref
   stamping the responsibility of the caller, not the broker adapter, so
   the structural invariant is enforced at the source.
3. **Test**: an integration test against a `requires_durable_submit=True`
   broker that runs `cmd_emergency_flatten._flatten` end-to-end and
   asserts the order_ref on each placed spec matches the expected
   pattern. Without that, the bug silently re-enters on the next time
   the order spec gets refactored.
4. **CLI smoke test**: run the documented panic CLI against a
   `FakeBroker` that declares `requires_durable_submit=True` (see
   `tests/engine/live/test_intent_identity_wiring.py::_RealBrokerFake`)
   and confirm exit code 0 with a synthetic position seeded.

## Provenance of the finding

Live observation during the 2026-06-16 HITL VCR-0002 Acceptance Gate #2
receipt run cleanup phase. After VCR-0019 fired (recovery_flatten put
the account net-short), the operator invoked the documented
emergency-flatten CLI to clean up and observed
`OrderRefusedError: ADR 0008: place_paper_order requires spec.order_ref`
on every liquidation attempt. The Phase 5A invariant at
`place_paper_order` is doing the right thing; the calling site never
got the wire-through.
