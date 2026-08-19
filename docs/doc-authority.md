# Docs Authority Index

**Purpose:** One map to load first. Tells an AI agent (or human) which docs are authoritative, which are supporting context, and which are archived. Load canonical docs for a domain before editing that domain's code.

**Agent convention:** Only docs marked `canonical` or `protected-canonical` should be used as implementation authority. `supporting` docs provide context and provenance. `archived` docs in `docs/archive/` carry status banners and must not be treated as authority.

**2026-07-04 prune.** ~150 point-in-time working docs — completed implementation plans (`docs/superpowers/`, `docs/architecture/phases/`), session handoffs (`docs/handoffs/`), shipped-feature PRDs, and closed audit findings (`docs/audits/auto-research/findings/`, `docs/audits/vibe-coded-app-research/`) — were **hard-deleted to git history** rather than archived. Git history is their provenance record. Open defects lifted out of the deleted audit findings live in `docs/known-gaps.md`.

**2026-07-29 Bot Control authority reconciliation (superseded 2026-08-06).** The old AccountOwner implementation
snapshot, former "single canonical" operator runbook, cockpit/cohort plans, obsolete
runbooks, dated audits, and the five-bot handoff moved to `docs/archive/` with status
banners. `docs/bot-control-operator-manual.md` is now historical IBKR provenance;
`docs/broker-v2-operator-manual.md` is the **current operating authority** for Alpaca Broker V2,
rendered in-app at `/brokers/alpaca/manual`. It supersedes all older operating manuals,
quick procedures, runbooks, controller/cockpit plans, AccountOwner snapshots, and
point-in-time audits as behavior instructions. ADRs remain decision authority; code,
contracts, and focused tests remain implementation evidence.

**Note on AI rules:** Agent-facing rules live in `.claude/rules/` (Claude Code) — not in `.codex/` (no `.codex/` directory exists in this repo). `AGENTS.md` is the cross-agent entry point.

---

## Protected canonicals (never edit without owner sign-off)

| Doc | Domain | Owner | Last reviewed |
|---|---|---|---|
| `docs/CURRENT.md` | Short current-docs entry point | Tim | 2026-05-23 |
| `docs/agent-start-here.md` | Minimal AI-agent loading guide | Tim | 2026-05-23 |
| `docs/architecture/engine-authority-map.md` | Engine ownership map | Tim | 2026-05-04 |
| `docs/architecture/numerical-authority-migration-plan.md` | Math authority consolidation | Tim | 2026-05-04 |
| `docs/math-sources-of-truth.md` | All mathematical authorities | Tim | ongoing |
| `README.md` | Public-facing project intro | Tim | — |
| `AGENTS.md` | Agent operating instructions | Tim | — |
| `CLAUDE.md` | Claude Code project instructions | Tim | — |

---

## Architecture Decision Records (ADRs) — canonical decisions

`docs/architecture/adrs/` holds the durable "why" behind the platform's
control-plane, broker-safety, sizing, and operator-surface design. Each ADR is
canonical for its decision unless a later ADR supersedes it. Load the relevant
ADR before changing the behavior it governs. Several shipped PRDs pruned on
2026-07-04 (broker-session-mirror, daemon-diagnostics, trader-activity-deploy)
have their decision preserved here.

**Every newly accepted ADR carries a `Vocabulary:` line** in its header, naming
the `CONTEXT.md` section it added or stating that none is owed (ADR 0040
Decision 4). The obligation is **unconditional** — it is not "if the ADR
introduces domain language", because no check can decide that predicate. The
author decides whether vocabulary is owed; the line only has to be there, which
is what makes it grep-checkable alongside ADR 0039's `Status` gate. Applies from
the next accepted ADR forward; **existing ADRs are not back-filled**.

