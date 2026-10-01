# Alpaca shadow authority — the custody world, fill models and cold start

**Status:** slice-4 design, current as of 2026-09-27. Shadow is an
independently activated simulation authority with no-submit ports and shared
simulated economics (`shadow_authority.py`, `shadow_broker.py`,
`shadow_activation.py`). PRD #2540 / #2547 removed its session journal,
receipt, paper-twin comparison and their CLI commands; they grant no
deployment permission and are not prerequisites.

## Worlds and paths

Custody is `shadow:<live_account_id>`, a reserved namespace beside `sim:`
(`account_authority.py`). It is an ordinary `ClerkSqliteRepository` account, so
it inherits the established-accounts registry, the recovery lock and the reset
registries unchanged — ruling R2: a second repository root would have needed a
parallel copy of all three.

| What | Where |
|---|---|
| Custody database, synthesized-order WAL | `accounts/alpaca/shadow:<live_account_id>/` — `clerk.db`, `simulated_orders.jsonl` |
| Activation fence | `accounts/shadow/` — `shadow_activation.jsonl` |
| Per-instance retained source bars | `accounts/alpaca/shadow-evidence:<strategy_instance_id>/` |

The ADR's "under `accounts/shadow/<live_account_id>/`" is satisfied in spirit by
the `accounts/shadow/` fence ledger, which scopes by row rather than
by directory; the custody database is at `accounts/alpaca/shadow:<id>/`. `shadow-evidence:` is deliberately not `shadow:`: an
evidence namespace is never a custody identity, and one function —
`account_authority.evidence_account_id_for` — chooses it for both the binding
authority and the replay proof.

`ShadowAccountReadPort` is a **composite** (ruling R3). Account, clock,
activities, assets, portfolio history and capabilities come from the live read
port; positions and orders come from the synthesized book, because the Clerk's
reconciliation sweep must reconcile the world it custodies and real positions
cannot share one reconciliation with synthesized fills. The consequence is worth
stating plainly: **a real position on the live account is invisible to the
shadow sweep and visible on the account card, which reads the broker directly.**

Two smaller wiring facts follow from the same posture. The shadow runtime
installs a null trade-updates evidence sink (ruling R15) — the live execution
stream is not shadow custody evidence, though the consumer still runs so the
stream-health gate samples the live websocket. And the Clerk-boundary liveness
recheck at ENTER now runs on the shadow authority as well as the real one: a
refusal the real Clerk would make has to appear in the paper twin too, or the
two stop reconciling trade-for-trade.

## Sharing the market-status connection with the Paper twin

