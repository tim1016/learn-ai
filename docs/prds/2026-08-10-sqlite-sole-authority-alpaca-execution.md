# SQLite Sole-Authority for Alpaca Execution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This is a **stacked-PR AFK program**: read "AFK Execution Model" before touching any task.

**Goal:** Make the event-sourced SQLite Alpaca Clerk the sole internal authority for execution slices, fills, positions, P&L, bot attribution, account history, external-order observation, bot config, and decision receipts — with no JSONL / Postgres / process-registry / direct-Alpaca product fallback.

**Architecture:** A fresh authority generation on schema v8. Execution slices become first-class (real Alpaca `execution_id`, per-slice qty/price/time) captured from the `trade_updates` websocket instead of reconstructed from cumulative order snapshots. Execution provenance rides in fold-projection tables (`fills`-family) and `facts_json` — never as new hashed `custody_transitions` columns. Product projections reuse the canonical FIFO engine (`fifo_pnl.py`) and the incremental rollup cache. Every product surface (panel, catalog, chart, orders, one consolidated desk, account history) reads SQLite folds only; broker reads survive as reconciliation/diagnostic paths the Clerk itself calls.

**Tech Stack:** Python 3.11 / FastAPI / Pydantic v2 / sqlite3 (WAL, `synchronous=FULL`) / pandas / Angular 22 (signals, zoneless, Vitest) / .NET 10 transport (unchanged) / `gh` CLI for stacked PRs.

## Global Constraints

Every task's requirements implicitly include this section. Values copied verbatim from the repo rules.

- **Time is `int64 ms UTC` everywhere in flight/at rest/on the wire.** No ISO strings, `datetime`, or naive datetimes as wire/storage. All session structure (open/close, half-days, windows) derives from the canonical NYSE calendar module — no hardcoded `time(9,30)`/`time(16,0)`. (`.claude/rules/temporal-rigor.md`)
- **Numerical tolerances are explicit.** Accumulated P&L: `atol=1e-6, rtol=0`. Indicator/closed-form: `atol=1e-9, rtol=0`. Never `np.allclose` with defaults. Reuse the canonical FIFO (`app/broker/alpaca/clerk/fifo_pnl.py`); introduce **no** new P&L math. (`.claude/rules/numerical-rigor.md`)
- **Hash-chain rule (load-bearing).** `custody_transitions` columns are hashed by `hashchain.PAYLOAD_COLUMNS`. Adding a hash-participating column breaks verification of every existing row at boot AND breaks mirror rebuild (`writes.py` KeyErrors on old payloads). Therefore: **new execution/provenance data goes into fold tables + `facts_json` (bump `facts_schema_version`); new behavior on the log uses new `transition_kind` values only.** New kinds are additive-safe for existing rows and mirror replay; never rename/remove a kind that appears in historical rows.
- **Pinned-schema governance.** `sqlite/schema.py::SCHEMA_DDL` is byte-for-byte pinned to `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md §3` and enforced by `tests/broker/alpaca/clerk/sqlite/test_schema_parity.py`. Any schema change edits the doc, the DDL, the parity test, and bumps `SCHEMA_VERSION` in the same commit.
- **No fallback under active SQLite authority.** SQLite unavailable/incomplete ⇒ `unavailable` / hold / `503` — never legacy JSONL/Postgres/broker-direct product data.
- **Crown-jewel invariants (re-prove under every touching slice):** capture-before-contact; never fabricate a terminal outcome from one lost response (30s grace + `by_client_order_id`); namespace-attributed exposure that never nets from the raw account position; cancel-first / prove-terminal EXIT; live-idempotent websocket dedup.
- **Router freeze.** `app/routers/live_instances.py` (>1000 lines) is frozen: no net line increase; new behavior in services. New HTTP surface for this program lives in `app/routers/` modules that are transport-only + service-backed.
- **Frontend rules.** Standalone components, `OnPush`, signals, `input()/output()`, `resource()`/`rxResource()`, `@if/@for(track)`, `inject()`, no `ngClass/ngStyle`. Backend identifiers render through the shared `receiptLabel` pipe; opaque audit tokens (order/intent ids, hashes, refs) preserved verbatim; backend-authored prose unpiped. Timestamps render through the shared timestamp component (instants `local`, date-anchored `date-et`).
- **Lint + test at project scope before every push.** `ruff check PythonDataService/app/ PythonDataService/tests/`; `npx eslint Frontend/src/ --max-warnings 0`; `dotnet format podman.sln --verify-no-changes`. Never run large pytest suites **inside** `polygon-data-service` (cgroup OOM); use a sibling container from the same image with the secret env cleared, or scoped test selection.
- **Thermo gate.** One `thermo-nuclear-code-quality-review` round before the first push that opens each PR; fix every **major** finding before push (minor optional). One-shot per PR.
- **No new dependencies** without a stated alternative-considered justification.

---

## AFK Execution Model

This program runs **away-from-keyboard as a single pipeline of six stacked PRs**. Read this before any task.

