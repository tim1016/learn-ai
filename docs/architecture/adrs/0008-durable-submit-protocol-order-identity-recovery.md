# ADR 0008 — Durable submit protocol & order-identity recovery: a run-scoped WAL, `order_ref` ownership, and uncertain-ack semantics

**Status:** Accepted 2026-06-04
**Decision drivers:** same-account relaunch poisoning (commits a884ad5, PR #443, #444); the `live-{order_id}` ownership bug class; persistent IBKR paper bot work (`docs/ibkr-paper-deployment-plan.md` § 16.4).
**Related:** ADR 0001 (JSON+Parquet substrate — reaffirmed here), ADR 0005 (engine-authored readiness / two-altitude broker ownership), `CONTEXT.md` (identity ladder, owned-orphan vs outside-mutation, submit-uncertain halt, uniform ownership ladder).

## Context

Order ownership in the live paper runtime was keyed on `live-{order_id}`, where `order_id` is `LivePortfolio._next_id()` — a per-run sequence that **resets to 1 each run**. So `live-1` in run A and `live-1` in run B collide, and IBKR's `order_id` is itself session-ephemeral. Ownership-by-`order_id` is structurally unable to survive a restart, which is the root of the repeated same-account relaunch-poisoning fixes.

Three further gaps compounded it:

1. **No durable record of an in-flight submit.** Orders went strategy → portfolio → broker with only an in-memory `_order_meta` dict. A crash between `placeOrder` and the next artifact flush left no durable evidence the order was attempted. `live_state.json` flushes per-bar; the submit happens sub-bar.
2. **Flatten paths bypassed identity entirely.** `recovery-flatten-*`, `emergency-flatten-*`, and shutdown-flatten orders carried ad-hoc `client_order_id`s matching no ownership scheme — the literal fills that poisoned relaunch.
3. **`ACK_FAILED` had no defined semantics.** A `placeOrder` exception or ack timeout is ambiguous: the order may or may not have reached IBKR. IBKR does **not** dedupe by `orderRef`, so a blind retry can double a real position.

`LiveStateEnvelope` already carried unpopulated placeholder fields (`pending_intents`, `submitted_orders`, `known_perm_ids`, `known_exec_ids`) and `cold_start_reconciler.py` already had a 7-step protocol against two unimplemented broker stubs — the scaffolding existed but the durable submit semantics did not.

The question was how to make order ownership survive process death without introducing a relational write store (which ADR-0001 deliberately deferred behind named triggers).

## Decision

**Adopt a narrow, run-scoped write-ahead log as a submit-lifecycle state machine, make `order_ref` the single ownership proof, and define `ACK_FAILED` as *uncertain* — never *failed*. No new substrate.**

### 1. Identity ladder, `order_ref` as ownership proof
`bot_order_namespace = learn-ai/{strategy_instance_id}/v1`; `order_ref = {bot_order_namespace}:{intent_id}` is written to IBKR's `order.orderRef` before any side effect and echoed back on open-order/execution callbacks. `intent_id` is a base64url-encoded `uuid4` (22 chars). `order_ref` length is **bounded, not assumed**: the fixed overhead is 35 chars (`learn-ai/`=9, `/v1`=3, `:`=1, `intent_id`=22) and `strategy_instance_id` may be up to 128, so once the IBKR `orderRef` cap `C` is verified (blocking; truncation is silent), Module A fails closed above `C` and a broker-owned instance must satisfy `len(strategy_instance_id) ≤ C − 35`. `C` is intentionally unset until the paper-receipt gate verifies the actual echoed cap; activation stays disabled while `C` is unset. Ownership is decided **only** by, in order: (1) the `order_ref` namespace, parsed on the **final** `:` and compared by **exact equality** (never `startswith` — `learn-ai/foo/v10:…` must not match `learn-ai/foo/v1`) against this instance's allowed-namespace set (one element, or `/v1`+`/v2` during a dual-read window), (2) known `intent_id`, (3) known `perm_id`, (4) known `exec_id`. `order_id` alone never proves ownership. `intent_kind`/`reason` are human provenance, never identity. `live-{order_id}`, `recovery-flatten-*`, and `emergency-flatten-*` are retired as identity mechanisms.

### 2. The intent ledger is a logical view, not a store
Its system of record is the run-scoped WAL folded over `live_state.json`'s `submitted_orders` (re-keyed by `intent_id`). An `intent_ledger.py` may hold the pure fold helpers but persists nothing of its own. **ADR-0001's substrate is unchanged.**

### 3. Run-scoped WAL as a submit-lifecycle state machine
`run_dir/intent_events.jsonl`, scoped to the submit critical section only:
- `PENDING_INTENT` — appended **and fsynced before `placeOrder`**.
- `SUBMITTED` — after a clean ack (carries `order_ref`, `order_id`, `perm_id`).
- `ACK_FAILED_UNCERTAIN` — on `placeOrder` raise or ack timeout (phase `placeOrder_uncertain`).
- In-session resolution of an uncertain submit yields `SUBMITTED_RECOVERED`, `INTENT_NOT_ACCEPTED`, or `SUBMIT_UNCERTAIN_HALTED`.
- `ADOPTED_BROKER_ORDER` — on cold-start adoption of an owned orphan.

Every event carries a per-run, strictly-monotonic `seq` (the fold cursor — see §5). One JSON line per event, fsynced before return. **Read contract:** only a single *trailing* unterminated line is tolerated (the fsync-before-`placeOrder` contract proves no side effect occurred for it); any other malformation — a torn line with complete lines after it, a parse failure on a terminated line — is corruption that **poisons**, and a complete un-acked `PENDING_INTENT` is resolved against the broker, never dropped. This is **not** a broad event-sourced replacement for live state; it covers exactly the durability gap between `placeOrder` and the next projection flush.

### 4. `ACK_FAILED` is uncertain, not failed
On `ACK_FAILED_UNCERTAIN` the engine **stops submitting new orders** and runs a defined procedure: wait a bounded `SETTLE_DELAY_S`, then probe the broker by `order_ref` with the namespace-scoped calls. The probe yields a three-way discriminator (a pure input to the state machine): `PRESENT` (any open/completed order or execution carries our `order_ref`) → adopt (`SUBMITTED_RECOVERED`); `PROVABLY_ABSENT` (defined as *both probe calls returned without error AND neither carries our `order_ref`*) → safe to retry **at most once, reusing the same `intent_id`/`order_ref`** (`INTENT_NOT_ACCEPTED`, `RETRY_CAP = 1`; a second uncertain → halt); `NOT_PROVABLE` (unreachable / probe error / ambiguous) → halt (`SUBMIT_UNCERTAIN_HALTED`). Halt is the default under any uncertainty. Blind retry, and any second retry, are banned.

### 5. Cold-start / resume reconciliation
Read `live_state.json`; replay this run's WAL tail after **`last_intent_wal_seq`** (the WAL `seq` cursor — never `last_artifact_flush_ms`, a wall-clock value that can collide/drift/reorder around fsync; `last_artifact_flush_ms` survives only as the coarse `reqExecutions` `since` hint, backstopped by `exec_id` dedupe); additionally read **exactly the immediately-prior run's** (discoverable via the run ledger / process registry; see Consequences) un-acked `PENDING_INTENT` tail and its emergency-flatten audit artifact (if any), resolving each by `order_ref` (closing the in-flight double-submit window). Reconcile projected ownership against IBKR by `order_ref` first, then `perm_id`, never `order_id`. Verdict:
- broker matches projection / WAL → continue;
- broker has an order whose parsed `order_ref` namespace **exactly equals ours** but whose intent is unknown → **owned orphan: adopt** (bounded adoption, persist projection before new submits; pause if exposure is ambiguous);
- broker has an order with **unknown namespace / no `order_ref` / foreign `perm_id`** → **outside mutation: poison/refuse.**

### 6. Uniform ladder for flatten paths
*All* order paths — strategy submit and every flatten variant (recovery, shutdown, force-flat, emergency) — mint `intent_id` and stamp `order_ref`. WAL writing is gated on single-writer ownership, not on the order kind: only **in-process, run-owned** paths append to `intent_events.jsonl`. The **out-of-process emergency-flatten** (engine dead, no safe concurrent WAL writer) does not write the live WAL; it stamps `order_ref` and writes a separate `emergency_flatten_audit.jsonl`, which a later cold-start discovers and adopts by namespace.

### 7. `/v1` is the `order_ref` wire-format version
It versions only the `order_ref` encoding — not strategy, config, spec, or model. A `/v2` bump requires an ADR/migration note **and dual-read ownership** (recognize both versions as owned until every prior-version order is closed).

### 8. Cutover is forced-flat
At the activation point, flatten + cancel + archive old `live_state.json` for every managed instance and start fresh under the new scheme. No old→new dual-read bridge code.

## Consequences

**Positive:**
- Ownership survives process death: `order_ref` is durable, namespace-scoped, and broker-echoed. The relaunch-poisoning bug class is closed at the root rather than per-prefix-patched.
- No new substrate. The WAL is an append-only JSONL artifact (cheap to fsync; one append per submit), naturally hashable into the daily manifest like other artifacts — ADR-0001 holds.
- A crash anywhere in the submit critical section is recoverable to a defined verdict (continue / adopt / poison), with no path that silently double-submits.
- Flatten orders stop being a special case; one ladder, one ownership rule.
- The forced-flat cutover deletes an entire class of transitional dual-read code that would otherwise need writing, testing, and later removal.

**Negative:**
- Cold-start now reads one prior run's WAL tail — a targeted cross-run artifact read that stretches "run-scoped." Bounded to a single prior `run_dir` (discoverable via the run ledger / process registry), reading only un-acked `PENDING_INTENT`s. Accepted: it's the only option that closes the in-flight double-submit window.
- The submit path gains an fsync before every `placeOrder`. Negligible for a 15-min-bar strategy's order rate; would warrant revisiting only for a high-frequency strategy.
- The forced-flat cutover costs a maintenance window and resets cross-run continuity. Acceptable for a paper research bot.
- An out-of-process emergency-flatten can place an order the live engine never WALs; it is recovered only at the next cold-start via namespace adoption, not in real time. Accepted: emergency-flatten is the engine-dead panic path by definition.

**Substrate-trigger guard (unchanged from ADR-0001):** a SQLite/Postgres write store enters only when a named ADR-0001 trigger fires (concurrent multi-consumer load, hot cross-run analytics, authenticated multi-operator audit). A per-order relational-shaped table is exactly that trigger and is explicitly **not** created here — the ledger stays a logical view.

## References

- `PythonDataService/app/engine/live/cold_start_reconciler.py` — resume protocol (7-step; broker stubs whose backing IBKR calls are a **blocking paper-receipt spike**, not an assumption — filled prior-run orders are absent from `reqOpenOrders`, and `orderRef` lives on `Order` not `Execution`; the Resolution-2 rule is *act only on namespace-matched orders*, independent of fetch primitive).
- `PythonDataService/app/engine/live/live_state_sidecar.py` — projection envelope (`submitted_orders` re-keyed by `intent_id`; WAL-fold cursor `last_intent_wal_seq`; `last_artifact_flush_ms` demoted to diagnostic + `reqExecutions` `since` hint).
- `PythonDataService/app/engine/live/halt.py`, `live_engine.py` — ownership / outside-mutation checks migrating off `live-{order_id}` onto the `order_ref` ladder.
- `PythonDataService/app/broker/ibkr/orders.py` — submit path; `order.orderRef` write and `OrderEvent.order_ref` capture (migration stages 11–12).
- `PythonDataService/app/engine/live/run.py` — `_recovery_flatten`, `cmd_emergency_flatten`; `bot_order_namespace` construction (run.py:622).
- `CONTEXT.md` — identity ladder, owned-orphan vs outside-mutation, submit-uncertain halt, uniform ownership ladder.
- ADR 0001 — substrate decision this ADR reaffirms.
