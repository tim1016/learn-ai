---
name: auto-research-tick
description: Single entry point for the learn-ai auto-research loop. Reads docs/audits/auto-research/state.json to determine mode (baseline / nightly / dormant) and runs that mode's instructions. Use when invoked manually as `/auto-research-tick`, when fired by the nightly cron (after hardening), or when explicitly told to "run auto-research" or "do an auto-research tick". Auto-trigger only on those exact phrases — do not invoke this skill speculatively. Currently only baseline mode is implemented; nightly mode is dormant until the hardening gate in baseline-math-rigor.md is clear.
---

# Auto-research tick

The auto-research loop has two long-term modes (**baseline**, **nightly**) plus dormant states. This skill is the single entry point for both. The current mode lives in `docs/audits/auto-research/state.json`.

## Mode dispatch

Read `docs/audits/auto-research/state.json` first. Branch on `mode`:

| `mode` value | Action |
|---|---|
| `baseline-not-started` | Initialize the baseline (see §Baseline mode → kickoff). |
| `baseline-in-progress` | Resume the baseline from `cursor` (see §Baseline mode → resume). |
| `baseline-complete-awaiting-remediation` | The baseline is done. Print a one-paragraph status from the most recent run summary, list open P0/P1 findings, and exit. Do not start nightly mode. |
| `hardened-nightly` | **Not implemented yet.** When you see this, refuse and tell the user to manually invoke the (future) nightly skill — the baseline must be acknowledged complete by a human before nightly mode goes live. |

Anything else: stop and ask the user. Do not guess.

## Hard constraints (every mode)

These never change. If a constraint conflicts with the user's instruction in a single tick, stop and ask.

- **Read-only outside `docs/audits/auto-research/`.** No edits to production code, tests, fixtures, configs, or any file under `PythonDataService/`, `Backend/`, `Frontend/`, `references/`, `.claude/`, `.codex/` (if present), or any other source path.
- **No commits, no branches, no PRs, no pushes.** Even if the user has previously authorized commits in another context.
- **No new dependencies.** Not in `requirements-*.txt`, `*.csproj`, `package.json`, anywhere.
- **No regenerating golden fixtures.** Ever.
- **No loosening tolerances.** Ever.
- **No restarting containers.** If a container is required and down, record the check as `not run, container down: <container_name>` and continue. Do not run `./restart.sh` or `podman compose up`.
- **No external network fetches** (WebFetch / GitHub MCP) without an explicit user authorization recorded in the finding doc. Default to vendored references only: `references/`, `docs/references/`, and the framework docs already cited in `.claude/rules/`.
- **No running LEAN, no running QuantLib outside its existing Python wrapper, no installing anything.**
- **Targeted tests only.** Run `pytest -k <name>`, `dotnet test --filter <name>`, or `vitest run -t <name>` to verify a specific static finding. **Never the full test suite** during a baseline tick.
- **No "greenwashing" tests** — never write a test asserting current behavior just to make a finding "go away".

## Budget and termination

Per tick:

- Hard cap: **8 hours** of wall-clock work or until usage runs out (whichever first).
- Soft check every ~10 minutes: persist `state.json` and a partial run summary so a forced exit leaves clean state.
- On rate-limit / usage-cap detection: write the run summary, save state, exit cleanly. Do **not** retry; the next nightly cron (or next manual invocation) will resume.
- Exit early if a full sweep of the configured scope produces no new P0/P1/P2 findings — record that in the run summary as the "no new findings" terminator.

## State management

`docs/audits/auto-research/state.json` schema (v1):

```json
{
  "mode": "baseline-not-started | baseline-in-progress | baseline-complete-awaiting-remediation | hardened-nightly",
  "phase": 1,
  "phase_name": "inventory",
  "cursor": "PythonDataService/app/engine/indicators/sma.py",
  "last_run": "2026-05-06T03:14:00-04:00",
  "budget_used_seconds": 24300,
  "open_findings": ["F-0001", "F-0002"],
  "closed_findings": [],
  "runs": [
    {"date": "2026-05-06", "phases_touched": [1, 2], "opened": 7, "closed": 0, "stop_reason": "budget"}
  ],
  "baseline_started_at": "2026-05-06T23:00:00-04:00",
  "baseline_completed_at": null,
  "schema_version": 1
}
```