### Stacked branch chain (base off `master`)

```
master
 └─ agent/sqlite-authority/s0-contract-fixtures      → PR (base: master)
     └─ agent/sqlite-authority/s1-execution-ledger   → PR (base: s0 branch)
         └─ agent/sqlite-authority/s2-projections    → PR (base: s1 branch)
             └─ agent/sqlite-authority/s3-consumers    → PR (base: s2 branch)
                 └─ agent/sqlite-authority/s4-desk      → PR (base: s3 branch)
                     └─ agent/sqlite-authority/s5-retire → PR (base: s4 branch)
```

- Each PR is created with `gh pr create --base <parent-branch> --head <this-branch>`. The AFK run **opens PRs; it never merges them.** The human reviews/merges the stack and, on merge of a base, GitHub auto-retargets its child to `master` (or the human rebases).
- Stacked PRs are **sequential by construction** (S1 needs S0's contract, etc.). Parallelism is *within* a slice and in *verification*, not across slices.

### Per-slice AFK loop (identical recipe)

For slice `Sn`, the driver executes in order:

1. `git switch` to `S(n-1)` tip (or `master` for S0), then `git switch -c agent/sqlite-authority/<sn-branch>`.
2. **Fan out parallel implementer agents** for the slice's independent parts (see each slice's "Parallel fan-out" note). Where agents mutate overlapping files, run them with `isolation: worktree` or serialize that pair.
3. **Fan out adversarial-verify agents** (independent) — each attempts to break the slice against its "Adversarial verify" criteria and the crown-jewel invariants. Findings route back to implementers; loop until clean.
4. Run project-scope lint + the slice's test surface. Iterate to green.
5. **One `thermo-nuclear-code-quality-review` round.** Fix every major finding. Re-run lint/tests.
6. `git commit` (frequent, per-task commits during the slice; the final state is what the PR ships), `git push -u origin <sn-branch>`.
7. `gh pr create --base <parent-branch>` with a body summarizing the slice + linking this plan + listing any accepted-minor thermo findings.
8. Proceed to `S(n+1)`.

### Hard stop conditions (AFK must never stack on red)

The run **halts on the current slice and writes a status report** (does not start the next slice) if any of:
- Project-scope lint or the slice test surface cannot be made green after bounded attempts.
- A thermo **major** finding cannot be resolved in-branch.
- An adversarial-verify agent **confirms** a crown-jewel invariant regression (e.g. exposure nets from raw account position; a terminal outcome is fabricated; a duplicate execution double-counts).
- A golden-fixture parity assertion fails at its pinned tolerance and the cause is not a fixture bug.

The report names the slice, the failing gate, the evidence, and the last green commit. The human resumes.

### Not AFK — S6 (human + market hours + host data-plane)

The fresh-generation cutover ceremony (stop bots → prove account flat/no working orders → activate new authority generation on v8 → redeploy) and the live paper ENTER→broker fill (partial or full)→EXIT→restart qualification required a human, live market hours, and a **host** data-plane (the container data-plane cannot drive live deploys/Clerk-RPC). S6 was completed on 2026-08-11; its receipts and the deterministic/live evidence split are documented under "Slice S6" below and in the paper-soak audit.

---

## File Structure

**Backend — capture / schema / folds (`PythonDataService/app/broker/`)**
- `contract/models.py` — add `execution_id: str | None` to `BrokerOrderEvent` (S0).
- `alpaca/adapter.py` — `from_alpaca_trade_update` maps `execution_id` onto the event (S0).
- `alpaca/clerk/trade_evidence.py` — `SqliteTradeUpdateEvidenceSink.record_lifecycle_event` stops `del event`; records per-execution slices (S1).
- `alpaca/clerk/sqlite/schema.py` — v8 DDL: `fills` execution-provenance columns, `external_orders`, `bot_config`, `decision_receipts`; new indexes; `SCHEMA_VERSION = 8` (S1).
- `alpaca/clerk/sqlite/facts.py` — `ExecutionSliceFilledFacts`, `ExecutionCorrectedFacts` (S1).
- `alpaca/clerk/sqlite/folds.py` — register `EXECUTION_SLICE_FILLED`, `EXECUTION_CORRECTED`; `_fold_execution_slice_filled`, `_fold_execution_corrected`; demote `_fold_order_fill_observed` to recovery-only (S1).
- `alpaca/clerk/sqlite/order_evidence.py` — capture path emits per-slice facts; cumulative path flagged `evidence_source="cumulative_recovery"` (S1).
- `alpaca/clerk/sqlite/economic_projection.py` — **new**: `bot_fills`, `account_executions`, `bot_economic_snapshot`, `catalog_economic_rollup` readers over SQLite facts, reusing `fifo_pnl` + incremental lots (S2).
- `alpaca/clerk/sqlite/reconcile.py` — foreign order → `external_orders` observation + hold (S4).

