# Docs Authority Index

**Purpose:** One map to load first. Tells an AI agent (or human) which docs are authoritative, which are supporting context, and which are archived. Load canonical docs for a domain before editing that domain's code.

**Agent convention:** Only docs marked `canonical` or `protected-canonical` should be used as implementation authority. `supporting` docs provide context and provenance. `archived` docs in `docs/archive/` carry status banners and must not be treated as authority.

**2026-07-04 prune.** ~150 point-in-time working docs — completed implementation plans (`docs/superpowers/`, `docs/architecture/phases/`), session handoffs (`docs/handoffs/`), shipped-feature PRDs, and closed audit findings (`docs/audits/auto-research/findings/`, `docs/audits/vibe-coded-app-research/`) — were **hard-deleted to git history** rather than archived. Git history is their provenance record. Open defects lifted out of the deleted audit findings live in `docs/known-gaps.md`. Going forward, new point-in-time docs are pruned to git history, not archived; `docs/archive/` retains only the earlier archived set.

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
| 0031 | Cross-stack boundary selection and generated contracts |

---

## Canonical docs (load before editing the domain)

| Doc | Domain | Replaces / supersedes | Last reviewed |
|---|---|---|---|
| `docs/architecture/options-math-authorities.md` | Options math | `docs/architecture/options-routes-research.md` (cleanup record) | 2026-04-29 |
| `docs/bot-lifecycle-account-owner-authority.md` | Bot lifecycle and account ownership implementation snapshot | `docs/architecture/bot-lifecycle-account-owner-prd.md` for implementation authority; supersedes `docs/architecture/bot-lifecycle-gate-map.md` on current shipped behavior | 2026-06-30 |
| `docs/architecture/ibkr-integration-tdd.md` | IBKR — design rationale ("why") | — | — |
| `docs/engine-persistence-authority.md` | Engine-side `BacktestEngine` runs persisting through `.NET` (parity gate + 6/8-category compare) | — | 2026-05-19 |
| `docs/feature-runner-authority.md` | Research Lab → Feature Runner | — | 2026-05-01 |
| `docs/ibkr-integration-authority.md` | IBKR integration snapshot | `docs/architecture/ibkr-integration-phase1/2/3.md` (archived) | 2026-05-04 |
| `docs/indicator-reliability-authority.md` | Indicator reliability methodology | — | — |
| `docs/ml-predictions-authority.md` | ML predictions (prediction-set artifact, StrategySpec wiring, QC parity infra) | — | 2026-05-12 |
| `docs/portfolio-management.md` | Portfolio management system | `docs/portfolio-system.md` (duplicate, disputed — PR2) | — |
| `docs/signal-engine-authority.md` | Signal engine | — | — |
| `docs/operator-architecture-and-runbook.md` | Single canonical operator / paper-trading manual | `docs/broker-user-manual.html`/`.pdf` (pruned 2026-07-04) | 2026-07-04 |
| `docs/known-gaps.md` | Living open-defect backlog (what is still broken or deferred) | consolidates the pruned audit-finding trees | 2026-07-04 |

---

## Supporting docs (useful context and provenance — not implementation authority)