Write atomically: write to `state.json.tmp`, then rename. Persist after every finding open/close and at least every 10 minutes during long static scans.

## Finding doc schema

One file per P0/P1/P2 finding at `docs/audits/auto-research/findings/F-NNNN-<slug>.md`. P3 findings roll up into a single `findings/P3-rollup.md`.

```markdown
---
id: F-0001
severity: P0 | P1 | P2
status: open | repro-test-written | awaiting-human | deferred | fixed-verified | wontfix
area: inventory | python-authority | timestamp | provenance | fixture | tolerance | ingestion | wire | frontend-consumption | documentation
canonical_file: <path or "n/a">
reference: <vendored path, paper citation, or "missing">
first_seen: 2026-05-06
last_seen: 2026-05-06
phase: 1
---

## What
One paragraph in plain language.

## Where
File + line numbers. Multiple locations OK.

## Why this severity
Tied to the taxonomy in §9 of `baseline-math-rigor.md`.

## Reproduction
Command, expected vs observed. Skip if static-only.

## Suggested resolution (NOT auto-applied)
What a human would change. The skill writes this; the skill does NOT apply it.

## Provenance of the finding itself
Phase + cursor that produced it. Which reference (vendored path or commit) was consulted, if any.
```

`status` values the skill is allowed to set: `open`, `repro-test-written`, `awaiting-human`, `fixed-verified`. The skill must respect human-set `deferred` and `wontfix` and never reopen them silently.

Finding ID allocation: monotonically increasing across the lifetime of `findings/`. On startup, scan existing files and pick the next free ID.

## Deduplication

Before opening a finding, hash key: `(area, canonical_file_or_location, finding_type)`. If a finding with that key exists in `findings/` (any status), update its `last_seen` and add a note rather than creating a duplicate.

# ============================================================
# BASELINE MODE
# ============================================================

The baseline is a **one-shot** comprehensive audit producing recommendations only. Its full spec lives in `docs/audits/auto-research/baseline-math-rigor.md`. The skill's job is to fill that doc in and write the supporting `findings/` files.

## Kickoff (`mode: baseline-not-started`)

1. Set `mode` to `baseline-in-progress`, `phase` to `1`, `phase_name` to `inventory`, `baseline_started_at` to now (ISO 8601 with offset).
2. Update `baseline-math-rigor.md`: set `Status:` to `in-progress`, `Started:` to today, `Run count:` to 1.
3. Create today's run file at `docs/audits/auto-research/runs/YYYY-MM-DD.md` with a header skeleton.
4. Begin Phase 1.

## Resume (`mode: baseline-in-progress`)

1. Read `cursor`. Pick up from the next item in the current phase.
2. If `last_run` is more than ~36 hours ago, note the gap in today's run summary (this likely means cron didn't fire — surface it, don't hide it).
3. Increment `Run count` in `baseline-math-rigor.md`.
4. Append a new row to §7 (Runs) at the start of the run, finalize counts at the end.

## Phases (dependency-ordered, severity sub-sorted within)

The phase order matches §5 of `baseline-math-rigor.md`. Each phase has a static sweep first; targeted test runs only when a finding's severity warrants verification and the relevant container is up.

### Phase 1 — Canonical math inventory & source-of-truth gaps

**Goal:** Make `docs/math-sources-of-truth.md` correct and complete.

