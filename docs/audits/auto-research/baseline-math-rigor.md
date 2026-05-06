# Math rigor baseline — learn-ai

**Status:** not-yet-started
**Started:** —
**Last updated:** —
**Run count:** 0
**Generator:** `.claude/skills/auto-research-tick` (baseline mode)

> This document is **frozen** once the baseline completes. Live state moves to a separate `current-state.md` after hardening. Do not edit this doc by hand once frozen except to append entries to the **Remediation log** at the bottom.

## 0. Executive summary

_Filled at the end of the first sweep and updated after every subsequent run that closes the loop on a phase. Running tallies live here; details live below._

| Severity | Open | Deferred | Closed | Total |
|---|---|---|---|---|
| P0 | — | — | — | — |
| P1 | — | — | — | — |
| P2 | — | — | — | — |
| P3 | — | — | — | — |

**Files audited:** —
**Files skipped:** — (reasons in §10)
**Phases complete:** —/10

## 1. Posture vs. `.claude/rules/numerical-rigor.md`

For each rule, one row: **holding** / **violated** / **partial**, with the count of supporting findings and a one-line summary.

| Rule | Holding? | Findings | Notes |
|---|---|---|---|
| Equivalence levels declared per port | — | — | — |
| Golden fixtures present and attributed | — | — | — |
| Tolerances explicit (no default `np.allclose`) | — | — | — |
| Tolerances justified when loosened | — | — | — |
| Timestamp canonical format `int64 ms UTC` at all boundaries | — | — | — |
| Timestamp ban-list clean (Python) | — | — | — |
| Timestamp ban-list clean (.NET) | — | — | — |
| Timestamp ban-list clean (TypeScript) | — | — | — |
| Fail-fast ingestion (no silent dedup / forward-fill) | — | — | — |
| Sovereignty (no runtime calls into `references/`) | — | — | — |
| Math Provenance Contract: 4-field block on canonical math | — | — | — |
| Single canonical per concept (no silent duplicates) | — | — | — |
| Authority hierarchy: Python is the home of canonical math (rule 5) | — | — | — |
| Warmup behavior documented per indicator | — | — | — |
| Reconciliation reports exist for reconciled ports | — | — | — |

## 2. Canonical math inventory

Cross-check between `docs/math-sources-of-truth.md` and the actual code.

- **Listed and present, canonical file matches:** —
- **Listed but canonical file missing or moved:** —
- **Unlisted canonical math discovered in code:** —
- **Listed as canonical but no provenance block on the file:** —
- **Listed with `pending-fixture` / `pending-migration` and still pending:** —

## 3. Findings index

Full per-finding files live in `docs/audits/auto-research/findings/`. Sort here is **dependency-ordered, severity sub-sorted** per the recommendation plan in §5.

### 3.1 Inventory & source-of-truth gaps
_(none yet)_

### 3.2 Python math-authority violations
_(none yet)_

### 3.3 Timestamp boundary violations
_(none yet)_

### 3.4 Provenance & reference gaps
_(none yet)_

### 3.5 Golden fixture gaps
_(none yet)_

### 3.6 Tolerance hygiene
_(none yet)_

### 3.7 Ingestion fidelity
_(none yet)_

### 3.8 Wire fidelity (Python → Backend → GraphQL → Frontend)
_(none yet)_

### 3.9 Frontend consumption / display-only violations
_(none yet)_

### 3.10 Documentation & auditability polish
_(none yet)_

## 4. Coverage map

What was audited, what was skipped, and why.

| Area | Scope | Status | Notes |
|---|---|---|---|
| `PythonDataService/app/engine/` | full | — | — |
| `PythonDataService/app/services/` | full | — | — |
| `PythonDataService/app/research/` | full | — | — |
| `PythonDataService/app/routers/` | wire-fidelity only | — | — |
| `PythonDataService/tests/` | tolerance + fixture audit | — | — |
| `Backend/Services/` | math-authority + wire | — | — |
| `Backend/Models/DTOs/` | wire fidelity (timestamps, dtypes) | — | — |
| `Backend.Tests/` | tolerance hygiene only | — | — |
| `Frontend/src/app/` | consumption + display-only | — | — |
| `references/` | vendored-immutability | — | — |
| `docs/references/` | reference-note completeness | — | — |
| `docs/math-sources-of-truth.md` | inventory cross-check | — | — |
| `docs/architecture/engine-authority-map.md` | drift vs reality | — | — |
| `docs/architecture/numerical-authority-migration-plan.md` | drift vs reality | — | — |
| `.claude/rules/` | self-consistency | — | — |
| `.codex/rules/` (if present) | self-consistency | — | — |

## 5. Recommendation plan (dependency-ordered)

