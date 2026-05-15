# SPY EMA crossover — paper dry-run pass (B2)

**Date:** 2026-05-15
**Status:** Brainstorming approved; ready for writing-plans
**Target dry-run gate date:** Tuesday 2026-05-19 (seed day: Monday 2026-05-18)
**PR sequence:** PR1 (indicator-state persistence) → PR2 (LiveEngine→reconcile producer-consumer CI test) → operator dry-run

**Related authoritative docs:**
- `docs/ibkr-integration-authority.md` — IBKR integration snapshot (§11 Phase 10 prereqs is the parent gate)
- `docs/runbooks/ibkr-paper-dry-run.md` — operator step-by-step (Steps 1–5)
- `docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md` — Path C shadow deployment (Phase D = "the dry run")
- `.claude/rules/numerical-rigor.md` — timestamp policy (int64 ms UTC), Decimal handling, golden-fixture rules
- `.claude/rules/python.md` — Pydantic v2, ruff scope, dependency-add policy

---

## 1. Goal and success criteria

### Goal

Ship indicator-state persistence for `SpyEmaCrossoverAlgorithm` so that a fresh `start` invocation at 09:30 ET can resume from the prior session's indicator internals instead of burning ~3 h 45 m on warmup. Then run one full-RTH dry run end-to-end on Tuesday 2026-05-19 in `--readonly` mode and produce a clean reconcile receipt that proves the warmup was actually skipped.

### Pass criteria for the Tue 2026-05-19 B2 dry-run gate

1. `start` on Tue runs `hydrate_policy=require`; the runner exits non-zero (exit 4) before any bar is consumed if Monday's sidecar is missing, stale per NYSE-previous-session, schema-mismatched, indicator-unready, symbol/period-mismatched, or non-flat.
2. `start` runs 09:30 → 16:00 ET, never halts on a non-data condition, and emits a `[BAR]` heartbeat every minute.
3. `decisions.parquet` has its **first row after the first consolidated 15-min bar close (09:45 ET, equivalently `bar_end_ms` ≤ 09:45 ET converted to UTC ms)** — not after 13:15 ET. This is the load-bearing assertion that warmup was skipped.
4. `reconcile` (Step 4 of the runbook) produces a well-formed `day-0.md` with **either** `Halt triggered for next session: no` **or** a halt whose sole cause is `fill-class breach count=N` from `--readonly`-mode unmatched ENTER/EXIT intents. The runbook calls the latter "expected behavior in dry-run mode, not a bug, and a pass on the pipeline-correctness criterion." Any other halt cause (engine divergence, data class divergence, schema error, etc.) fails the gate. Operator deletes the breach `halt.flag` before any subsequent `pre-flight`.
5. The hydration receipt at `<run_dir>/indicator_state_hydration.json` shows `accepted=true`, and its SHA-256 appears in `reconcile/day-0.hashes.json`.
6. **Bonus, not gating:** any ENTER/EXIT signal during the session.

### Out of scope (explicit; not bugs)

- `commissionReport` callback wiring — real fills still record `fee=0`; OK for `--readonly`, gap before paper-week day 1.
- `equity_curve.parquet` writer — reconcile doesn't compare equity series today.
- Periodic mid-session state writes (W3) and mid-session crash recovery — only force-flat + graceful-shutdown checkpoints in PR1.
- Generic indicator persistence framework — strategy-specific hooks on `SpyEmaCrossoverAlgorithm` only; base-class promotion deferred to the next live strategy.
- Automatic seed-day mode detection — operator passes `--hydrate-policy optional` explicitly on Monday.
- `live-runtime` row in `docs/math-sources-of-truth.md` — added only after the 15-day paper week per Path C §6.

---

## 2. Architecture

### 2.1 Module surface changes