**Backend — canonical math / decision receipts (unchanged math, new call sites)**
- `alpaca/clerk/fifo_pnl.py` — reused as-is (S2). No new P&L math.
- `alpaca/clerk/sqlite/decision_receipts.py` — **new**: bounded `decision_receipts` reader/writer (S1 table, S5 cutover).

**Backend — projections / adapters / routers (`PythonDataService/app/services`, `app/routers`)**
- `services/broker_v2_panel/sqlite_panel_adapter.py` — populate `recent_fills`/`fills_today`/`realized_pnl_today`/`open_pnl`, real `working_orders` qty/side from the economic snapshot (S3).
- `services/broker_v2_panel/chart_projection_service.py` — accept SQLite `FillRecord`s (S3).
- `services/sqlite_clerk_transaction_projection.py` — execution economics + origin (`classify_ownership`), drop hardcoded `"strategy"`, external/unknown origins (S4).
- `schemas/clerk_transaction_projection.py` — extend `TransactionOrigin` with `external`, `unknown` (S4).
- `routers/clerk_transactions.py` — external-order rows + operator-ack action wiring (transport only) (S4).

**Frontend (`Frontend/src/app`)**
- `components/broker/account-desk/*` — the single authoritative SQLite desk (origin-aware grid) that alpaca-desk consolidates into (S4).
- `components/brokers/alpaca-desk/alpaca-orders-table.component.ts` — retired/repointed to the SQLite account-history surface (S4).

**Docs**
- `docs/architecture/adrs/0035-...md` + `docs/architecture/engine-authority-map.md` — reconcile Accepted/active vs Proposed/pending (S0).
- `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md §3` — v8 DDL (S1).
- `docs/references/` — golden-fixture attribution + tolerance notes (S0/S2).

---

## Slice S0 — Contract & Fixtures (tracer bullet)

**Branch:** `agent/sqlite-authority/s0-contract-fixtures` (base `master`).
**Goal:** Land the execution-identity contract, reconcile the doc split, and land **failing** golden fixtures that encode the target behavior. Nothing here changes runtime folds yet — it makes the target machine-checkable.
**Parallel fan-out:** (a) doc reconciliation; (b) `BrokerOrderEvent.execution_id` + parser; (c) each golden-fixture family (independent files).

**Interfaces produced (consumed by S1+):**
- `BrokerOrderEvent.execution_id: str | None` — the raw Alpaca `execution_id` for `fill`/`partial_fill` frames, else `None`.
- Golden fixtures under `PythonDataService/tests/fixtures/golden/alpaca-sqlite-execution/` with `attribution.md`.

### Task S0.1: Add `execution_id` to the execution-event contract

**Files:**
- Modify: `PythonDataService/app/broker/contract/models.py` (`BrokerOrderEvent`, ~lines 174-186)
- Modify: `PythonDataService/app/broker/alpaca/adapter.py` (`from_alpaca_trade_update`, ~lines 345-375)
- Test: `PythonDataService/tests/broker/alpaca/test_adapter_orders.py`

**Interfaces — Produces:** `BrokerOrderEvent(event_type, occurred_at_ms, price, quantity, execution_id)`.

- [ ] **Step 1: Write the failing test**

```python
def test_from_alpaca_trade_update_preserves_execution_id():
    payload = {
        "event": "fill",
        "execution_id": "exec-abc-123",
        "timestamp": "2026-08-10T13:31:00Z",
        "price": "190.25",
        "qty": "3",
        "order": {"id": "ord-1", "client_order_id": "learn-ai/sid-1/v1::intent-1"},
    }
    event = from_alpaca_trade_update(payload)
    assert event.execution_id == "exec-abc-123"
    assert event.quantity == 3.0
    assert event.price == 190.25
```

- [ ] **Step 2: Run it, verify it fails** — `pytest tests/broker/alpaca/test_adapter_orders.py::test_from_alpaca_trade_update_preserves_execution_id -v` → FAIL (`execution_id` not a field / not mapped).
- [ ] **Step 3: Implement** — add `execution_id: str | None = None` to `BrokerOrderEvent`; in `from_alpaca_trade_update`, set `execution_id=payload.get("execution_id")`. Leave `execution_id=None` for non-fill events.
- [ ] **Step 4: Run it, verify it passes.**
- [ ] **Step 5: Regression** — assert `execution_id is None` for a `canceled` frame; assert the legacy JSONL path (`fills.normalize_fill_event`) still derives `event_key` unchanged.
- [ ] **Step 6: Commit** — `git commit -m "feat(alpaca): add execution_id to BrokerOrderEvent contract"`.

### Task S0.2: Reconcile ADR 0035 ↔ engine-authority-map

**Files:** Modify `docs/architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md` (Status), `docs/architecture/engine-authority-map.md` (row ~19).

