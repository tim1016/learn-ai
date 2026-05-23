# Docs Authority Index

**Purpose:** One map to load first. Tells an AI agent (or human) which docs are authoritative, which are supporting context, and which are archived. Load canonical docs for a domain before editing that domain's code.

**Agent convention:** Only docs marked `canonical` or `protected-canonical` should be used as implementation authority. `supporting` docs provide context and provenance. `archived` docs in `docs/archive/` carry status banners and must not be treated as authority.

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

## Canonical docs (load before editing the domain)

| Doc | Domain | Replaces / supersedes | Last reviewed |
|---|---|---|---|
| `docs/architecture/options-math-authorities.md` | Options math | `docs/architecture/options-routes-research.md` (cleanup record) | 2026-04-29 |
| `docs/architecture/ibkr-integration-tdd.md` | IBKR — design rationale ("why") | — | — |
| `docs/engine-persistence-authority.md` | Engine-side `BacktestEngine` runs persisting through `.NET` (parity gate + 6/8-category compare) | — | 2026-05-19 |
| `docs/feature-runner-authority.md` | Research Lab → Feature Runner | — | 2026-05-01 |
| `docs/ibkr-integration-authority.md` | IBKR integration snapshot | `docs/architecture/ibkr-integration-phase1/2/3.md` (archived) | 2026-05-04 |
| `docs/indicator-reliability-authority.md` | Indicator reliability methodology | — | — |
| `docs/ml-predictions-authority.md` | ML predictions (prediction-set artifact, StrategySpec wiring, QC parity infra) | — | 2026-05-12 |
| `docs/portfolio-management.md` | Portfolio management system | `docs/portfolio-system.md` (duplicate, disputed — PR2) | — |
| `docs/signal-engine-authority.md` | Signal engine | — | — |

---

## Supporting docs (useful context and provenance — not implementation authority)

| Doc | Domain | Notes |
|---|---|---|
| `docs/arch-overview.md` | System architecture overview | Review on next arch change |
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

## Archive (preserved for provenance — not implementation authority)

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