**Static sweep:**
- For every row in `docs/math-sources-of-truth.md`: confirm the `Canonical` file path exists; record findings for moved/renamed/deleted entries.
- Code-side scan to surface **undocumented canonical math** the registry doesn't list. Heuristics:
  - Files in `PythonDataService/app/engine/` not referenced anywhere in the registry
  - Files in `PythonDataService/app/services/` whose names match math concepts (`*_pricer*`, `*_greeks*`, `*_iv*`, `*solver*`, `*statistics*`, `*backtest*`, `*indicator*`, `*valuation*`)
  - Methods in `Backend/Services/Implementation/*.cs` that contain arithmetic over price / Greek / indicator values (grep for `Math.`, `decimal`, multiplication of model fields, etc.) and aren't registered as legacy duplicates
  - TS files matching `Frontend/src/app/utils/*math*`, `*pricer*`, `*greeks*`, `*calculator*`, `*compute*` not registered
- Confirm the same against `docs/architecture/engine-authority-map.md` and `docs/architecture/numerical-authority-migration-plan.md`. Findings record drift between any two of: registry, authority map, migration plan, code reality.

**Severity heuristics:**
- Unregistered canonical math doing live computation → **P1**
- Registered file moved/renamed without registry update → **P1**
- Registry entry stuck in `pending-migration` long after the migration plan's stated phase → **P2** (escalate to P1 if the duplicate is still serving live traffic)
- Authority-map / migration-plan drift vs registry → **P2**

**Output:** §2 of `baseline-math-rigor.md` populated; one finding per inventory mismatch.

### Phase 2 — Python math-authority violations (rule 5)

**Goal:** Confirm Python is the authority for canonical math; every duplicate has a parity test naming the canonical file.

**Static sweep:**
- For each registry row marked `legacy-ok` or `pending-migration`: open the duplicate file and confirm:
  - Its provenance block (or header comment) names the canonical Python file in `Canonical implementation`
  - It has a parity test named in `Validated against`, and that test exists
- For every `Backend/Services/Implementation/*.cs` containing arithmetic on price / Greek / indicator / statistic values: confirm it is either a passthrough to Python, an aggregation of Python-supplied numbers, or has a registered `legacy-ok` justification with parity test.
- Same sweep across `Frontend/src/app/**/*.ts` for `Math.`-heavy files outside the registered legacy set (currently `Frontend/src/app/utils/black-scholes.ts` is the registered exception).

**Severity heuristics:**
- A non-Python layer computing canonical math without a parity test → **P1** (escalate to **P0** if the resulting number is rendered to the user as authoritative).
- Duplicate that points at canonical but has no parity test → **P1**.
- Stale parity test (last green run > 6 months ago) → **P2**.

### Phase 3 — Timestamp boundary violations

**Goal:** Confirm `int64 ms UTC` is the only wire and storage format and the ban list is clean.

**Static sweep — Python:** grep `PythonDataService/` for:
- `datetime.utcnow`, `datetime.utcfromtimestamp`
- `datetime.now()` without `tz=`
- `pd.to_datetime(` without `utc=True` (multi-line aware)
- `.strftime(".*Z")` on a naive datetime
- Any field literally named `timestamp` / `ts` / `time` typed as `str` in a Pydantic model

**Static sweep — .NET:** grep `Backend/` for:
- `DateTime.Parse(`
- `DateTime.ParseExact(` *without* an explicit offset designator in the format string
- Field named `timestamp` / `ts` / `time` typed `string` or `DateTime` in a DTO
- Any new `DateTime` instance in a canonicalization or ingestion path

**Static sweep — TypeScript:** grep `Frontend/src/` for:
- `new Date(<string variable>)` where the input variable did not come from a literal full-ISO-with-tz
- `Date.parse(`
- Field named `timestamp` / `ts` / `time` typed `string` or `Date` in an interface that crosses the wire

**Severity heuristics:**
- Ban-list violation in an active ingestion path → **P0**
- Ban-list violation in a serialization or DTO path → **P0** if user-visible numbers depend on it; **P1** otherwise
- Ban-list violation in a non-canonicalization helper → **P2**
- DTO field typed `string` for a wire-side timestamp → **P1**

**Reference:** `.claude/rules/numerical-rigor.md` → "Timestamp rigor → Ban list".

### Phase 4 — Provenance & reference gaps

**Goal:** Every canonical math file carries the 4-field block: `Formula` / `Reference` / `Canonical implementation` / `Validated against`.