- [ ] Make the two documents agree on one truth: this program starts a **fresh generation**; state the current acceptance/activation status consistently in both, and record that the SQLite economic ledger, bot attribution, account history, decision receipts, and product projections are **in scope** of the authority (the ADR presently under-scopes these).
- [ ] Define, in the ADR or a linked reference: **fill count = number of effective execution slices** (not filled orders, not lifecycle updates); **corrections** = an effective replacement whose superseding transition adjusts position/P&L while the corrected slice stays auditable; **`fills_today` / `realized_pnl_today`** = closed lots with `closed_at_ms ∈ [session_open_ms, session_close_ms)` for the canonical NY session (half-day aware).
- [ ] Commit — `docs(alpaca): reconcile SQLite authority scope across ADR 0035 and authority map`.

### Task S0.3: Failing golden fixtures (one file per family)

**Files (create):**
- `tests/fixtures/golden/alpaca-sqlite-execution/googl_round_trip/{input.json,expected.json,attribution.md}`
- `.../external_order/…`, `.../partial_fill_sequence/…`, `.../downward_correction/…`, `.../null_vs_verified_zero/…`
- Test: `tests/broker/alpaca/clerk/sqlite/test_execution_authority_golden.py`

- [ ] **GOOGL round-trip:** a BUY (2 execution slices) then a SELL closing the position → expected: 3 attributed fills for `sqlite-cohort-googl-0810`, `fills_today == 3`, one closed lot, realized P&L per FIFO, `open_pnl == 0.0`, `marks_complete == True`.
- [ ] **External order:** a broker order whose `client_order_id` is not in a bot namespace → expected: one `external_orders` observation + one active hold; **zero** change to any bot's fills/positions/P&L; an operator-ack action available.
- [ ] **Partial-fill sequence:** slices (2, then 5) → expected: two fill rows summing to 7, not cumulative 2 then 7; per-slice `execution_id`s preserved as `fill_id`s.
- [ ] **Downward correction:** a slice of 5 later corrected to 3 → expected: an appended `EXECUTION_CORRECTED` superseding transition; effective position uses 3; the original 5-slice stays auditable; no negative-qty fill row silently overstating exposure.
- [ ] **Null vs verified-zero:** a bot with no fills → `fills_today == 0` and `realized_pnl_today == 0.0` (verified zero); a bot whose economic projection is unavailable → `None` (unavailable). Assert the two are distinguishable.
- [ ] Write `test_execution_authority_golden.py` asserting each fixture at `atol=1e-6, rtol=0` for P&L, exact integers for counts. These tests **fail now** (folds/projections not built) — that is expected; they are the S1/S2 acceptance oracle. Mark them `@pytest.mark.xfail(reason="S1/S2 implement the authority", strict=True)` and each implementing task flips its fixture to passing by removing the xfail.
- [ ] Commit — `test(alpaca): add failing golden fixtures for SQLite execution authority`.

**Adversarial verify (S0):** confirm the GOOGL fixture is *derivable* from websocket `trade_updates` frames (execution_id present) and not from cumulative snapshots; confirm the external-order fixture's `client_order_id` genuinely fails `order_identity.classify_ownership`.

---

## Slice S1 — Authoritative Execution Ledger (schema v8 + capture)

**Branch:** `agent/sqlite-authority/s1-execution-ledger` (base S0).
**Goal:** Capture per-execution slices with real `execution_id` idempotency into a v8 schema; add external-order, bot-config, and decision-receipt tables; keep the hash chain clean; demote cumulative folding to recovery-only.
**Parallel fan-out:** DDL+pinned-contract+parity-test must land **first** (serial); then parallel: (b) facts + new folds; (c) sink capture change; (d) external_orders table+fold; (e) bot_config persistence; (f) decision_receipts table.

**Interfaces produced (consumed by S2+):**
- `fills` columns: `execution_id TEXT` (the real Alpaca exec id, = `fill_id` on the capture path), `evidence_source TEXT CHECK IN ('websocket','activity_recovery','cumulative_recovery')`, `event_kind TEXT CHECK IN ('fill','correction')`, `superseded_execution_ref TEXT`, `fee REAL`, `fee_fidelity TEXT CHECK IN ('reported','not_reported')`, `source_event_at_ms`, `clerk_observed_at_ms`, `recorded_at_ms`, and v8's nullable `recorded_transition_sequence`.
- New transition kinds: `EXECUTION_SLICE_FILLED`, `EXECUTION_CORRECTED`.
- Tables: `external_orders` (including v8 `order_type`, `limit_price`, `stop_price`, and `filled_avg_price`), `bot_config`, `decision_receipts`.

### Task S1.1: v8 schema (DDL + pinned contract + parity test + version)

**Files:** `sqlite/schema.py` (`SCHEMA_DDL`, `SCHEMA_VERSION`, `SCHEMA_MIGRATIONS`), `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md §3`, `tests/broker/alpaca/clerk/sqlite/test_schema_parity.py`.