The order matters: each step unblocks the next. Severity is the sub-sort within each step.

1. **Canonical math inventory / source-of-truth gaps** — fix `docs/math-sources-of-truth.md` first; everything downstream depends on a correct registry.
2. **Python math-authority violations** — every authoritative number must have its canonical in Python (rule 5) or carry an explicit, parity-tested justification.
3. **Timestamp boundary violations** — `int64 ms UTC` at every wire and storage point; ban-list clean across all layers.
4. **Provenance & reference gaps** — every canonical math file carries the 4-field block.
5. **Golden fixture gaps** — every canonical math has a fixture under `tests/fixtures/golden/<name>/` with attribution.
6. **Tolerance hygiene** — every float comparison declares `atol`/`rtol`; loosened tolerances are justified.
7. **Ingestion fidelity** — Polygon/IBKR ingestion preserves timestamp, dtype, ordering, monotonicity, and surfaces duplicates rather than silencing them.
8. **Wire fidelity** — Python → Backend → GraphQL → Frontend signal preserves the value without recomputation, narrowing, or string mutation.
9. **Frontend consumption / display-only violations** — UI displays without recomputing; `DatePipe` / `toFixed` / chart formatters are display-only and never round-tripped.
10. **Documentation & auditability polish** — reference notes complete, reconciliation reports present, warmup documented per indicator.

_Per-step recommendations populate after the first sweep._

## 6. Definition of "rigor restored" (the hardening gate)

The nightly auto-research cron is **not** scheduled until every box below is checked. This is the contract.

- [ ] All P0 findings closed (`fixed-verified`).
- [ ] All P1 findings closed or `deferred` with a documented reason in the per-finding doc.
- [ ] `docs/math-sources-of-truth.md` is regenerated, reviewed, and matches the actual code.
- [ ] Every canonical math file in the registry carries the 4-field provenance block (`Formula` / `Reference` / `Canonical implementation` / `Validated against`).
- [ ] Every entry marked `pending-fixture` in the registry has either a fixture or an explicit `deferred` row in this doc.
- [ ] Tolerance audit clean: no `np.allclose` / `np.isclose` without explicit `atol` and `rtol` in canonical-math tests; loosened tolerances justified in their docstring or test file.
- [ ] Timestamp ban-list grep clean across `PythonDataService/`, `Backend/`, `Frontend/src/`.
- [ ] Reference notes (`docs/references/<name>.md`) exist for every reconciled port.
- [ ] Warmup behavior is documented in the module docstring of every indicator.
- [ ] No runtime imports from `references/`.

## 7. Runs

| # | Date | Phase(s) touched | Findings opened | Findings closed | Notes |
|---|---|---|---|---|---|

Per-run summaries in `docs/audits/auto-research/runs/YYYY-MM-DD.md` (created on first run).

## 8. Methodology

- **Read-only.** This baseline does not edit production code, tests, or fixtures. The only writes are under `docs/audits/auto-research/`.
- **Vendored references only.** External fetches require explicit human approval (recorded in the relevant per-finding doc with URL + commit/tag + reason).
- **Static-first.** Targeted `pytest -k` / `dotnet test --filter` / `vitest run -t` only when a static finding needs verification. Full test suites only at the end of a run if writing tests is later authorized.
- **Container-aware.** If a container is required for a check and it's down, the check is recorded as `not run, container down` rather than skipped silently.
- **Resumable.** State lives in `state.json`; runs may span multiple nights. Findings are deduplicated by `(area, file, finding_type)`.

## 9. Severity taxonomy

- **P0** — Active numerical corruption or timestamp boundary violation in a live/canonical path; parity failure above documented tolerance on deployed canonical math.
- **P1** — Missing provenance or golden fixture for canonical math; tolerance loosened without justification; non-Python layer computing authoritative math without a parity-tested mirror.
- **P2** — Missing/weak attribution, stale fixture, incomplete reference note, weak edge-case coverage, suspicious dtype drift.
- **P3** — Documentation polish, naming, minor auditability improvements where math is currently correct. Rolled up in `findings/P3-rollup.md` rather than per-finding files.

## 10. Out of scope

The baseline does **not** examine:

- Frontend visual regression / styling
- General UI polish
- Performance / latency
- Security review
- Broad strategy profitability or correctness
- Live trading behavior
- Dependency upgrades
- Refactors unrelated to numerical fidelity

Strategy logic is in scope **only** when it reveals a math-authority violation, a timestamp violation, or a primitive-calculation parity issue.

## 11. Remediation log

_Append-only. One row per finding closed. The baseline is **frozen** once the hardening gate is clear; this section is the only one that grows after that point._

| Date | Finding | Closed by | Verification | Commit / PR |
|---|---|---|---|---|