**Static sweep:**
- For every Python file listed as `Canonical` in `docs/math-sources-of-truth.md`: open it, look in the module docstring or the relevant function/class docstring for the four field labels.
- Same for `Backend/` and `Frontend/` canonical-or-legacy-ok files in the registry.
- Cross-check `docs/references/<name>.md` exists for every entry that names one.

**Severity heuristics:**
- Canonical file with no provenance block at all → **P1**
- Provenance block missing one or more fields → **P1** if `Reference` or `Validated against` is missing; **P2** otherwise
- `Validated against:` says `manually checked` / `looks right` / similar non-test phrasing → **P1**
- Reference cited but no corresponding `docs/references/<name>.md` → **P2**

### Phase 5 — Golden fixture gaps

**Goal:** Every canonical math has a fixture under `PythonDataService/tests/fixtures/golden/<name>/` with `input`, `output`, and `attribution`.

**Static sweep:**
- For every canonical Python math file in the registry: walk `tests/fixtures/golden/` looking for a directory whose name matches.
- For each found fixture: confirm presence of input, output, and an attribution file (`README.md` or `attribution.json`) that includes reference source, generation date, and regeneration command.
- Flag fixtures referenced in tests but missing on disk.
- Flag fixtures present on disk but referenced by no test.

**Severity heuristics:**
- Canonical math listed as `pending-fixture` in the registry → **P1**
- Canonical math NOT listed `pending-fixture` but fixture missing → **P0** (the registry lies)
- Fixture present, attribution missing → **P2**
- Fixture older than 12 months with no upstream reference change → **P3** (rollup)

### Phase 6 — Tolerance hygiene

**Goal:** Every float comparison declares `atol` and `rtol`; loosened tolerances are justified.

**Static sweep:**
- grep `PythonDataService/tests/` and `Backend.Tests/` and `Frontend/src/` for:
  - `np.allclose(`, `np.isclose(`, `assertAlmostEqual` — flag any without explicit `atol=` and `rtol=`
  - `assert.closeTo(` (Vitest/Jest) — flag without explicit precision
  - `Assert.Equal(.., .., delta:` — flag without an inline rationale comment
- For each loosened tolerance (`atol > 1e-9` for indicators, `atol > 1e-6` for PnL, `atol > 1e-6` or `rtol > 1e-6` for Greeks, `atol > 1e-10` for probabilities): require an inline justification comment OR a justification in `docs/references/<name>.md`. Flag if absent.

**Severity heuristics:**
- Bare `np.allclose(a, b)` with defaults → **P1**
- Loosened tolerance with no justification → **P1**
- Justification present but vague ("close enough", "after a few bars") → **P2**

### Phase 7 — Ingestion fidelity

**Goal:** Polygon / IBKR / FRED / any external feed → preserves timestamp, dtype, ordering, monotonicity, and surfaces duplicates rather than silencing them.

**Static sweep:**
- Find every external-API client in `PythonDataService/app/services/` (`polygon_*`, `ibkr_*`, `fred_*`, etc.).
- For each: verify the timestamp parsing path lands at `int64 ms UTC`, fail-fast on duplicates, fail-fast on non-monotonic sequences, no silent `drop_duplicates`, no forward-fill.
- Verify dtype handling: numeric fields stay numeric; precision loss (e.g., `round(x, 6)` before wire) is flagged.

**Severity heuristics:**
- Silent dedup or forward-fill in an ingestion path → **P0**
- Timestamp string passed through without canonicalization → **P0**
- `round(x, N)` before wire on a numeric value the consumer treats as authoritative → **P1**
- dtype coercion inferred by pandas with no explicit `dtype=` argument → **P2**

### Phase 8 — Wire fidelity (Python → Backend → GraphQL → Frontend)

**Goal:** A number computed in Python arrives at the Frontend signal unmangled.

**Static sweep:**
- For each canonical Python output that is consumed by Frontend (find via FastAPI router → `Backend/Services/*Service.cs` typed HttpClient → GraphQL resolver → Frontend service → component signal):
  - At each hop, the field types preserve the value (no `decimal` → `double` narrowing without justification, no `int64 ms` → `string` mutation, no `number` → `string` mutation in DTOs)
  - No layer recomputes the value