- [ ] Add `fills` provenance columns (fold table — **not** hash-participating, safe to add). Backfill semantics: `fill_id` on the capture path is the Alpaca `execution_id`; the cumulative-recovery path keeps the synthesized `order_ref:qty` id but sets `evidence_source='cumulative_recovery'`.
- [ ] Add `external_orders(external_order_id PK, broker_order_id, client_order_id, symbol, side, qty, price, observed_at_ms, acknowledged_at_ms, ack_operator, evidence_refs_json)`; `bot_config(strategy_instance_id PK FK, strategy_key, display_name, config_json, config_hash, created_at_ms)`; `decision_receipts(strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, observed_at_ms, facts_json, PRIMARY KEY(strategy_instance_id, seq))` with a bounded-tail index.
- [ ] Bump `SCHEMA_VERSION = 8`. Register the guarded sequential additive path
  in `SCHEMA_MIGRATIONS`: entry `6` applies the v6→v7 execution-provenance
  `fills` columns, tables, and indexes; entry `7` applies v7→v8
  `fills.recorded_transition_sequence` plus the external-order `order_type`,
  `limit_price`, `stop_price`, and `filled_avg_price` columns. A caller reaches
  v8 only by applying both entries in order. The v6→v7 step first proves every
  operational v6 table empty; a data-bearing v6 authority fails closed and
  rolls back untouched. A fresh generation is created directly at v8 via
  `apply_schema`.
- [ ] Update the pinned-contracts doc §3 to the exact new DDL block; make `test_schema_parity.py` pass byte-for-byte.
- [ ] Assert boot `verify_chain` / `integrity_check` still pass on a freshly-initialized v8 DB (no hash-participating column added). Commit.

### Task S1.2: Execution-slice facts + folds

**Files:** `sqlite/facts.py`, `sqlite/folds.py`, test `tests/broker/alpaca/clerk/sqlite/test_folds_execution.py`.

- [ ] `ExecutionSliceFilledFacts(execution_id, symbol, side, slice_qty, slice_price, fee, fee_fidelity, evidence_source, source_event_at_ms)`; `ExecutionCorrectedFacts(execution_id, superseded_execution_ref, symbol, side, corrected_qty, corrected_price, why)`.
- [ ] `_fold_execution_slice_filled`: `INSERT INTO fills` keyed on `execution_id` (UNIQUE ⇒ durable idempotency; a redelivery is a no-op); update `positions` by `signed_delta`; **never** net from raw account position. Reject/no-op if `execution_id` already recorded.
- [ ] `_fold_execution_corrected`: append a fill row `is_correction=1, event_kind='correction'` linked via `superseded_execution_ref`; adjust `positions` by the *delta* between corrected and superseded effective qty; the superseded row stays. If a regression cannot be explained by a matching superseded ref → do **not** write; raise an `UNCERTAINTY_RAISED` that blocks new exposure (reuse existing uncertainty fold).
- [ ] Register both kinds in `DEFAULT_FOLD_REGISTRY`. Confirm mirror-rebuild replays them (same registry drives rebuild).
- [ ] TDD each against the S0 partial-fill + correction fixtures; flip those fixtures' `xfail` off. Commit.

### Task S1.3: Sink records slices (stop discarding the event)

**Files:** `alpaca/clerk/trade_evidence.py` (`SqliteTradeUpdateEvidenceSink.record_lifecycle_event`), `sqlite/order_evidence.py`, test `tests/broker/alpaca/clerk/test_trade_evidence_sqlite.py`.

- [ ] Remove `del event`. For `event.event_type in ('fill','partial_fill')` with `event.execution_id`: resolve the owning effect operation (as today), then append an `EXECUTION_SLICE_FILLED` transition from the event's own `execution_id`/`quantity`/`price`/`occurred_at_ms`. Fold the aggregate order acknowledgement separately and monotonically (as today).
- [ ] Keep the cumulative `fold_order_evidence` path **only** for recovery/reconcile call sites; tag rows it writes `evidence_source='cumulative_recovery'` and mark exact-execution coverage incomplete.
- [ ] Idempotency: the `fills.execution_id` UNIQUE constraint is the durable guard; the consumer `_seen` map remains an optimization. Test websocket **redelivery** (same execution_id) and **process restart** → no duplicate fill.
- [ ] Unexplained order (no local order) path unchanged (raises hold). Commit.

### Task S1.4: Bot config + decision-receipt tables populated at write time

**Files:** where `strategy_instances` rows are inserted (registration path), `sqlite/decision_receipts.py`, tests.

- [ ] On bot registration, persist the **complete immutable config** into `bot_config` (strategy_key, display_name, config_json, config_hash) — so the roster never shows `strategy_key="unknown"` and never depends on a disposable runner binding. `STRATEGY_INSTANCE_REGISTERED` fold writes `bot_config` in the same transaction.
- [ ] Add `decision_receipts` writer (bounded) + `tail(n)` / `by_transaction(ref)` reader. Do **not** wire the runtime decision producer yet (S5 cuts the JSONL over); this slice only lands the table + API so S5 is a pure swap.
- [ ] Commit.

**Adversarial verify (S1):** attempt double-count via redelivery + restart; attempt exposure overstatement via an unmatched downward correction (must block, not silently write); confirm no new hash-participating column (boot `verify_chain` green); confirm a fresh v8 DB rebuilds identically from its mirror.

