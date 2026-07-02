# ADR 0001 — Control-plane substrate stays JSON + Parquet + hash sidecars; Postgres is a future projection layer, not a substrate

**Status:** Accepted 2026-05-28
**Decision drivers:** persistent IBKR paper bot work (`docs/ibkr-paper-deployment-plan.md`); shadow VWAP strategy roadmap; Angular bot control control surface.
**Related:** ADR 0002 (shadow mode), ADR 0003 (operational topology), `docs/ibkr-paper-deployment-plan.md` § 16.

## Context

The pre-implementation design draft for the persistent paper-trading bot proposed a Postgres-backed control plane with `bot_registry`, `bot_desired_state`, `bot_command`, `bot_status`, `trade_decision`, `order_fill`, and `divergence` tables. The motivation was a database-authoritative kill switch readable from the existing .NET GraphQL / Angular stack, and queryable status for a future "Bot Status & Control" UI.

Reconnaissance against `master` at the time of the design lock surfaced that Phases 1–9 of the deployment plan had already shipped, with a different substrate:

- `run_ledger.json` — deterministic, hashed run identity (`run_id = sha256(canonical_json(payload))`), per PR #176 / Phase C-1.
- `decisions.parquet` (intent + indicator snapshot per bar), `executions.parquet` (broker fill events), `trades.parquet` (closed-trade ledger) — per `app/engine/live/artifacts.py`.
- `halt.flag` / `poisoned.flag` — atomic kill-switch / refuse-resume signals — per PRs #190, #193.
- `day-N.{md,json,parquet,hashes.json}` — daily three-way reconciliation report bundle with SHA-256 manifest of uncommitted artifacts — per PR #175 / Phase 9.
- Per-strategy stable indicator-state sidecar (`artifacts/live_state/spy_ema_crossover/SPY_15m.json`) for warmup skipping — per PR #239.

The substrate is already paying its rent: deterministic identity for reproducibility, immutable-ish artifacts for audit, hash manifests for tamper-evidence, column-typed Parquet for analytics.

The question was whether to (a) migrate to Postgres, (b) leave substrate alone and add only net-new capabilities, or (c) build a parallel Postgres control plane that mirrors the artifacts.

## Decision

**Keep JSON + Parquet + hash-sidecar artifacts canonical.** No Postgres in the live-runtime control plane.

If — and only if — the local file / SSE read-models for the Angular bot control nav become genuinely painful (specific named pain, not anticipated convenience), introduce Postgres as a **projection layer**: a downstream read-replica derived from the canonical artifacts, never the source of truth. Postgres never owns desired state, never owns the run ledger, never owns the audit trail. The artifacts remain the only thing that has to be backed up, reproduced, or restored.

This decision is independent of — and does not constrain — any future Postgres-backed app concerns that are not part of the live-runtime control plane (auth, multi-user identity, command audit log under an authenticated UI).

## Consequences

**Positive:**
- Zero migration cost. The next work targets the actual gaps (order-idempotency sidecar, shadow adapter, divergence taxonomies) instead of re-substrating a working system.
- Scientific posture preserved: deterministic run identity, hash-sealed artifact bundles, replayable evidence. A run is reproducible from a tarball of its `run_dir` plus the strategy spec hash.
- Schema growth is per-strategy parquet driven by `StrategySpec` (see deployment plan § 16 Resolution 5), not a Postgres migration with cross-strategy lock-step.
- The command channel (file-based per ADR-implied Resolution 7) composes naturally with the substrate: command files are themselves auditable artifacts hashable into the daily manifest.

**Negative:**
- Cross-strategy union queries ("show me every decision where shadow VWAP and executing EMA disagreed") require artifact joins in pandas / polars / DuckDB rather than a single SQL query. Acceptable: the join is one-liner column-typed work, and the divergence layer already exists as the natural home for cross-strategy comparison.
- Angular reactivity has to come from filesystem polling or SSE rather than a Postgres LISTEN/NOTIFY. Acceptable: existing `/api/live-runs` endpoints already serve this pattern, with mtime-signature LRU and inode-tracked deque already shipped.
- If the projection-layer trigger fires later, there's a one-time cost to introduce Postgres as a projection. That cost is bounded and downstream.

**Projection-layer trigger criteria** (Postgres becomes worthwhile when, not before):
1. Multiple concurrent UI consumers need consistent low-latency status reads under load that filesystem polling can't satisfy.
2. Cross-run analytic queries become a hot path in the operator's daily workflow (not just the reconciliation report).
3. Authenticated multi-operator command audit requires an identity/transaction store the file substrate can't cleanly express.

Until at least one of these fires, the substrate remains JSON + Parquet + hash sidecars.

## References

- `PythonDataService/app/engine/live/run_ledger.py` — canonical run identity.
- `PythonDataService/app/engine/live/artifacts.py` — Decision / Execution / Trade row pin.
- `PythonDataService/app/engine/live/reconcile.py` — three-way daily reconciler (Phase 9).
- `docs/ibkr-paper-deployment-plan.md` § 16 — design-lock round (this ADR is Resolution 1).
- `docs/ibkr-integration-authority.md` — authoritative live-runtime status as of `master`.
