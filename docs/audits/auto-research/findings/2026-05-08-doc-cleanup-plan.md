# Doc Cleanup Plan — 2026-05-08

**Type:** Documentation audit (plan-only)
**Scope:** All `*.md`, `*.pdf`, `*.txt`, `*.log` under repo root and `docs/`
**Files inventoried:** 174 (19 root-level + 155 under `docs/`)
**Protected files:** Not touched (AGENTS.md, README.md, docs/math-sources-of-truth.md, docs/architecture/engine-authority-map.md, docs/architecture/numerical-authority-migration-plan.md, docs/references/**, docs/audits/auto-research/findings/**, .claude/rules/**, .claude/skills/**)
**Method:** Read all protected canonicals in full first; skim non-protected docs for classification; git-log staleness check; explicit quote-and-cite for every claimed conflict.

> **Provenance note:** This plan was produced by a remote Claude Code agent run (Sonnet 4.6, routine `trig_0141aDdwumcszZkYtdVCZEQh`, fired 2026-05-09). The agent's session output was truncated mid-§2-Conflict-C during transcription; that gap is marked inline below. Sections 3 (proposed first cleanup PR) and 4 (do-not-touch-yet list) were not captured from the source paste and are listed as open. Reviewer suggestions captured 2026-05-09 (Tim) appear at the end of this document.

---

## 1. Classification table

### Root-level files

| File | Classification | Recommendation | Reason |
|------|---------------|----------------|--------|
| `AGENTS.md` | canonical | keep | Protected; agent operating instructions |
| `BUNDLE-README.md` | root-clutter | delete-after-approval | Setup script for unpacking a scaffold bundle — describes `docs/references/` as "(empty placeholder)" when it now contains 60+ files. Not needed after initial setup. |
| `CLAUDE.md` | canonical | keep | Primary project instructions for Claude Code |
| `README.md` | canonical | keep | Protected; public-facing project intro |
| `SpyEmaCrossoverOptions_V1_Challenges.md` | stale-handoff | move-to-archive | Appears to be a session-level challenge/design note for a specific strategy iteration — superseded by engine-authority-map.md and current engine implementation. |
| `TESTING.md` | duplicate | delete-after-approval | Duplicates `.claude/rules/testing.md` (authoritative per CLAUDE.md hierarchy) with slightly different commands. Two authoritative-looking testing guides for the same project creates ambiguity. |
| `arch-refactor-plan.md` | stale-plan | move-to-archive | Root-level architecture refactor plan; superseded by the shipped migrations documented in `docs/architecture/numerical-authority-migration-plan.md`. |
| `backend-test-output.txt` | test-output | delete-after-approval | Raw test runner stdout; no analytical content, pure agent-context noise. |
| `frontend-test-final.txt` | test-output | delete-after-approval | Raw test runner stdout; no analytical content. |
| `indicator-docs.txt` | test-output | delete-after-approval | Captured CLI output for indicator docs; stale artifact. |
| `level-2-implementation-plan.md` | stale-plan | move-to-archive | Implementation plan whose Phase 2 work is now reflected in engine-authority-map.md and the shipped codebase. |
| `options-chain-research-plan.md` | stale-plan | move-to-archive | Research plan for options-chain work; current authority is `docs/architecture/options-math-authorities.md`. |
| `research-plan-stat-rigor-update.md` | stale-plan | move-to-archive | Stat-rigor update plan; incorporated into `docs/audits/auto-research/baseline-math-rigor.md` and ongoing findings. |
| `research-plan.md` | stale-plan | move-to-archive | Early research plan; superseded by `docs/audits/auto-research/` auto-research state system. |
| `spy-lean-output-research-plan.md` | stale-plan | move-to-archive | LEAN output research plan; work completed — see `docs/spy-lean-output-report.md` and LEAN references in `references/lean/`. |
| `test-output.txt` | test-output | delete-after-approval | Raw test runner stdout. |
| `test-output2.txt` | test-output | delete-after-approval | Raw test runner stdout. |
| `test-output3.txt` | test-output | delete-after-approval | Raw test runner stdout. |
| `test-output4.txt` | test-output | delete-after-approval | Raw test runner stdout. |
| `validation_report_SPY_15m.pdf` | root-clutter | move-out-of-root | PDF report at repo root; should live under `docs/audits/` or `docs/references/` with a date-prefixed name. |

### docs/ top-level files

| File | Classification | Recommendation | Reason |
|------|---------------|----------------|--------|
| `docs/arch-overview.md` | supporting | keep | System architecture overview; aligns with current stack. Review on next arch change. |
| `docs/bars-open-attribute-fix.md` | supporting | keep | Specific bug-fix note for `ib_async.RealTimeBar.open_` attribute — surgical, useful context for IBKR bar handling. |
| `docs/black-scholes-implementation.md` | conflict | move-to-archive | Documents the TS `black-scholes.ts` implementation as a primary reference with no acknowledgement of its `[LEGACY-OK]` status (see §2, Conflict A). Misleads future agents into treating the TS pricer as a current authority. |
| `docs/code-review-top3-improvements.md` | stale-plan | move-to-archive | Dated 2026-03-31; reviews compose.yaml, image sizes, and ML patterns. All concerns either addressed or superseded by newer architecture decisions. |
| `docs/codex-phase-1-4-audit.md` | supporting | keep | "Most recent code audit" per `docs/ibkr-integration-authority.md`; tracks Phase 10 prereqs. Keep until superseded by a newer audit. |
| `docs/csv-data-pipeline-plan.md` | stale-plan | move-to-archive | CSV data pipeline plan; current data pipeline is Polygon-based per FastAPI service architecture. |
| `docs/data-lab-roadmap.md` | stale-plan | move-to-archive | Data lab roadmap; work materially completed — current state described in engine-authority-map.md and auto-research runs. |
| `docs/demo-2026-05-05.md` | stale-handoff | move-to-archive | Demo-day session artifact (2026-05-05); ephemeral event documentation. |
| `docs/design-handoff-data-lab-2026-04-24.md` | stale-handoff | move-to-archive | Per-session design handoff (2026-04-24); work completed; superseded by current implementation. |
| `docs/design-handoff-prompt.md` | prompt-snippet | move-to-archive | Raw LLM prompt for a design-session follow-up on the dark theme; not a design doc. |
| `docs/engine-phase-1-2-refined-plan.md` | stale-plan | keep | Referenced by engine-authority-map.md "Deprecated engines" section for Strategy Lab deprecation lineage. Keep until Strategy Lab is fully removed. |
| `docs/engine-tv-alignment-roadmap.md` | stale-plan | move-to-archive | TradingView/Polygon alignment roadmap; work completed per `docs/tv-polygon-validation-gotchas.md`. |
| `docs/feature-runner-authority.md` | canonical | keep | "Canonical reference" for Research Lab → Feature Runner; owner-update rule enforced; last reviewed 2026-05-01. |
| `docs/ibkr-integration-authority.md` | canonical | keep | "Canonical reference" for IBKR integration implementation snapshot; last reviewed 2026-05-04. |
| `docs/ibkr-paper-deployment-feedback.md` | supporting | keep | Feedback from paper-trading deployment; live operational reference for the IBKR paper phase. |
| `docs/ibkr-paper-deployment-plan.md` | supporting | keep | Referenced by `ibkr-integration-authority.md`; Phases 6/7 replay-parity + Phase 8/9/10 roadmap. |
| `docs/indicator-reliability-authority.md` | canonical | keep | Authority doc for indicator reliability methodology; owner-update rule. |
| `docs/indicator-reliability-methodology.md` | supporting | keep | Methodology details backing `indicator-reliability-authority.md`. |
| `docs/lean-engine-implementation-plan.md` | conflict | move-to-archive | Says "Build a NEW Python backtest engine … bit-exactly" as a future goal; the engine is now built and its authorities are documented in engine-authority-map.md (see §2, Conflict B). Keeping this creates false impression of an open build task. |
| `docs/lean-engine-phase1-verification-report.md` | supporting | keep | Verification report; evidential artifact for engine correctness. |
| `docs/lean-engine-phase2-plan.md` | stale-plan | move-to-archive | Phase 2 plan for the engine; work shipped per engine-authority-map.md. |
| `docs/lean-framework-integration-plan.md` | stale-plan | move-to-archive | Framework integration plan; integration complete per current repo state. |
| `docs/math-rigor.md` | supporting | keep | Referenced by `docs/math-sources-of-truth.md` (protected) as the source for variance-time interpolation (Upgrade 1) and FRED rate plan (Upgrade 4). Keeping it preserves traceability for those `pending-fixture` registry rows. |
| `docs/math-sources-of-truth.md` | canonical | keep | **PROTECTED.** Single source of truth for all mathematical authorities. |
| `docs/options-companion-format.md` | supporting | keep | Format spec for options companion data; operational reference for the options pipeline. |
| `docs/options-cross-section-overview.md` | supporting | keep | Options cross-section research overview; useful context for the options research pipeline. |
| `docs/overnight-codex-prompt.md` | prompt-snippet | move-to-archive | Verbatim LLM prompt for overnight unsupervised execution; not a design doc. Opening: "You are running unsupervised overnight…" |
| `docs/overnight-progress.md` | stale-handoff | move-to-archive | Overnight progress log from an IBKR paper-trading session; operational ephemera. |
| `docs/phase-1-2-deep-dive.md` | stale-plan | move-to-archive | Deep-dive planning doc for Phases 1 & 2; phases shipped. |
| `docs/portfolio-management.md` | canonical | keep | Titled "Portfolio Management System — Comprehensive Reference"; more detailed of the two portfolio system docs (18 resolvers, 12 mutations, interface-naming, full data model). Treat as the canonical portfolio system reference. |
| `docs/portfolio-system-plan.md` | stale-plan | move-to-archive | Implementation plan for the portfolio system; system now shipped. Superseded by `docs/portfolio-management.md`. |
| `docs/portfolio-system.md` | duplicate | move-to-archive | Shorter, less-detailed version of `docs/portfolio-management.md` covering the same architecture (see §2, Conflict C). Interface naming inconsistent with the .NET implementation (missing `I` prefix). |
| `docs/portfolio-upgrades-plan.md` | stale-plan | move-to-archive | Portfolio upgrade plan; upgrades likely shipped or folded into auto-research findings. |
| `docs/portfolio-validation-plan.md` | supporting | keep | Defines 10 core validation tests for the portfolio system; likely still partially actionable. Flag for human review before archiving. |
| `docs/process/agent-collaboration.md` | supporting | keep | Process guidance for multi-agent collaboration; kept under `docs/process/`. |
| `docs/process/autonomous-decisions.md` | supporting | keep | Process guidance for autonomous decision-making; operational. |
| `docs/process/pr-review-escalations.md` | supporting | keep | PR escalation protocol; operational. |
| `docs/session-handoff-2026-05-04.md` | stale-handoff | move-to-archive | Session handoff from 2026-05-04 IBKR paper-trading runtime work; ephemeral. |
| `docs/signal-engine-authority.md` | canonical | keep | Authority doc for the signal engine; owner-update rule enforced. |
| `docs/spy-lean-output-report.md` | supporting | keep | Reconciliation report for SPY LEAN output; evidential artifact. |
| `docs/spy-lean-output/source-map.md` | supporting | keep | Source map for LEAN output artifacts; pairs with the report. |
| `docs/strategy-lab-ux-improvement-plan.md` | stale-plan | move-to-archive | UX improvement plan for Strategy Lab, which is deprecated per engine-authority-map.md "Deprecated engines" table. |
| `docs/tv-polygon-validation-gotchas.md` | supporting | keep | Operational gotchas for TradingView/Polygon data alignment; still relevant. |
| `docs/validation-study-inventory.md` | supporting | keep | Inventory of validation studies; useful research provenance. |
| `docs/validation/SPY_ORB_Strategy_Plan.md` | stale-plan | move-to-archive | ORB strategy plan; strategy work either completed or superseded by engine-authority-map.md strategy registry. |

### docs/architecture/ files

| File | Classification | Recommendation | Reason |
|------|---------------|----------------|--------|
| `docs/architecture/backtesting-engine-grounding-2026-04-26.md` | supporting | keep | Diagnostic audit explicitly referenced by `numerical-authority-migration-plan.md` as its motivating artifact. |
| `docs/architecture/build-alpha-style-features-1-8-research-spec.md` | stale-plan | keep | Research spec dated 2026-05-06; Phases A-E1 shipped per engine-authority-map.md. However, features 6-8 (sensitivity sweeps, robustness, multi-symbol) appear unshipped — keep for traceability until engine-authority-map.md absorbs this scope. |
| `docs/architecture/design-handoff-edge-2026-04-25.md` | stale-handoff | move-to-archive | Per-session design handoff (2026-04-25); edge feature shipped; ephemeral. |
| `docs/architecture/design-handoff-engine-lab-2026-04-26.md` | stale-handoff | move-to-archive | Per-session design handoff (2026-04-26); engine lab phases shipped; ephemeral. |
| `docs/architecture/edge-design-temporary-docs.md` | stale-plan | move-to-archive | Filename contains "temporary"; ~8000-word LLM-generated TDD-style spec for an unrealized single-page feature ("The financial markets of the year 2026 demand…"). Aspirational prose, not an implementation record. Current edge authority is engine-authority-map.md. |
| `docs/architecture/edge-feature-design.md` | supporting | keep | Engineering spec for the edge feature with concrete component breakdowns; more actionable than `edge-design-temporary-docs.md`. |
| `docs/architecture/edge-functionality-testing.md` | supporting | keep | Functionality testing guide for edge; engineering-focused, not a prompt or handoff. |
| `docs/architecture/engine-authority-map.md` | canonical | keep | **PROTECTED.** Engine ownership map; last reviewed 2026-05-04. |
| `docs/architecture/external-trading-platform-inspiration-2026-05-08.md` | supporting | keep | Recent (2026-05-08) inspiration doc; may inform near-future decisions. |
| `docs/architecture/frontend-architecture-review-2026-04-23.md` | stale-plan | move-to-archive | Frontend architecture review from 2026-04-23; Angular 21 conventions now captured in `.claude/rules/angular.md`. |
| `docs/architecture/ibkr-integration-phase1.md` | stale-phase | move-to-archive | Frozen phase snapshot; `ibkr-integration-authority.md` explicitly calls it a "frozen snapshot of what each phase shipped." Move to `docs/audits/archive/` to preserve the record without cluttering the architecture index. |
| `docs/architecture/ibkr-integration-phase2.md` | stale-phase | move-to-archive | Same as above. |
| `docs/architecture/ibkr-integration-phase3.md` | stale-phase | move-to-archive | Same as above. |
| `docs/architecture/ibkr-integration-tdd.md` | canonical | keep | Design rationale doc explicitly cited by `ibkr-integration-authority.md`: "Read first to understand 'why.'" |
| `docs/architecture/iv-ownership-research.md` | supporting | keep | IV pipeline research doc; cited by `iv-research-chat-notes.md`; ~32k tokens per its own intro, but authoritative research backing the IV pipeline. |
| `docs/architecture/iv-research-chat-notes.md` | stale-handoff | move-to-archive | Verbatim chat-session notes from a Cowork session; meta-description of a conversation, not a design record. |
| `docs/architecture/iv-research-prompt.md` | prompt-snippet | move-to-archive | Verbatim paste prompt for an external quant review: "Paste the section below into ChatGPT…". Not a design doc. |
| `docs/architecture/numerical-authority-migration-plan.md` | canonical | keep | **PROTECTED.** Active migration plan for math authority consolidation. |
| `docs/architecture/options-cleanup-2026-04-29.md` | supporting | keep | Post-cleanup decisions log from the options-routes cleanup; referenced by `options-math-authorities.md` (its `Last reviewed` entry). |
| `docs/architecture/options-math-authorities.md` | canonical | keep | "Single source of truth, by calculation" for options math; actively maintained. |
| `docs/architecture/options-research.md` | supporting | keep | Research notes for options pipeline development; useful provenance. |
| `docs/architecture/options-routes-research.md` | supporting | keep | Referenced by `options-math-authorities.md` header as the cleanup doc that motivated Phase 1. |
| `docs/architecture/options-ux-design-prompt.md` | prompt-snippet | move-to-archive | Verbatim accumulator prompt: "When the cleanup is complete (Phase 8), paste the contents below into Claude Design…". Not a design doc. |
| `docs/architecture/options-vol-platform-tdd.md` | supporting | keep | TDD design for the volatility platform; contains architectural decisions that may still be actionable. |
| `docs/architecture/sse-job-streams.md` | supporting | keep | SSE job stream design; SSE is in use per the IBKR integration. |
| `docs/architecture/vol-surface-dashboard-plan.md` | stale-plan | move-to-archive | Vol surface dashboard plan; feature status unclear. Flag for human review before hard archival. |

### docs/audits/ files

| File | Classification | Recommendation | Reason |
|------|---------------|----------------|--------|
| `docs/audits/computational-fidelity-2026-04-22.md` | supporting | keep | Root diagnostic audit; cited by `numerical-rigor.md` as the source for the timestamp-ban motivation ("See docs/audits/computational-fidelity-2026-04-22.md § 2"). |
| `docs/audits/computational-fidelity-2026-04-22-addendum.md` | supporting | keep | Addendum to above audit; cited by same rule. |
| `docs/audits/python-tests-plan-2026-04-23.md` | stale-plan | move-to-archive | 2026-04-23 Python test improvement plan; absorbed into auto-research findings and the ongoing F-series. |
| `docs/audits/structural-integrity-2026-04-22.md` | supporting | keep | Structural integrity audit; still useful for historical context on which violations were known. |
| `docs/audits/auto-research/**` | canonical/supporting | keep | **PROTECTED findings** (F-series) and auto-research runs; active system. |

### docs/superpowers/specs/ files

| File | Classification | Recommendation | Reason |
|------|---------------|----------------|--------|
| `docs/superpowers/specs/2026-05-08-golden-fixtures-design.md` | supporting | keep | Recent (2026-05-08) design spec for golden fixtures; likely actively referenced. |
| `docs/superpowers/specs/2026-05-08-golden-fixtures-opus-handoff.md` | stale-handoff | move-to-archive | Handoff doc for an Opus overnight session; operational ephemera. |
| `docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md` | supporting | keep | Recent (2026-05-08) design for IBKR shadow deployment; likely actively referenced. |

### docs/runbooks/ and docs/process/ files

| File | Classification | Recommendation | Reason |
|------|---------------|----------------|--------|
| `docs/runbooks/ibkr-paper-dry-run.md` | supporting | keep | Operational runbook for the IBKR paper dry run; referenced by the IBKR authority doc. |
| `docs/process/agent-collaboration.md` | supporting | keep | Process doc; operational. |
| `docs/process/autonomous-decisions.md` | supporting | keep | Process doc; operational. |
| `docs/process/pr-review-escalations.md` | supporting | keep | PR escalation protocol; operational. |

### docs/references/** files

All files under `docs/references/` are **PROTECTED** — per-port attribution notes, golden fixture records, and reconciliation reports. No recommendations made here.

---

## 2. Direct conflicts against canonical docs

### Conflict A — `docs/black-scholes-implementation.md` vs `docs/architecture/numerical-authority-migration-plan.md`

**File 1:** `docs/black-scholes-implementation.md`, header (line 1–3):

```text
Black-Scholes Implementation & Current P&L Curve
Reference document for verifying the math in Frontend/src/app/utils/black-scholes.ts
and the chart data pipeline in options-strategy-lab.component.ts.
```

**File 2:** `docs/architecture/numerical-authority-migration-plan.md`, Phase 1.3 (lines 18–22):

```text
Phase 1.3 shipped (2026-04-27). Frontend/src/app/utils/black-scholes.ts header
upgraded to [LEGACY-OK — RENDER-HELPER ONLY, NO NEW CALLERS]. Two intentional callers
documented … Both callers produce exploratory feedback, not numbers users compare against
another number; the canonical authorities bs_greeks.py + quantlib_pricer.py remain
the only math authorities (parity-pinned to atol=1e-10).
```

**Conflict:** `black-scholes-implementation.md` presents `black-scholes.ts` as "the math" to verify, with no acknowledgement that the file is now `[LEGACY-OK]` and that `bs_greeks.py` / `quantlib_pricer.py` are the canonical authorities. A future agent loading this doc would treat the TS pricer as a valid reference implementation.

**Resolution:** The migration plan wins (it is protected; it records a shipped phase). `black-scholes-implementation.md` should be moved to archive. If kept, it needs a prominent header note pointing to `options-math-authorities.md` as the current authority.

---

### Conflict B — `docs/lean-engine-implementation-plan.md` vs `docs/architecture/engine-authority-map.md`

**File 1:** `docs/lean-engine-implementation-plan.md`, header (lines 1–9):

```text
LEAN-Compatible Backtest Engine — Implementation Plan
Goal
Build a new Python backtest engine inside PythonDataService/app/engine/ that
reproduces the trades produced by QuantConnect LEAN's SpyEmaCrossoverAlgorithm
bit-exactly as the first validation milestone, then generalize from that foundation.

This document supersedes the speculative phasing in lean-pipeline-research-plan.md
with concrete decisions, file layouts, and a validation procedure…
```

**File 2:** `docs/architecture/engine-authority-map.md` (protected), "The map" table, first row:

```text
| Interactive backtest (stocks, indicator strategies) | Engine Lab | PythonDataService/app/engine/
via router app/routers/engine.py | Canonical event-driven engine; LEAN-ported semantics |
canonical — vendored LEAN reference at references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/
```

**Conflict:** The implementation plan frames the engine as a future build task ("Build a new Python backtest engine"). The engine-authority-map (protected, Status: Active, Last reviewed 2026-05-04) documents the same engine as **canonical** and fully shipped. Keeping the plan in the active docs tree misleads future agents into thinking the build is still open work.

**Resolution:** engine-authority-map.md wins (protected, more recent, records shipped state). `lean-engine-implementation-plan.md` should be archived under `docs/audits/archive/`.

---

### Conflict C — `docs/portfolio-system.md` vs `docs/portfolio-management.md`

**File 1:** `docs/portfolio-system.md`, heading + service names (lines 1, 32–37):

```text
Portfolio Management System
…
│ │ PortfolioService ──► PositionEngine (FIFO lots) │
│ │ ValuationService ──► live price lookup + multiplier math │
│ │ SnapshotService ──► equity curve, metrics (Sharpe, etc.) │
│ │ RiskService ──► dollar delta, vega, rule evaluation │
│ │ ReconciliationService ──► drift detection + auto-fix │
│ │ StrategyAttributionService ──► backtest import + PnL split │
```

**File 2:** `docs/portfolio-management.md`, heading + service names (lines 1, 36–43):

```text
Portfolio Management System — Comprehensive Reference
…
│ │ IPortfolioService ────────► Account/Order/Trade CRUD │
│ │ IPositionEngine ─────────► FIFO lot allocation + rebuild │
│ │ IPortfolioValuationService ► live price lookup + MTM │
│ │ ISnapshotService ────────► equity curve + performance metrics │
│ │ IPortfolioRiskService ───► dollar delta, vega, scenarios │
│ │ IPortfolioReconciliationService ► drift detection + auto-fix │
│ │ IStrategyAttributionService ──► backtest import + PnL split │
```

**Conflict:** `portfolio-system.md` uses concrete class names without the `I` interface prefix; `portfolio-management.md` uses interface names (matching the .NET `Backend/` code conventions per `.claude/rules/dotnet.md`). The two docs describe the same system at different levels of detail and …

> **[SOURCE TRUNCATED]** The transcribed source paste ended here. The remainder of Conflict C's resolution and any additional conflicts (D, E, …) were not captured. The classification table (§1) recommends `portfolio-management.md` as the kept canonical and `portfolio-system.md` as the duplicate to move-to-archive — which is consistent with where this analysis was heading — but the agent's final written resolution is missing.

---

## 3. Proposed first cleanup PR (10–20 lowest-risk items)

> **[NOT CAPTURED]** This section was not present in the transcribed source paste. See §6 below for reviewer-proposed PR1 scope, which narrows the agent's intent based on the §1 / §2 content that was captured.

---

## 4. Do-not-touch-yet list

> **[NOT CAPTURED]** This section was not present in the transcribed source paste. The classification table flagged the following entries with explicit "human review" caveats which together form a reasonable first-pass do-not-touch list:
>
> - `TESTING.md` (root) — duplicate of `.claude/rules/testing.md`, but human-vs-agent audience overlap (see reviewer note in §6)
> - `docs/portfolio-validation-plan.md` — defines 10 validation tests, "likely still partially actionable"
> - `docs/architecture/build-alpha-style-features-1-8-research-spec.md` — features 6-8 likely unshipped
> - `docs/architecture/vol-surface-dashboard-plan.md` — feature status unclear
> - `docs/black-scholes-implementation.md`, `docs/lean-engine-implementation-plan.md`, `docs/portfolio-system.md` — the three conflict files; reviewer wants these in PR2 with explicit canonical replacements
>
> Treat this list as provisional until a follow-up audit fills in §3 and §4 properly.

---

## 5. Open items / gaps in this report

1. §2 Conflict C resolution truncated — needs the agent's recommendation written out (the `portfolio-management.md`-wins direction is implied by §1 but not explicit here).
2. §3 (proposed first cleanup PR) not captured — reviewer suggestions in §6 narrow this.
3. §4 (do-not-touch-yet list) not captured — placeholder above is reconstructed from §1 caveats.
4. Re-running the audit against the fresh `master` and capturing the full output would close all three gaps.

---

## 6. Reviewer suggestions — 2026-05-09 (Tim)

> Captured verbatim from in-conversation review of the plan above. These shape PR2 (the actual cleanup) more than PR1 (the file write of this audit), but are recorded here so the next agent sees them.

This is a strong plan. Main suggestion: **make the cleanup AI-friendly by demoting docs before deleting them.** Deletion is fine for raw test output, but plans/prompts/handoffs can contain useful provenance. Archive + explicit status banners gives agents a clear hierarchy without erasing context.

A few concrete tweaks:

**Add a docs authority index.** `docs/README.md` or `docs/doc-authority.md` with a Markdown table:

`Doc | Status | Authority level | Owner | Replaces | Replaced by | Last reviewed`

This keeps `.md` as the source of truth and gives AI one obvious map to load first.

**Standardize status banners** at the top of archived/stale files:

```markdown
> **Status:** Archived / superseded.
> **Do not use as implementation authority.**
> Current authority: `docs/architecture/engine-authority-map.md`.
> Archived because: shipped phase snapshot.
```

This is friendlier to AI than only moving files.

**Prefer `docs/archive/` over `docs/audits/archive/`** unless the archived thing is truly an audit artifact. Plans, prompts, handoffs, and stale architecture docs are broader than audits. Maybe:

- `docs/archive/plans/`
- `docs/archive/prompts/`
- `docs/archive/handoffs/`
- `docs/archive/reports/`

**Do not delete `TESTING.md` yet.** Agree with the do-not-touch call. Testing docs often serve humans while `.claude` / `.codex` rules serve agents. Better path: merge any still-correct human commands into `TESTING.md`, then make `.codex/rules/testing.md` point to it or vice versa.

**Resolve `.claude` vs `.codex` naming before cleanup.** Repo instructions mention `.codex/rules/**`, but the plan protects `.claude/rules/**`. If both exist, define which is canonical. If only `.claude` exists on master, update AGENTS / plan language so future agents don't chase a phantom path.

**For moved files, run a link check** or at least search for references before the PR. If `engine-authority-map.md` or a protected doc links to a moved file, update the link in the same PR.

**For "delete-after-approval"**, consider replacing root clutter with one Markdown ledger: `docs/archive/deleted-artifacts.md` listing deleted raw outputs and why. Keeps the decision auditable while removing context noise.

**Make PR1 even narrower:** delete raw `*.txt` test outputs, archive obvious prompts/handoffs, add the archive banner convention, and add the docs authority index. Save disputed docs (Black-Scholes, portfolio, LEAN plans) for PR2, where the canonical replacements can be made explicit.