The rehearsal keeps IBKR price bars and separate Alpaca custody/execution in
each worker. Alpaca may reject the second stock-data status socket with error
406, including across an owner's Paper and Live credentials
([vendor streaming contract](https://docs.alpaca.markets/us/docs/streaming-market-data)).
No additional data subscription is required by this implementation: the Paper
worker can set `ALPACA_MARKET_STATUS_UPSTREAM_URL` to the Shadow service's
`/api/brokers/alpaca/market-status-snapshot` endpoint. Both workers must share
the existing protected control-channel secret; no trading credential is sent
to this endpoint. The option is refused in Live mode.

The Paper worker polls that source instead of opening another status socket.
It still reads its own broker clock and uses its own trade-update stream.
Snapshots preserve original vendor halt/resume timestamps and retain known
halts across upstream reconnects. Missing, rejected, future-dated or stale
source evidence blocks new exposure; cached connection proof expires after
five seconds even if polling stalls. Only a status subscription or valid
status event can establish the vendor connection: connection greetings,
authentication errors and error 406 cannot briefly admit a run. Regression
coverage is in `tests/broker/alpaca/test_market_liveness.py`.

Shadow lifecycle projection and boot recovery use the same closed set of
SQLite-backed primary authorities as the other operator surfaces. The public
account route maps to its Shadow custody namespace for authority facts;
foreign accounts remain refused. Composed-runtime tests in
`tests/broker/v2panel/test_shadow_operator_surfaces.py` cover launch, stop,
resume, and recovery of a failed activation without replacing its binding.

## Fill models

The fill model is chosen by leg shape (ruling R4), and both models live in the
one canonical file `app/broker/alpaca/clerk/fill_models.py`:

- **`decision_bar_close`** — a leg without `extended_hours` fills at its bound
  decision bar's close, through the same `immediate_fill_price` the `sim:` world
  uses. A non-marketable regular limit cancels on the spot.
- **`limit_touch`** — an extended-session leg rests, and settles under
  `limit_touch_fill` against the bars its own instance retained after the
  decision bar. It cancels at the declared window's close for the decision's
  trading day — the instant the vendor would have cancelled a DAY extended
  order. A decision bar that closes outside the declared window cannot rest an
  order at all: the record would be incoherent, an order submitted at or after
  the instant it is recorded as cancelled.

Settlement is driven by **reads**, not by a background task (ruling R5): the
sweep's periodic order and position reads are the clock, and
`ShadowOrderBook._settle_locked` is the consumer of `limit_touch_fill`. A read
takes the transaction and appends only while a resting order exists; a book of
terminal orders costs a plain WAL read. With no touch, an order cancels only
once `now_ms >= cancel_at_ms + (decision_bar.end_ms - decision_bar.start_ms)` —
one bucket of slack, so the closing bucket has been retained before the book
concludes the order never touched. Once that slack has elapsed the cancel is
**final**: a bar retained afterwards that would have touched the limit does not
revive the order. That determinism is the point of R5 and it is the residual to
watch, because it is the shape of the #1921 feed-stall family.

`cancel` (ruling R6) marks a resting synthesized order cancelled and returns; on
a terminal order it is a no-op. The ADR's "no-op" means *no vendor call* — the
EXIT machine's cancel-and-prove still needs a terminal answer, and it gets one.

Provenance is durable in `SynthesizedAnchor` (`synthesized_orders.py`):
`fill_model`, `evidence_account_id`, `provider`, `bar_identity`, `bar_ref`,
`decision_bar_start_ms`, `decision_bar_end_ms`, `cancel_at_ms` for a resting
model, and `fill_bar_ref` once a bar produced the fill. A shadow fill is priced
only from its own instance's evidence: `ShadowOrderBook._evidence_namespace_for`
derives the expected `shadow-evidence:<sid>` from the order's own namespace, and
a bar retained by any other instance is refused.

## Cold start

`verify_shadow_namespace_empty` transfers ADR 0002 invariant 1 to the live
account: it holds no Clerk-minted order, ever. The check is bounded by what the
read port can see (ruling R9) — the newest page of the whole history plus the
newest page of open orders (both `limit=MAX_OPEN_ORDER_SNAPSHOT`, 500; only the
history page being full yields `SHADOW_NAMESPACE_UNPROVEN`) — and it insists on
the **live** read port, because handed the shadow port every category would
answer from the synthesized book and the check would pass vacuously on a
poisoned account.

Two refusals, both of which leave the live boot with no authority installed
rather than a permissive one:

- `SHADOW_NAMESPACE_POISONED` — any order whose `client_order_id` parses as a
  Clerk order ref already exists on the account.
- `SHADOW_NAMESPACE_UNPROVEN` — the history read reached the 500-row page
  boundary, so emptiness cannot be proven from one page. This is the sweep's own
  posture at the same boundary, and it is why a paginated order-history walk is
  a recorded follow-up: an account past 500 historical orders cannot shadow
  until one exists.

## Operator recipe

Configuration is the normal activation surface. The `activate` CLI is the
recovery fallback.

The shadow custody database these commands read and write lives on the
VM-local `alpaca-clerk-data` named volume the running worker mounts at
`/app/artifacts/alpaca_clerk` (see `compose.yaml`), not on the host tree at
`PythonDataService/artifacts/alpaca_clerk` — that host tree is mounted
read-only at `/app/alpaca_clerk_legacy` and normal runtime never reads
authority from it. Run the command below from the repo root (where
`compose.yaml` lives) inside a one-shot `python-service` container against
that same volume, the same pattern the
[SQLite Clerk recovery/cutover runbook](../runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md)
uses. A host-side `python -m scripts.manage_alpaca_shadow` invocation writes
`activate`'s fence into the unmounted legacy tree instead, and the worker
never sees it.

```bash
# Once per live account, before the first shadow boot.
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_alpaca_shadow --live-account-id <ACCOUNT> \
  --artifacts-root /app/artifacts/alpaca_clerk \
  activate
```

## Follow-ups (not this slice)

- **A paginated order-history walk.** Cold start proves an empty Clerk namespace
  from one 500-row page; an account past 500 historical orders refuses
  `SHADOW_NAMESPACE_UNPROVEN` and cannot shadow until the walk exists.