| ADR | Decision |
|---|---|
| 0001 | Control-plane substrate: JSON + Parquet, files canonical |
| 0002 | Shadow-mode enforcement at the adapter level (no submit) |
| 0003 | Operational topology: host venv |
| 0004 | Instance-addressed operator control plane (durable desired-state) |
| 0005 | Engine-authored readiness; two-altitude broker ownership |
| 0006 | Deploy is a host-daemon control-plane op; content-addressed `run_id` |
| 0007 | Host-daemon shared-secret auth |
| 0008 | Durable submit protocol: order identity + recovery |
| 0009 | Live sizing authority + provenance (the spec is not the live sizing authority) |
| 0010 | Operator-action contract: flatten / pause / stop |
| 0011 | Broker safety verdict: fail-closed, halt-on-transition, guarded Resume |
| 0012 | Strategy as signal generator; action-plan baseline |
| 0013 | Operator surface: judgment vs evidence (no frontend-derived verdicts) |
| 0014 | Broker-authored operator view: backend-rendered narratives |
| 0015 | Operator notice contract |
| 0016 | Bot-control trader-authored activity + deploy packages |
| 0017 | Per-bot lifecycle workbench: nodes explain, not gate |
| 0018 | Broker session mirror: client observatory + recovery |
| 0019 | Daemon diagnostics: composed control-plane authority |
| 0020 | Strategy validation is a strategy-level property; Deploy selects only |
| 0021 | Deploy launch defaults and the paper-execution guardrail envelope |
| 0022 | Temporal authority: canonical calendar and timestamps |
| 0023 | Strategy-validation human flag and Deploy re-home |
| 0024 | Bot event stream narrated-gate pipeline |
| 0025 | Single dominant headline notice placement |
| 0026 | Daily bot lifecycle: three states and the single-writer evaluator (§4 and its 2026-07-21 amendment superseded for Alpaca by ADR 0038; §4's derived-projection design was never built — read the banner before implementing) |
| 0027 | Operator blocker disposition taxonomy |
| 0028 | Bot Cockpit channel contracts (Clerk authority is superseded by ADR-0030) |
| 0029 | Live-session authority and IBKR capability |
| 0030 | Account Clerk authority is account-rooted and journal-canonical |
| 0031 | Cross-stack boundary selection and generated contracts |
| 0032 | Broker contract v2 and verbatim capture |
| 0033 | Account custody clocks and safety composition |
| 0035 | Alpaca Account Clerk event-sourced SQLite authority (supersedes JSONL-authority parts of 0001/0008/0030/0033 for Alpaca only) |
| 0036 | One flatness boundary (`abs(q) >= 1e-9`), owned by the backend; Angular holds no flatness verdict. Succeeds ADR 0013's no-frontend-derived-verdicts principle for numeric boundaries |
| 0037 | SQLite is the sole Alpaca custody authority; legacy JSONL retired (no activation fence = no authority, never a fallback). Completes ADR 0035 |
| 0038 | One bot control plane (Alpaca runner); the evaluator plane retires with IBKR bot-control. SQLite holds the duty facts it already fences; control intent stays file-backed so the stop latch outlives the Clerk. Supersedes ADR 0026 §4 for Alpaca |
| 0039 | An ADR's Status states the decision's standing, not the code's conformance. The ADR file is the sole status authority; one closed value (`Accepted`/`Proposed`/`Superseded`/`Retired`) per ADR, narrative moved out, CI-checkable |
| 0040 | `CONTEXT.md` is one glossary of the live trading/operator domain (not repo process); every section declares its lineage (`live` / `historical (ADR 0037/0038)` / `compatibility evidence (ADR 0038)` / `neutral`); the dangling §16.4 deferral is deleted; every newly accepted ADR carries a `Vocabulary:` line |
| 0041 | The operator manual's Button Reference (and Glossary tables) are generated from `OPERATOR_COPY`, not hand-written; "When available" prose is dropped in favour of the panel's runtime gate reasons; CI regenerate-and-diff, as for the OpenAPI/GraphQL snapshots |

---

## Canonical docs (load before editing the domain)

| Doc | Domain | Replaces / supersedes | Last reviewed |
|---|---|---|---|
| `docs/architecture/options-math-authorities.md` | Options math | `docs/architecture/options-routes-research.md` (cleanup record) | 2026-04-29 |
| `docs/broker-v2-operator-manual.md` | **Current** Alpaca Broker V2 operating authority | Legacy IBKR bot-control manual and point-in-time implementation plans | 2026-08-19 |
| `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md` | Focused Alpaca SQLite recovery/cutover subprocedure incorporated by the Broker V2 manual; no independent policy authority | — | 2026-08-06 |
| `docs/architecture/ibkr-integration-tdd.md` | IBKR read/evidence design rationale and retired-actuation record ("why") | Former Phase 3/4 submit/cancel design (retired by #1583) | 2026-08-19 |
| `docs/engine-persistence-authority.md` | Engine-side `BacktestEngine` runs persisting through `.NET` (parity gate + 6/8-category compare) | — | 2026-05-19 |
| `docs/feature-runner-authority.md` | Research Lab → Feature Runner | — | 2026-05-01 |
| `docs/ibkr-integration-authority.md` | Current read-only IBKR capability/account/order-history/market-data authority and retired-actuation boundary | `docs/architecture/ibkr-integration-phase1/2/3.md` (archived) and the pre-#1583 executable snapshot | 2026-08-19 |
| `docs/indicator-reliability-authority.md` | Indicator reliability methodology | — | — |
| `docs/ml-predictions-authority.md` | ML predictions (prediction-set artifact, StrategySpec wiring, QC parity infra) | — | 2026-05-12 |
| `docs/portfolio-management.md` | Portfolio management system | `docs/portfolio-system.md` (duplicate, disputed — PR2) | — |
| `docs/signal-engine-authority.md` | Signal engine | — | — |
| `docs/known-gaps.md` | Living open-defect backlog (what is still broken or deferred) | consolidates the pruned audit-finding trees | 2026-08-19 |

---

## Supporting docs (useful context and provenance — not implementation authority)

| Doc | Domain | Notes |
|---|---|---|
| `docs/arch-overview.md` | System architecture overview | Review on next arch change |
| `docs/bot-control-operator-manual.md` | Historical IBKR operator record | The UI, rendered manual, and catalog/control projections it describes are retired; use the Broker V2 manual for current behavior. |
| `docs/architecture/backtesting-engine-grounding-2026-04-26.md` | Engine diagnostic | Cited by `numerical-authority-migration-plan.md` |
| `docs/architecture/build-alpha-style-features-1-8-research-spec.md` | Alpha-style features | Features 6-8 may be unshipped — keep for traceability |
| `docs/architecture/edge-feature-design.md` | Edge feature engineering spec | Actionable engineering spec |
| `docs/architecture/edge-functionality-testing.md` | Edge testing guide | Engineering-focused |
| `docs/architecture/external-trading-platform-inspiration-2026-05-08.md` | Platform inspiration | Recent (2026-05-08) |
| `docs/architecture/iv-ownership-research.md` | IV pipeline research | ~32k tokens; authoritative research backing IV pipeline |
| `docs/architecture/options-cleanup-2026-04-29.md` | Options cleanup audit trail | Referenced by `options-math-authorities.md` |
| `docs/architecture/options-research.md` | Options implementation truth | — |
| `docs/architecture/options-routes-research.md` | Options routes cleanup record | Motivated Phase 1 of `options-math-authorities.md` |
| `docs/architecture/options-vol-platform-tdd.md` | Vol platform design | Contains actionable architectural decisions |
| `docs/architecture/sse-job-streams.md` | SSE job streams | SSE is in use per IBKR integration |
| `docs/audits/computational-fidelity-2026-04-22.md` | Timestamp ban motivation | Cited by `numerical-rigor.md` |
| `docs/audits/computational-fidelity-2026-04-22-addendum.md` | Timestamp ban motivation | Addendum cited by same rule |
| `docs/audits/structural-integrity-2026-04-22.md` | Known violation baseline | Historical context |
| `docs/audits/bot-control-8bot-call-graph-audit-2026-07-28.md` | Eight-bot call-graph evidence | Supporting investigation only; its open findings are tracked in `docs/known-gaps.md`, and it is never an operator procedure |
| `docs/audits/clerk-lineage-reachability-2026-08-17.md` | Alpaca-vs-IBKR Clerk lineage coupling and request reachability | Supporting evidence for wayfinder #1589 / ADR-0030/0032/0035 scope questions; distinguishes *executed* from merely *imported* |
| `docs/audits/alpaca-sqlite-sole-authority-retirement-2026-08-19.md` | ADR-0037 legacy Alpaca custody retirement and migration-gate receipt | Records structural deletion, preserved SQLite/IBKR evidence boundaries, and the explicit external-inventory prerequisite for #1618/#1656–#1660 |
| `docs/audits/numeric-authority-census-2026-08-17.md` | P&L / exposure / position implementation census | Supporting evidence for #1590 and ADR 0036; refutes the suspected FIFO duplication |
| `docs/audits/state-writer-census-2026-08-17.md` | Lifecycle and deploy state-writer inventory | Supporting evidence for #1591; input to the single-writer decision (#1598) |
| `docs/audits/submit-to-custody-fail-open-sweep-2026-08-17.md` | Alpaca submit-to-custody fail-open seams (5 confirmed, 9 refuted) | Supporting evidence for #1592; its confirmed seams are landed in `docs/known-gaps.md` via #1604 |
| `docs/audits/{three-bot-lifecycle-2026-07-23,2026-07-23-findings-corrected,eight-bot-deploy-ui-2026-07-27,deployment-validation-connectivity-incident-2026-07-27}.md` | Retired IBKR Bot Control validation and incident provenance | Historical evidence only; use the Broker V2 manual and `known-gaps.md` for current truth |
| `docs/bars-open-attribute-fix.md` | IBKR bar handling | Surgical bug-fix note for `ib_async.RealTimeBar.open_` |
| `docs/codex-phase-1-4-audit.md` | Retired IBKR Phases 1-4 code audit | Historical evidence only; current authority is `docs/ibkr-integration-authority.md` |
| `docs/engine-phase-1-2-refined-plan.md` | Strategy Lab deprecation lineage | Historical completion evidence; Strategy Lab is removed. |
| `docs/indicator-reliability-methodology.md` | Indicator reliability details | Backs `indicator-reliability-authority.md` |
| `docs/lean-engine-phase1-verification-report.md` | Engine correctness evidence | Evidential artifact |
| `docs/references/alpaca-sqlite-clerk-invariant-traceability.md` | ADR 0035 invariant-to-code/test evidence | Supports ADR 0035 and issue #1395 review |
| `docs/references/alpaca-sqlite-clerk-recovery-language.md` | Trader/operator action-language matrix | Backend-authored wording contract evidence; operator policy remains in the manuals |
| `docs/references/alpaca-sqlite-clerk-source-guarantees.md` | Alpaca guarantee-to-implementation matrix | Official-source provenance for adapter constraints |
| `docs/math-rigor.md` | Variance-time and FRED rate backing | Cited by `math-sources-of-truth.md` — keep for traceability |
| `docs/options-companion-format.md` | Options companion data format | Operational reference |
| `docs/options-cross-section-overview.md` | Options cross-section research | Useful pipeline context |
| `docs/portfolio-validation-plan.md` | Portfolio validation tests | 10 core tests; likely partially actionable — flag before archiving |
| `docs/process/agent-collaboration.md` | Multi-agent collaboration process | Operational |
| `docs/process/autonomous-decisions.md` | Autonomous decision-making process | Operational |
| `docs/process/pr-review-escalations.md` | PR escalation protocol | Operational |
| `docs/spy-lean-output-report.md` | SPY LEAN reconciliation | Evidential artifact |
| `docs/spy-lean-output/source-map.md` | LEAN output source map | Pairs with the report |
| `docs/superpowers/specs/2026-05-08-golden-fixtures-design.md` | Golden fixtures design spec | Recent (2026-05-08) |
| `docs/tv-polygon-validation-gotchas.md` | TradingView/Polygon alignment | Operational gotchas |
| `docs/validation-study-inventory.md` | Validation study inventory | Research provenance |

---

## Active / in-flight design (supporting — pruned once shipped + ADR-captured)

These describe work currently being built. They are design authority *for now*;
when the feature ships and an ADR or authority doc absorbs the decision, the PRD
is pruned to git history (as the broker-session-mirror and daemon-diagnostics
PRDs were on 2026-07-04). Verify status before trusting them as current.

| Doc | Domain |
|---|---|
| `docs/architecture/operator-notice-prd.md` | Operator notice contract implementation (ADR-0015) |
| `docs/prds/alpaca-account-clerk-sqlite-control-plane.md` | Proposed Alpaca Account Clerk SQLite authority, operation-first custody timeline, and fail-closed recovery; requires a follow-up ADR before implementation authority |

---

## Archive (preserved for provenance — not implementation authority)

Point-in-time docs normally prune to git history. The 2026-07-22 Clerk/controller
consolidation is a deliberate exception: its obsolete material has operational and
audit value, so it was moved to `docs/archive/` with explicit replacement pointers.

All files under `docs/archive/` carry a status banner. See `docs/archive/README.md` for the convention.

Key archive subdirectories:
- `docs/archive/plans/` — stale plans, phase snapshots, and conflict docs (archived in PR1+PR2)
- `docs/archive/reports/` — dated audits and historical implementation snapshots
- `docs/archive/runbooks/` — superseded operator/runbook material
- `docs/archive/prompts/` — verbatim LLM prompts stored as files
- `docs/archive/handoffs/` — per-session context dumps and demo notes
- `docs/archive/deleted-artifacts.md` — ledger of deleted raw outputs

The Clerk/controller batch is represented by the following archive roots:

- `docs/archive/runbooks/operator-architecture-and-runbook.md`
- `docs/archive/runbooks/bot-cockpit-traffic-controller-guide.md`
- `docs/archive/reports/bot-lifecycle-account-owner-authority.md`
- `docs/archive/plans/2026-07-20-concurrent-cohort-reconciliation-hardening.md`
- `docs/archive/reports/three-bot-concurrency-and-emergency-flatten-2026-07-17.md`

Previously disputed docs are now archived in `docs/archive/plans/` with banners naming their canonical replacement:
- `docs/archive/plans/black-scholes-implementation.md` → authority: `docs/architecture/options-math-authorities.md`
- `docs/archive/plans/lean-engine-implementation-plan.md` → authority: `docs/architecture/engine-authority-map.md`
- `docs/archive/plans/portfolio-system.md` → authority: `docs/portfolio-management.md`