- Same trace for timestamp fields.

**Severity heuristics:**
- Recomputation of a canonical value in Backend or Frontend → **P0** (this *is* a math-authority violation; cross-link to a Phase 2 finding)
- Type narrowing on a numeric field that crosses the wire → **P1**
- Timestamp converted to string mid-pipeline (not at the display boundary) → **P0**

### Phase 9 — Frontend consumption / display-only violations

**Goal:** UI displays without recomputing; `DatePipe` / `toFixed` / chart formatters are display-only and never round-tripped.

**Static sweep:**
- grep `Frontend/src/` for `toFixed(`, `DatePipe`, `formatDate(`, `parseFloat(`, `Number(` on incoming numeric fields.
- Confirm the formatted result is rendered to the DOM only — never stored back in a signal that crosses the wire, never sent to a service.
- Flag any chart-formatter output that gets persisted or sent in a request.

**Severity heuristics:**
- Display-formatted value sent back over the wire → **P0**
- Display-formatted value stored in a signal that's read by another computation → **P1**
- Inconsistent formatting between layers (e.g., 6dp here, 4dp there) for the same canonical field → **P2** (rollup if cosmetic)

### Phase 10 — Documentation & auditability polish

**Goal:** Reference notes complete, reconciliation reports present, warmup documented per indicator.

**Static sweep:**
- For every entry in `docs/math-sources-of-truth.md` with a reference: confirm `docs/references/<name>.md` exists and includes commit/tag attribution.
- For every reconciled port: confirm `docs/references/reconciliations/<name>.md` exists.
- For every indicator: confirm warmup behavior is documented in the module docstring per `.claude/rules/numerical-rigor.md` → "Warmup rigor".

**Severity heuristics:**
- Missing reference note for a canonical math entry → **P2**
- Missing warmup docstring on an indicator → **P3** (rollup)
- Missing reconciliation report for a strategy claimed parity-pinned → **P2**

## Phase completion and exit

A phase is "complete" when:
- The static sweep has visited every item in scope for that phase
- Every finding has been written to `findings/` with `status: open` (or `awaiting-human` if it's been seen before)
- The relevant section of `baseline-math-rigor.md` (§3.x and the row in §1) is updated

Update `state.json`: increment `phase`, reset `cursor`, persist atomically.

## Baseline completion

When phase 10 finishes:

1. Re-run the executive summary in §0 of `baseline-math-rigor.md` from the current `findings/` state.
2. Populate §5 with concrete recommendations per group, ordered for unblocking.
3. Confirm the §6 hardening gate matches the actual state of the repo (some boxes may already be checked).
4. Set `mode` to `baseline-complete-awaiting-remediation`. Set `baseline_completed_at`.
5. Write a final run summary noting "baseline sweep complete".
6. Tell the user (in the run summary) that the next step is human remediation, and that the nightly cron should not be scheduled until the §6 gate is clear.

# ============================================================
# RUN SUMMARY TEMPLATE
# ============================================================

`docs/audits/auto-research/runs/YYYY-MM-DD.md`:

```markdown
# Auto-research run — YYYY-MM-DD

**Mode:** baseline-in-progress
**Started:** <iso>
**Ended:** <iso>
**Stop reason:** budget | usage-cap | no-new-findings | manual-interrupt | error
**Phases touched:** [1, 2]
**Cursor at end:** <path or n/a>

## Findings opened
- F-NNNN — Px — area — one-line summary

## Findings updated (last_seen bumped)
- F-NNNN — Px — area — one-line summary

## Findings closed
- F-NNNN — verified by …

## Skipped
- <area / file> — reason (e.g., container down, pending human input)

## Notes for the human
- Anything the human should look at before the next run.
```

## When you don't know

If the state file is missing, malformed, or describes a mode this version of the skill doesn't recognize: stop, write nothing, tell the user. The user owns the loop's lifecycle, not the skill.