**S1 test surface:** `pytest tests/broker/alpaca/clerk/ tests/broker/alpaca/ -k "sqlite or trade_evidence or fold or schema"`.

---

## Slice S2 — SQLite-native economic projections

**Branch:** `agent/sqlite-authority/s2-projections` (base S1).
**Goal:** Compute fills, fill counts, realized/open P&L, and catalog rollups from SQLite facts only, reusing the canonical FIFO and incremental lots.
**Parallel fan-out:** (a) `bot_fills`; (b) `account_executions`; (c) `bot_economic_snapshot`; (d) `catalog_economic_rollup`. All read the S1 ledger; independent.

**Interfaces produced (consumed by S3/S4):**
- `bot_fills(strategy_instance_id, cursor, limit, from_ms, to_ms) -> Page[FillRecord]`
- `account_executions(cursor, limit, origin, bot, symbol, state) -> Page[ExecutionRow]`
- `bot_economic_snapshot(strategy_instance_id, session_window, marks) -> EconomicSnapshot` with: recent effective fills, exact `fills_today`, SQLite-attributed `exposure`, FIFO `realized_pnl_today`, `open_pnl` (or `None` if marks incomplete), `fee_fidelity`, `execution_coverage`, `authority_generation`, `control_revision`.
- `catalog_economic_rollup(sids) -> dict[sid, EconomicSnapshot]` in one read transaction.

### Task S2.1: `bot_economic_snapshot` over SQLite fills (reuse FIFO)

**Files (create):** `sqlite/economic_projection.py`; test `tests/broker/alpaca/clerk/sqlite/test_economic_projection.py`.

- [ ] Build `FillRecord`s from the `fills` table (per-slice, dedup by `execution_id`), sorted by `filled_at_ms`. Feed `fifo_pnl.compute_fifo_pnl` / `realized_pnl_for_window`. **Do not page raw fills for P&L** — FIFO needs full history; maintain incremental lots via `apply_fill_to_lots` (reuse the `rollup_cache` pattern). Bounded reads apply to *display lists*, not lot computation.
- [ ] Session window from the canonical NYSE calendar (half-day aware); `realized_pnl_today` filters closed lots by `closed_at_ms` in `[open, close)`.
- [ ] `open_pnl` via `compute_open_pnl(marks)` — `None` unless every open-lot symbol has a mark; carry the mark source-timestamp; never a partial sum.
- [ ] `fee_fidelity`: `not_reported` whenever any fill has `fee is None` (Alpaca paper = no per-fill fee) — propagate honestly, never `$0.00`.
- [ ] Flip the GOOGL + null-vs-zero fixtures' xfail off; assert at `atol=1e-6`. Commit.

### Task S2.2: `bot_fills`, `account_executions`, `catalog_economic_rollup`

**Files:** `sqlite/economic_projection.py`; tests.

- [ ] `bot_fills` / `account_executions` — keyset-paginated bounded readers (cursor by `recorded_at_ms, execution_id`), filterable by window/origin/symbol/state; **display** projections, distinct from the lot computation.
- [ ] `catalog_economic_rollup` — one read transaction across all sids for the roster; reuse the incremental snapshot per sid.
- [ ] Golden-fixture + shape/dtype tests. Commit.

**Adversarial verify (S2):** "2 fills today cannot coexist with 0 chart markers" — assert panel and chart derive from the same SQLite revision/snapshot; DST/half-day boundary tests for `fills_today`/`realized_pnl_today`; prove a prior-session BUY + today SELL is not miscounted (no pre-filter before FIFO).

---

## Slice S3 — Migrate product consumers to SQLite

**Branch:** `agent/sqlite-authority/s3-consumers` (base S2).
**Goal:** Panel, chart, roster read the S2 economic snapshot; remove the hardcoded nulls; roster shows real strategy identity.
**Parallel fan-out:** (a) panel adapter; (b) chart projection; (c) roster/catalog identity.

### Task S3.1: Un-hardcode the panel adapter

**Files:** `services/broker_v2_panel/sqlite_panel_adapter.py` (lines 97-103, 285-304), test `tests/broker/v2panel/test_panel_projection.py`.

- [ ] Replace `recent_fills=[]`, `fills_today=None`, `realized_pnl_today=None`, `open_pnl=None` with the `bot_economic_snapshot` values. Populate `working_orders` `side`/`quantity`/`filled_quantity` from SQLite (retire "Unknown — inspect custody timeline").
- [ ] Test: a bot with the GOOGL fixture renders 3 recent fills, `fills_today=3`, realized P&L, `open_pnl` (or null if no mark). Commit.

### Task S3.2: Chart markers from SQLite fills

**Files:** `services/broker_v2_panel/chart_projection_service.py` (`build_live_chart`, `_markers_in_window`, `_fill_to_marker`), tests.