| Doc | Domain | Notes |
|---|---|---|
| `docs/arch-overview.md` | System architecture overview | Review on next arch change |
| `docs/architecture/backtesting-engine-grounding-2026-04-26.md` | Engine diagnostic | Cited by `numerical-authority-migration-plan.md` |
| `docs/architecture/bot-lifecycle-gate-map.md` | Bot lifecycle gate visualization | Supporting design map only; current implementation authority is `docs/bot-lifecycle-account-owner-authority.md` |
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
| `docs/bars-open-attribute-fix.md` | IBKR bar handling | Surgical bug-fix note for `ib_async.RealTimeBar.open_` |
| `docs/codex-phase-1-4-audit.md` | IBKR Phases 1-4 code audit | "Most recent code audit" per `ibkr-integration-authority.md` |
| `docs/engine-phase-1-2-refined-plan.md` | Strategy Lab deprecation lineage | Keep until Strategy Lab is fully removed |
| `docs/ibkr-paper-deployment-feedback.md` | IBKR paper-trading feedback | Live operational reference |
| `docs/ibkr-paper-deployment-plan.md` | IBKR paper-trading phases 6-10 | Phases 8/9/10 roadmap |
| `docs/indicator-reliability-methodology.md` | Indicator reliability details | Backs `indicator-reliability-authority.md` |
| `docs/lean-engine-phase1-verification-report.md` | Engine correctness evidence | Evidential artifact |
| `docs/math-rigor.md` | Variance-time and FRED rate backing | Cited by `math-sources-of-truth.md` — keep for traceability |
| `docs/options-companion-format.md` | Options companion data format | Operational reference |
| `docs/options-cross-section-overview.md` | Options cross-section research | Useful pipeline context |
| `docs/portfolio-validation-plan.md` | Portfolio validation tests | 10 core tests; likely partially actionable — flag before archiving |
| `docs/process/agent-collaboration.md` | Multi-agent collaboration process | Operational |
| `docs/process/autonomous-decisions.md` | Autonomous decision-making process | Operational |
| `docs/process/pr-review-escalations.md` | PR escalation protocol | Operational |
| `docs/runbooks/ibkr-paper-dry-run.md` | IBKR paper dry-run runbook | Referenced by IBKR authority doc |
| `docs/spy-lean-output-report.md` | SPY LEAN reconciliation | Evidential artifact |
| `docs/spy-lean-output/source-map.md` | LEAN output source map | Pairs with the report |
| `docs/superpowers/specs/2026-05-08-golden-fixtures-design.md` | Golden fixtures design spec | Recent (2026-05-08) |
| `docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md` | IBKR shadow deployment design | Recent (2026-05-08) |
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
| `docs/bot-lifecycle-workbench-redesign.md` | Per-bot workbench redesign (backs ADR-0017 + bot-lifecycle authority DoD) |
| `docs/architecture/bot-control-account-triage-reconciliation-prd.md` | Account triage + reconciliation pages (S0/S1 landed; S0 remainder open) |
| `docs/architecture/bot-control-attention-currentness-prd.md` | Attention currentness + proof alignment (initial slice landed) |
| `docs/architecture/bot-control-inspector-receipts-prd.md` | Node-scoped trader-friendly receipts rail |
| `docs/architecture/bot-truth-surface-prd.md` | Backend-authored bot truth surface + receipt rail |
| `docs/architecture/ibkr-account-truth-cross-bot-validation-prd.md` | IBKR account truth + cross-bot trade validation (MVP in progress) |
| `docs/architecture/operator-notice-prd.md` | Operator notice contract implementation (ADR-0015) |
| `docs/architecture/bot-control-critical-requirements-review.md` | Bot-control decisions / requirements review |

---

## Archive (preserved for provenance — not implementation authority)

Point-in-time working docs are now **pruned to git history**, not archived (see
the 2026-07-04 prune note above and `docs/archive/README.md`). The `docs/archive/`
tree below is the earlier archived set, frozen and preserved.

All files under `docs/archive/` carry a status banner. See `docs/archive/README.md` for the convention.

Key archive subdirectories:
- `docs/archive/plans/` — stale plans, phase snapshots, and conflict docs (archived in PR1+PR2)
- `docs/archive/prompts/` — verbatim LLM prompts stored as files
- `docs/archive/handoffs/` — per-session context dumps and demo notes
- `docs/archive/deleted-artifacts.md` — ledger of deleted raw outputs

Previously disputed docs are now archived in `docs/archive/plans/` with banners naming their canonical replacement:
- `docs/archive/plans/black-scholes-implementation.md` → authority: `docs/architecture/options-math-authorities.md`
- `docs/archive/plans/lean-engine-implementation-plan.md` → authority: `docs/architecture/engine-authority-map.md`
- `docs/archive/plans/portfolio-system.md` → authority: `docs/portfolio-management.md`