| Module | Type | What it owns |
|---|---|---|
| `app/engine/live/indicator_state.py` | **new** | `IndicatorStateEnvelope` + `IndicatorStatePayload` (Pydantic v2), `HydratePolicy` enum, `IndicatorStateRepo` (read/write the global sidecar with advisory file lock), `HydrationReceipt`, `ValidationResult`, top-level `hydrate(...)` and `maybe_write(...)` functions. |
| `app/engine/live/nyse_calendar.py` | **new** | `previous_completed_nyse_session_close_ms(session_start_ms: int) -> int`. Thin wrapper over `pandas_market_calendars.get_calendar("NYSE")` (already in `requirements-light.txt`). Pure function; UTC ms in, UTC ms out; honors early-close days and holidays. |
| `app/engine/strategy/algorithms/spy_ema_crossover.py` | **edit** | Add three strategy-local methods: `report_state_for_persistence() -> IndicatorStatePayload \| None` (returns `None` unless indicators are all `is_ready`, position is flat, no pending orders, no open insights); `restore_state_from_persistence(payload) -> None` (rehydrates `ema5` / `ema10` / `rsi14` internals + `_prev_ema5_above_ema10`); `validate_state_payload(payload) -> ValidationResult` (check #4 in §4.1 ladder — required keys + types for this strategy's shape). Strategy-local for PR1; base-class promotion deferred. |
| `app/engine/live/live_context.py` | **edit** | New ctor params `hydrate_policy: HydratePolicy` and `hydration_receipt_path: Path`. New methods `hydrate_indicator_state(strategy)` and `maybe_write_indicator_state(strategy, reason)`. |
| `app/engine/live/live_engine.py` | **edit** | Three call sites: after `strategy.initialize()` → `ctx.hydrate_indicator_state(strategy)`; after force-flat barrier submits → `ctx.maybe_write_indicator_state(strategy, "force_flat")`; in `engine.run()`'s `finally` → `ctx.maybe_write_indicator_state(strategy, "shutdown")`. |
| `app/engine/live/run.py` | **edit** | `start --hydrate-policy {require\|optional\|disabled}` (default `require`); `--allow-cold-start` (alias for `--hydrate-policy disabled`). Threads through to `LiveContext`. New exit code `4` for hydrate-validation failure. |
| `app/engine/live/reconcile.py` | **edit** | `_build_day_hashes_manifest` includes `indicator_state_hydration.json` if it exists under `<run_dir>/`. Markdown receipt's manifest section lists it with SHA-256. |
| `app/engine/strategy/base.py` | **no change** | Hooks live on `SpyEmaCrossoverAlgorithm` only for PR1. Promoting to base class is the seam for PR2-N. |

### 2.2 Envelope (generic part — validated for every payload type)

```json
{
  "schema_version": 1,
  "strategy_key": "spy_ema_crossover",
  "symbol": "SPY",
  "consolidator_period_min": 15,
  "last_consolidated_bar_end_ms": 1747166100000,
  "captured_at_ms": 1747166107842,
  "captured_reason": "force_flat",
  "code_sha": "<git HEAD at write time>",
  "strategy_spec_sha": "<spec file sha at write time>",
  "payload": { ... }
}
```

### 2.3 Payload (SpyEma-specific part for PR1)

```json
{
  "ema5":  {"is_ready": true, "samples": 18, "value": "412.345678901234567890", "ewma_state": "..."},
  "ema10": {"is_ready": true, "samples": 18, "value": "411.234567890123456789", "ewma_state": "..."},
  "rsi14": {"is_ready": true, "samples": 18, "avg_gain": "1.2345", "avg_loss": "0.8762",
            "value": "58.42", "prev_close": "411.50"},
  "_prev_ema5_above_ema10": true,
  "lifecycle": {
    "position_qty": 0,
    "pending_orders_count": 0,
    "open_insights": 0,
    "last_signal_kind": null,
    "last_signal_bar_end_ms": null
  }
}
```

All `Decimal` fields serialize as **quoted strings** to round-trip exactly via `Decimal(str)`. The exact set of indicator internal fields per indicator (e.g. `ewma_state` for EMA) is discovered during PR1 implementation by reading `app/engine/indicators/{ema,rsi}.py`; the design constraint is that `restore_state_from_persistence(report_state_for_persistence())` produces a strategy that emits **bit-identical** indicator outputs on the next N bars relative to a freshly-warmed strategy fed the same prior bars (`Decimal` equality, `atol=0`). Test 4 in §5.1 enforces this.

### 2.4 File paths

```
PythonDataService/
  artifacts/
    live_state/
      spy_ema_crossover/
        SPY_15m.json              # the stable global sidecar
        SPY_15m.json.lock         # advisory file lock (fcntl on POSIX, msvcrt on Windows)
    live_runs/
      <run_id>/
        run_ledger.json           # immutable; untouched
        indicator_state_hydration.json   # per-run receipt
        decisions.parquet
        executions.parquet
        reconcile/
          day-0.{md,json,parquet,hashes.json}    # hashes.json now lists hydration receipt
```

`artifacts/` is gitignored. `live_state/` is a new top-level under it; no `.gitkeep` needed (directory is created on first write).

### 2.5 Hydration receipt schema

```json
{
  "schema_version": 1,
  "hydrated_at_ms": 1747641007500,
  "policy": "require",
  "global_path": "PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json",
  "global_sha256": "abc123...",
  "accepted": true,
  "strategy_key": "spy_ema_crossover",
  "symbol": "SPY",
  "consolidator_period_min": 15,
  "sidecar_last_consolidated_bar_end_ms": 1747166100000,
  "expected_prev_session_close_ms": 1747166100000,
  "calendar": "NYSE",
  "validation": {
    "schema_version_ok": true,
    "identity_ok": true,
    "calendar_ok": true,
    "payload_shape_ok": true,
    "indicators_ready_ok": true,
    "lifecycle_flat_ok": true,
    "failure_reason": null
  }
}
```

On `accepted=false`, `validation.failure_reason` is one of: `"disabled_by_operator"`, `"missing"`, `"schema_mismatch"`, `"identity_mismatch"`, `"calendar_stale"`, `"payload_mismatch"`, `"indicators_unready"`, `"lifecycle_not_flat"`. Mutually exclusive — the validation ladder is sequential; first failure stops.

---

## 3. Data flow

### 3.1 Read / hydrate path (called once after `strategy.initialize()`)

```
LiveEngine.run()
  └─> strategy.initialize()                          [unchanged]
  └─> ctx.hydrate_indicator_state(strategy)          [NEW]
        ├─ if policy == DISABLED:
        │     write receipt {accepted: false, reason: "disabled_by_operator"}; return
        ├─ read PythonDataService/artifacts/live_state/spy_ema_crossover/SPY_15m.json
        │   (under advisory lock; OK if missing)
        ├─ if missing:
        │     policy == REQUIRE  → write receipt {accepted: false, reason: "missing"};
        │                          raise IndicatorStateHydrationError → exit 4
        │     policy == OPTIONAL → write receipt {accepted: false, reason: "missing"}; return  (cold-start)
        ├─ run validation ladder (§4)
        │   on any failure:
        │     policy == REQUIRE  → write receipt {accepted: false, reason: <field>}; raise → exit 4
        │     policy == OPTIONAL → write receipt {accepted: false, reason: <field>}; return (cold-start)
        ├─ strategy.restore_state_from_persistence(envelope.payload)
        └─ write receipt {accepted: true, validation: {…all_passed}, global_sha256: <sha>}
```

### 3.2 Write / checkpoint path (two call sites)

```
on force_flat_barrier completion (LiveEngine.run, ~15:55 ET):
  ctx.maybe_write_indicator_state(strategy, reason="force_flat")
    └─ payload = strategy.report_state_for_persistence()
    └─ if payload is None:
         log "force-flat checkpoint skipped: strategy reports non-restorable state"; return
    └─ build envelope (captured_reason="force_flat", code_sha, strategy_spec_sha,
                       last_consolidated_bar_end_ms = ctx.last_consolidator_emit_end_ms)
    └─ acquire lock on SPY_15m.json
    └─ atomic write (tmp + os.replace) to global path
    └─ release lock

on engine.run() finally (graceful shutdown):
  ctx.maybe_write_indicator_state(strategy, reason="shutdown")
    └─ payload = strategy.report_state_for_persistence()
    └─ if payload is None: log; return
    └─ read existing global sidecar (if any)
    └─ if existing and existing.last_consolidated_bar_end_ms >= new.last_consolidated_bar_end_ms:
         log "shutdown checkpoint skipped: not newer than existing (Ctrl-C before force-flat)"; return
    └─ acquire lock + atomic write
```

### 3.3 The "newer" check on shutdown

The "newer" check is what makes early-Ctrl-C-before-force-flat safe: an operator who kills the runner at 11:00 ET cannot accidentally overwrite the prior session's good 15:55 sidecar with a partial-day checkpoint. Comparison is on `last_consolidated_bar_end_ms` (int64), not wall-clock — the on-disk envelope's bar-end timestamp is the authority.

Implication for shutdown checkpoints: in practice, mid-session SIGINT under an open position will return `payload is None` from `report_state_for_persistence()` (non-flat lifecycle) and skip *before* the newer-check is consulted. The newer-check is the guard for the narrow window between force-flat completion and the operator hitting Ctrl+C around 15:58 ET — both checkpoints would carry the same `last_consolidated_bar_end_ms`, the `>=` comparison correctly treats them as equivalent, and the older one wins. (Either is correct; this preserves the invariant that force-flat is the canonical write point.)

---

## 4. Validation and failure model

### 4.1 The six-check validation ladder (sequential; first failure stops)

| # | Check | Failure reason | What it catches |
|---|---|---|---|
| 1 | **Schema parse** — file readable, JSON parses, envelope passes Pydantic validation | `schema_mismatch` | Truncated / hand-edited / wrong-version sidecar |
| 2 | **Identity** — envelope `strategy_key`/`symbol`/`consolidator_period_min` match the runner's `LiveConfig` + strategy class | `identity_mismatch` | Operator pointing the wrong strategy at the wrong sidecar; param drift across PRs |
| 3 | **Calendar** — `envelope.last_consolidated_bar_end_ms == previous_completed_nyse_session_close_ms(session_start_ms)` | `calendar_stale` | Fri close → Tue open (after a Mon trading day). Includes early-close days |
| 4 | **Payload shape** — strategy's own `validate_state_payload(envelope.payload)` returns OK (required keys + types) | `payload_mismatch` | Spec-level param drift (e.g. RSI 14→21) that doesn't show up in envelope identity |
| 5 | **Indicators ready** — every indicator in payload has `is_ready==true` and `samples >= period+1` | `indicators_unready` | Sidecar captured mid-warmup; force-flat fired before warmup complete |
| 6 | **Lifecycle flat** — `position_qty == 0` AND `pending_orders_count == 0` AND `open_insights == 0` | `lifecycle_not_flat` | "Are we hydrating into a phantom open trade?" |

`code_sha` and `strategy_spec_sha` are **recorded** in both envelope and receipt but **not checked** by the ladder (forensic-only). Rationale: most PRs (logging, types, doc edits, unrelated code) don't change indicator math, and rejecting a sidecar on any tree change would force a 3 h 45 m re-warm for no real reason. Strict-block alternatives (Q3b/Q3c in the brainstorming record) were considered and rejected for PR1; revisit if a math-changing PR ever silently invalidates yesterday's sidecar.

### 4.2 Failure model by policy

| Policy | Sidecar missing | Sidecar present + validation fails | Sidecar present + valid |
|---|---|---|---|
| `require` (default for B2 gate / paper week) | Receipt `accepted=false, reason="missing"` → **exit 4** | Receipt `accepted=false, reason=<which>` → **exit 4** | Receipt `accepted=true`; restore; continue |
| `optional` (seed day / first run ever) | Receipt `accepted=false, reason="missing"`; **cold-start; continue** | Receipt `accepted=false, reason=<which>`; **cold-start; continue** | Receipt `accepted=true`; restore; continue |
| `disabled` (`--allow-cold-start`) | Receipt `accepted=false, reason="disabled_by_operator"`; no read attempted; cold-start; continue | (sidecar not read) | (sidecar not read) |

Across all three policies, the **write path is identical** — end-of-session checkpoints run regardless. `disabled` means "skip hydrate but still write so today seeds tomorrow."

### 4.3 Exit codes (additive)

| Code | Meaning | Source |
|---|---|---|
| 0 | Success | existing |
| 1 | Pre-flight halt (dirty tree, NTP fail, prior halt.flag, etc.) | existing |
| 2 | CLI/argparse input error | existing |
| 3 | Unhandled exception; recovery-flatten attempted | existing |
| **4** | **Hydrate validation failure under `require` policy** | **NEW** |

Receipt is written *before* exit 4 is raised, so post-mortem inspection is one `cat indicator_state_hydration.json` away.

### 4.4 Interaction with pre-flight

`pre_flight.py` (Step 2 of the runbook) **does not** check the sidecar. Pre-flight is git/NTP/halt-flag/positions; the sidecar check is a runtime concern inside `start`, immediately after `strategy.initialize()`. Rationale: pre-flight runs before the broker is even connected; sidecar validation needs `LiveConfig` + the constructed strategy. Localizing the failure to `start` keeps the existing pre-flight contract intact.

---

## 5. Tests

### 5.1 PR1 test inventory (under `PythonDataService/tests/engine/live/`)

| File | Tests | What |
|---|---|---|
| `test_nyse_calendar.py` | 7+ | Matrix from §6 — normal weekday, holiday-skip (Memorial Day 2026-05-25), weekend, early-close (Black Friday 2026-11-27), day-after-early-close, observed-holiday (2026 Independence Day Sat→Fri obs.), pathological weekend `session_start_ms`. Pure function; parameterized; no broker / engine deps. |
| `test_indicator_state_envelope.py` | ~8 | Envelope Pydantic round-trip including `Decimal`-as-string fields; schema_version `2` rejection; identity-field validators; payload pass-through. |
| `test_indicator_state_repo.py` | ~6 | Atomic write (tmp + `os.replace`), advisory lock acquire/release, missing-file read returns `None`, corrupt-JSON read raises schema error, "newer" comparison (`existing.last_consolidated_bar_end_ms >= new` → skip). |
| `test_spy_ema_persistence.py` | ~10 | `report_state_for_persistence` returns `None` when indicators not ready / position open / pending orders > 0 / open insights > 0; returns full payload when flat-and-ready. `restore_state_from_persistence` round-trips through a known state with `Decimal` equality (`atol=0`, exact). **The load-bearing test:** a post-restore strategy and a freshly-warmed-from-the-same-bars strategy produce **bit-identical** indicator outputs on the next 5 bars — this is what makes the warm-start equivalence claim auditable. |
| `test_live_context_hydrate.py` | ~12 | Six-row validation ladder under `require` (each failure path exits 4 with the right receipt reason); under `optional` (each failure path cold-starts with the right reason); under `disabled` (no read attempted, receipt written immediately). Plus the happy path. Fakes the strategy; uses `tmp_path` for the global sidecar. |
| `test_live_engine_checkpoint.py` | ~5 | Force-flat write fires `maybe_write` after barrier; skips when strategy reports non-restorable; graceful-shutdown `finally` fires; "newer" check refuses to overwrite a 15:55 sidecar with an 11:00-Ctrl+C state. Uses `FakeBroker`. |

**Existing replay parity gate** (`test_live_engine_replay.py`) must remain green. The hydrate hook is gated by `hydrate_policy != DISABLED`; replay tests pass `disabled` (no read) so the gate's `Decimal("0")` tolerance is unaffected.

### 5.2 PR2 test inventory

`tests/engine/live/test_live_engine_to_reconcile_producer.py`, 1 test:

1. Initialize a minimal `LiveEngine` against `FakeBroker` and a tiny in-memory bar source (≤ 30 bars).
2. Run it through a synthetic session end including force-flat → sidecar is written.
3. Run `reconcile.write_day_report` against the produced artifacts.
4. Assert: `day-0.parquet`/`json`/`hashes.json`/`md` all materialize; the hashes-json `indicator_state_hydration.json` entry is non-empty and its SHA-256 matches the file on disk; the committed Markdown receipt references the hydration receipt by SHA.

This is the CI version of "the dry-run is the integration test." Closes the contract gap §11 of `ibkr-integration-authority.md` calls out.

### 5.3 Project-scope verification before push

```
podman exec polygon-data-service python -m pytest /app/tests -k "not slow"
ruff check PythonDataService/app/ PythonDataService/tests/
```

Both run at project scope, not file scope (per `.claude/rules/python.md` lint scope and `.claude/rules/testing.md` pre-push test hygiene). Baseline established against `origin/master` before reporting any pre-existing failures.

---

## 6. NYSE calendar helper

### 6.1 Signature and contract

```python
def previous_completed_nyse_session_close_ms(session_start_ms: int) -> int:
    """
    Return int64 ms UTC for the regular-close timestamp of the most recent
    completed NYSE session strictly before the given session_start_ms.

    Honors early-close days (13:00 ET on the half-day schedule). Honors
    holidays (the prior session is the prior *trading* session, which may
    be 1, 2, or 3+ calendar days back).

    Implementation: pandas_market_calendars.get_calendar("NYSE").schedule(...)
    with a 14-day lookback window from session_start_ms; pick the latest
    schedule row whose 'market_close' < session_start_ms. Convert via
    int64 ms UTC.

    Raises NoSessionError if no completed session exists in the lookback
    window (pathological inputs only; should never happen in normal use).
    """
```

UTC ms in, UTC ms out. No timezone string ever escapes the function. Consumed only by check #3 of the validation ladder.

### 6.2 Test matrix

| Case | session_start (ET) | Expected previous close (ET) | Why |
|---|---|---|---|
| Tue after normal Mon | Tue 09:30 | Mon 16:00 | The happy path B2 cares about |
| Tue after holiday Mon (Memorial Day 2026-05-25) | Tue 2026-05-26 09:30 | Fri 2026-05-22 16:00 | Holiday-skip |
| Mon after normal Fri | Mon 09:30 | Fri 16:00 | Weekend-skip |
| Fri after Thanksgiving Thu | Fri 2026-11-27 13:00 | Wed 2026-11-25 16:00 | Thanksgiving holiday; Fri 11/27 is itself early-close |
| Day after early-close | Mon 2026-11-30 09:30 | Fri 2026-11-27 13:00 | Prior session was an early close |
| Independence Day Sat → observed Fri | Mon 2026-07-06 09:30 | Thu 2026-07-02 16:00 | Fri 2026-07-03 is the observed holiday |
| Sat / Sun session_start (pathological) | Sat 09:30 | raises `NoSessionError` | Runner shouldn't start on a non-session day; surface it |

---

## 7. Documentation changes (PR1)

| File | What changes |
|---|---|
| `docs/runbooks/ibkr-paper-dry-run.md` | Step 3 gets a new "Hydrate policy" subsection: `--hydrate-policy require\|optional\|disabled`, default `require`. Add Step 3a "seed day" describing the cold-start path. Add a "where indicator state lives" cross-reference to the new global path. |
| `docs/ibkr-integration-authority.md` | §6 "Live runtime" surface table adds `indicator_state.py` and `nyse_calendar.py` rows. §11 Phase 10 prereq row "indicator-state-persistence across restarts" flips to **SHIPPED** with PR# and date. §12 operational checklist item 3 gains a sub-step about hydrate-policy expectations on day 1 vs day 2. Bump "Last reviewed." |
| `PythonDataService/app/engine/live/README.md` | New section "Indicator state persistence" — file layout, envelope/payload, policy tri-state, where receipts go. ~30 lines. |
| `docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md` | This file. Committed in PR1. |

---

## 8. PR sequencing

```
master
  └─ feat/indicator-state-persistence-spy-ema       (PR1)
       indicator_state.py, nyse_calendar.py, strategy hooks, context wiring,
       engine call sites, run.py flags, reconcile manifest, tests, docs.
       Target merge: by EOD Fri 2026-05-15 or Sat 2026-05-16.
            │
            └─ test/live-to-reconcile-producer       (PR2)
                  Producer-consumer CI test. Fast, deterministic.
                  Target merge: Sat/Sun before market open Mon.
                       │
                       └─ (no code PR for the dry-run itself; operator branch is artifact-only)
                            Mon 2026-05-18 — seed day, hydrate_policy=optional
                            Tue 2026-05-19 — B2 dry-run gate, hydrate_policy=require
                            Receipt PR: docs/references/reconciliations/dry-run-2026-05-19/day-0.md
```

Each PR has its own branch off master per the branch-per-task rule. PR1 and PR2 must both be merged before the Tuesday gate.

---

## 9. Operator timeline

### 9.1 Monday 2026-05-18 — seed day (~10.5 h operator wall clock)

| Time (ET) | Action |
|---|---|
| 08:30 | Activate venv. Verify `.env` (`IBKR_MODE=paper`, `IBKR_PORT=4002`, `IBKR_CLIENT_ID=42`, `IBKR_READONLY=true`, `IBKR_HOST` override). Stop `polygon-data-service` container. Launch IB Gateway, log into DU paper account. |
| 09:00 | `init-ledger` → Monday's `run_id`. |
| 09:10 | `pre-flight`. All `OK`. |
| 09:20 | `start --hydrate-policy optional`. Sidecar missing → receipt `accepted=false, reason="missing"`; cold-start; continue. |
| 09:20→13:05 | Warmup. `[BAR]` heartbeat every minute; `decisions.parquet` empty; `snapshot=None`. |
| 13:05 onward | Indicators ready; decisions start writing. |
| 15:55 | Force-flat barrier fires. **First sidecar write** at `artifacts/live_state/spy_ema_crossover/SPY_15m.json`, `captured_reason="force_flat"`, `last_consolidated_bar_end_ms` = 15:45 close. |
| 16:00 | Graceful shutdown checkpoint. "Newer" check: equal → no overwrite. |
| 16:10 | Step 4 (reconcile). `day-0.md` committed under `docs/references/reconciliations/dry-run-seed-2026-05-18/`. |
| 16:15 | **Halt-flag cleanup.** If the strategy emitted any ENTER/EXIT signal post-warmup, reconcile will have written a `fill-class breach` `halt.flag` (`--readonly` means no broker fills came back to match the intents — expected per runbook Step 4, not a bug). Inspect `cat artifacts/live_runs/<monday_run_id>/halt.flag`; if the cause is the dry-run fill-class breach, delete the flag. Tuesday's pre-flight `no_halt_flag` check will otherwise refuse to proceed. |

### 9.2 Tuesday 2026-05-19 — B2 dry-run gate (the actual pass criterion)

| Time (ET) | Action |
|---|---|
| 09:00 | Activate venv. Same `.env`. Stop container. Launch IB Gateway. |
| 09:10 | `init-ledger` → Tuesday's `run_id` (different from Monday's). |
| 09:15 | `pre-flight`. All `OK` — no halt.flag from Monday. |
| 09:25 | `start --hydrate-policy require` (default; no flag needed). Reads Monday's sidecar; validation ladder passes all six checks; `accepted=true` receipt written; strategy restored. |
| 09:30 | RTH open. Engine consumes first 1-min bars; first 15-min consolidator emit at 09:45. |
| 09:45 | **First `decisions.parquet` row** (≠ 13:15). Load-bearing assertion of the gate. |
| 09:45→15:55 | Decisions every 15 min. `[BAR]` heartbeat every minute. |
| 15:55 | Force-flat. Sidecar overwritten with Tuesday's state. |
| 16:00 | End of session. |
| 16:10 | Step 4 (reconcile). `day-0.md` committed under `docs/references/reconciliations/dry-run-2026-05-19/`. **The deliverable.** Receipt is allowed to show a `fill-class breach` halt from `--readonly`-mode unmatched signals (see §1 pass criterion 4); any *other* halt cause fails the gate. |
| 16:15 | Same halt-flag cleanup as Monday — if breach halt fired, delete the flag so subsequent runs aren't blocked. Receipt PR opens with the dry-run-2026-05-19 day-0 Markdown. |