- [ ] Change the marker source from `OrderJournalEntry`/`project_instance_fills` to SQLite `FillRecord`s from S2 at the **same revision** the panel used. Keep bar sourcing (Polygon/live) unchanged.
- [ ] Test: chart markers count == `fills_today` for the GOOGL fixture window. Commit.

### Task S3.3: Roster shows real strategy identity

**Files:** `services/broker_v2_panel/catalog_projection_service.py`, `sqlite_panel_adapter.py` (`build_sqlite_catalog`), tests.

- [ ] Source `strategy_key`/display name from `bot_config` (S1). Assert the roster never emits `"unknown"` for a registered bot. Catalog rollups (exposure, fills_today, P&L, last_activity) from `catalog_economic_rollup`. Commit.

**Adversarial verify (S3):** grep the active-SQLite panel/chart/roster paths for any JSONL/Postgres/rollup-of-JSONL read; confirm none remain on the product path (full retirement is S5, but S3 must not *add* one).

---

## Slice S4 — Account history + external origin + one authoritative desk

**Branch:** `agent/sqlite-authority/s4-desk` (base S3).
**Goal:** Enrich the SQLite transaction projection with execution economics + origin; record external orders as external rows + hold + operator-ack; consolidate to one SQLite-backed desk surface.
**Parallel fan-out:** (a) transaction projection + origin enum; (b) external-order observation + ack action (backend); (c) desk consolidation (frontend).

### Task S4.1: Origin-aware transaction projection

**Files:** `services/sqlite_clerk_transaction_projection.py` (drop hardcoded `origin="strategy"`, line 117), `schemas/clerk_transaction_projection.py` (`TransactionOrigin` += `external`, `unknown`), tests.

- [ ] Derive origin via `order_identity.classify_ownership`: bot-namespace → `strategy` (with bot/run identity); manual namespace → `manual`; unrecognized → `external` or `unknown` (+ hold). Add execution economics (qty/price/fee/fee_fidelity) from the S2 projection. Commit.

### Task S4.2: External orders → observation + hold + operator-ack

**Files:** `sqlite/reconcile.py`, `trade_evidence.py` (unexplained path), `sqlite/folds.py` (external-order + ack folds), `routers/clerk_transactions.py` (ack action, transport only), tests.