**Re-stated gate pass criteria:** §1 list, plus the timestamp of `decisions.parquet[0].bar_end_ms` is ≤ 09:45 ET converted to UTC ms.

---

## Appendix A — Decisions resolved during brainstorming (rejected alternatives)

For future readers: the brainstorming session considered and rejected:

- **A vs B vs C for persistence scope.** Picked **C** with the user's refinement "generic envelope, strategy-specific payload." A (strategy-only) risked a throwaway shape; B (generic framework) is too much framework before the second live strategy exists.
- **Sidecar location: per-`run_id` vs global vs both.** Picked **B + run-scoped hydration receipt** (global sidecar keyed by identity-tuple; per-run receipt for forensics) over per-run (operator gymnastics) and "both" (marginal value).
- **Mutating `run_ledger.json` with hydration metadata.** Rejected — the ledger is immutable identity and its on-disk SHA is part of the receipt trail. The hydration receipt is a sibling file instead.
- **Staleness expressed as "N minutes."** Rejected in favor of **NYSE-previous-completed-session** equality. The user pointed out that 96 h would accidentally accept Fri close → Tue open if Monday was a trading day but the process crashed before writing.
- **W3 periodic mid-session writes.** Rejected for PR1. Force-flat + graceful-shutdown only. Periodic writes would help mid-session crash recovery, which is explicitly out of scope.
- **Strict-block on `code_sha` / `strategy_spec_sha` mismatch (Q3b/Q3c).** Rejected in favor of **forensic-only (Q3a)**. Cosmetic PRs shouldn't force a 3 h 45 m re-warm; revisit if a math-changing PR ever silently invalidates a sidecar.
- **Pulling `commissionReport` / `equity_curve.parquet` into PR1.** Rejected; not gating for `--readonly` dry-run. Tracked as paper-week-day-1 prereqs separately.