- [ ] Foreign/unexplained order: write an `external_orders` observation **and** raise the exposure-blocking hold (unchanged safety) **and** surface it as an `external` transaction row (never touching any bot's fills/P&L).
- [ ] Add an operator-acknowledge action (SQLite transition) that reclassifies the external order as reviewed and resolves its hold — a UI exit button. Test: ack clears the hold; the external row remains; no bot P&L changed. Commit.

### Task S4.3: One authoritative SQLite desk

**Files:** `components/broker/account-desk/*` (reused as the single desk), `components/brokers/alpaca-desk/alpaca-orders-table.component.ts` (retire/repoint), `Frontend/src/app/services/brokers.service.ts`, component specs.

- [ ] Consolidate to a single SQLite-backed desk: the account-desk origin-aware, filterable transaction grid becomes the one implementation; alpaca-desk's "Transaction history" table is retired in favor of it (or repointed to the same `/api/accounts/{id}/transactions` surface + component). Labels render "Placed by <bot>", "External / manual", "Unknown — review required" via `receiptLabel` where they are codes.
- [ ] Retire the generic broker-orders **product** read; keep `GET /api/brokers/{broker}/orders` for reconciliation/diagnostics only.
- [ ] Vitest: the desk renders SQLite rows with origin labels; a browser/network spec asserts the Alpaca pages do not call `/api/brokers/alpaca/orders`. Commit.

**Adversarial verify (S4):** an external order never alters a bot's fills/P&L; the ack path can't clear a hold that has other active causes; origin classification matches `classify_ownership` on the ownership ladder.

---

## Slice S5 — Retire secondary authorities + contract tests

**Branch:** `agent/sqlite-authority/s5-retire` (base S4).
**Goal:** Make "sole authority" enforceable: decision receipts to SQLite, process-registry demoted, JSONL/Postgres product readers removed, and a contract test that fails if they come back.
**Parallel fan-out:** (a) decision-receipt cutover; (b) process-registry demotion; (c) reader removal + contract test.

### Task S5.1: Decision receipts → SQLite

**Files:** the decision producer call site, `sqlite/decision_receipts.py`, the recent-decisions endpoint/reader, tests.

- [ ] Write `enter_intent`/`exit_intent`/`no_action`/`blocked` receipts into the `decision_receipts` table (S1) instead of the JSONL `DecisionJournal`; point the "LATEST DECISION"/recent-decisions UI reader at SQLite. Assert receipts are **not** in `custody_transitions` (separate table, per the decision). Commit.

### Task S5.2: Process registry → ephemeral liveness only

**Files:** wherever the process registry reconstructs durable bot identity/lifecycle under active SQLite, tests.

- [ ] Remove durable-identity reconstruction from the process registry on the active-SQLite path (identity/lifecycle come from SQLite `strategy_instances`/`bot_config`/`runs`); the registry may report ephemeral task liveness only. Commit.

### Task S5.3: Remove product readers + contract test

**Files:** active-SQLite code paths reading JSONL custody / Postgres Clerk projections / in-memory rollup-of-JSONL; new `tests/broker/alpaca/clerk/test_authority_isolation.py`.

- [ ] Remove those reads from active-SQLite product paths (raw capture + archived artifacts may remain for diagnostics, but cannot populate product state).
- [ ] Contract test: fail if any active-SQLite screen/API module imports a JSONL/Postgres/rollup-of-JSONL reader (import-graph assertion over the product modules). Commit.

**Adversarial verify (S5):** grep the whole active-SQLite product surface for a fallback path; kill the SQLite DB in a test harness and assert every product read returns unavailable/hold/503, never legacy data.

---

## Slice S6 — Supervised fresh-generation cutover + live qualification (HUMAN, not AFK)

Tracked as GitHub issue #1447 and executed by the human on a **host** data-plane
during market hours on 2026-08-11. Post-acceptance fault hardening from #1440 was
qualified in the same campaign.

- [x] Stop all bots; block new exposure; verify no working orders; reconcile positions; obtain fresh broker proof of flat/no-open-orders.
- [x] With both writers stopped, atomically preserve and verify the exact final generation-1 DB/WAL/mirror set (schema, identity, revision/sequence, head hash, file hashes). Initialize a **new authority generation** on schema v8 (clean-slate; no import), take and verify its transition-free online baseline backup, then redeploy the desired instance with full config persisted.
- [x] Record `execution_coverage_start_ms`. (No JSONL consulted at read time.)
- [x] Before exposure, verify the clean generation-2 DB/mirror pair at identical genesis and reconcile against Alpaca. After the non-empty paper round trip, rebuild from the mirror and prove identical fills/positions/lots/rollups reproduce, then reconcile flat again.
- [x] **Qualification gates (all passed on 2026-08-11):** GOOGL shows its fills in bot detail, roster rollup, chart markers, and account history; every account transaction identifies a bot or says external/unknown; websocket redelivery + process restart + REST/activity recovery never duplicate a fill; partial fills preserve every slice; corrections restate exposure + FIFO P&L; external orders never alter a bot's fills/P&L; SQLite/mirror rebuild reproduces identical results; corrupt/missing SQLite ⇒ unavailable/503 (never fallback); a browser network test proves the Alpaca pages never call the generic broker-orders endpoint; DST/half-day/holiday/session-boundary window tests pin fill-count and realized-P&L windows; catalog+panel performance remains within existing budgets; the final supervised one-share paper ENTER→full-fill→EXIT→restart ceremony preserves identical counts, attribution, and P&L. Receipts are in `docs/audits/alpaca-sqlite-clerk-paper-soak-2026-08-07.md`.

**Recorded S6 execution interpretation.** The stopped-writer preservation is the
final generation-1 recovery artifact; the earlier online backup is retained but is
not represented as the cutover head. The pre-exposure mirror proof was the exact
empty genesis pair; the meaningful non-empty rebuild followed the paper round trip.
The one-share broker ceremony could produce either partial or full execution and in
fact produced two terminal full fills. It does not claim a live partial. Partial,
correction, and duplicate/redelivery behavior is supported by the retained
source-accurate deterministic receipts, while the supervised paper receipt confirms
the normal broker/UI/restart path. This is the formal evidence split used for #1440's
“where live confirmation is meaningful” rule.

---

## Self-Review

- **Spec coverage:** the eight authority-contract rows (config, lifecycle, orders, executions/corrections, attribution, external orders, positions/lots, fills/P&L, history, marks, raw-capture) each map to S1–S4 tasks; the six broken-today anchors each map to a fix (trade_evidence→S1.3, order_evidence/folds→S1.2, schema→S1.1, panel adapter→S3.1, chart→S3.2, orders-table/desk→S4.3, transaction projection→S4.1, docs→S0.2). Qualification gates → S6.
- **Placeholder scan:** no "TBD"/"handle edge cases" — each task names files, interfaces, and the concrete assertion. Later-slice code that legitimately depends on earlier-slice output is specified by interface + test oracle (the S0 golden fixtures), not by fabricated line-level code.
- **Type consistency:** `execution_id` (S0) → `fills.execution_id`/`ExecutionSliceFilledFacts` (S1) → `bot_economic_snapshot` (S2) → panel/chart/desk (S3/S4) use the same names. `TransactionOrigin` extension (S4.1) matches the desk labels (S4.3).
- **Hash-chain safety:** every schema change adds fold-table columns or new transition kinds only; the boot `verify_chain` assertion is in S1.1's acceptance.

## Execution Handoff

This plan is built to run **AFK as a stacked-PR pipeline** per the "AFK Execution Model": each slice fans out parallel implementer + adversarial-verify agents, runs project-scope lint + the slice test surface, takes one thermo round, then opens its stacked PR (never merges). The run halts on red rather than stacking. S6 is human-run.
