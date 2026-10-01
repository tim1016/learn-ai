# Kill list — authority docs, runbooks, READMEs and the glossary (#2713)

Part of map #2700. Plan only: nothing here is deleted by this branch.

- **Read at:** `87b8e261021ec673c2e7c80448c0973bccd45378` (`origin/master` when this list was built, which is the map's charting SHA). Every `file:line` below is at that SHA.
- **Area:** the 22 top-level `docs/*.md`, `docs/runbooks/` (16 files), `README.md`, `TESTING.md`, `CONTEXT.md`, `Frontend/POLYGON_INTEGRATION.md`, and the `*.md` files under `PythonDataService/app/`, `Frontend/src/` and `scripts/`.

## How the evidence was gathered

1. **Inbound links.** For each doc, `git grep -l -F <basename>` across all tracked files (the full path for READMEs), excluding the doc itself. Each hit is sorted by where it comes from: CLAUDE/AGENTS, rule, skill, README, Frontend code or served asset, code, test, CI, ADR, kept doc, or one-off doc.
2. **Dead paths.** A script listed every repo path a kept doc names that is no longer tracked, which is how the stale sections below were found.
3. **CONTEXT terms.** For each `CONTEXT.md` section, a script checked whether its backticked identifiers still appear in running code (`PythonDataService/app`, `PythonDataService/scripts`, `Frontend/src/app`, `Backend` minus migrations, `deploy/`, `scripts/`). Tests and docs were not counted.

**How the link rule is read here.** Three kinds of link do not make a doc "read":
- a row in an index (`docs/doc-authority.md`, `docs/CURRENT.md`), because an index lists everything by design;
- a link from a one-off doc (plans, PRDs, audits, research), because #2711 is cutting those;
- links between docs that are both being cut;
- a key in the docs checker's classification map, because that only says what class a file is, not that anyone reads it.

An ADR link does count. ADRs are assumed kept unless #2712 lists them.

## Whole-file cuts

| Path | Kind | Evidence |
|---|---|---|
| `docs/CURRENT.md` | sediment (index loop) | Linked only from `docs/agent-start-here.md`, `docs/doc-authority.md:57` and the checker's hard-coded class map (`scripts/check_documentation_contract.py:29`). Nothing in CLAUDE/AGENTS, rules, skills, README, UI or code links it. It restates `doc-authority.md`, and its "Current Cleanup Notes" are prune history. |
| `docs/agent-start-here.md` | sediment (index loop) | Linked only from `docs/doc-authority.md:58` and `scripts/check_documentation_contract.py:30`. Its load list (`AGENTS.md` → `CURRENT.md` → rules) duplicates what `AGENTS.md`/`CLAUDE.md` already tell an agent. |
| `docs/sprite-art-provenance.md` | retired + unlinked | No inbound refs. It describes the art in `swordsman-sprite-pack.ts`, which was deleted in `263cda16` (#1853, Design Lab retired). No other tracked file has "sprite" in its path. |
| `docs/feature-runner-authority.md` | unlinked | Linked only from `doc-authority.md:166` (index) and `indicator-reliability-authority.md:323` (also cut). The live component `Frontend/src/app/components/research-lab/feature-runner/feature-runner.component.ts` does not link it. |
| `docs/indicator-reliability-authority.md` | unlinked duplicate | Linked only from `doc-authority.md:168` and `feature-runner-authority.md:761`. The read copy is the served `docs/indicator-reliability-methodology.md` (`Frontend/src/app/app.routes.ts:283`, `research-lab/indicator-reliability/doc-refs.ts:9`). |
| `docs/portfolio-management.md` | unlinked | Linked only from `doc-authority.md:170`. The portfolio code itself is live (`Backend/GraphQL/PortfolioQuery.cs`), so this cut rests on the link rule alone. |
| `docs/portfolio-validation-plan.md` | unlinked plan | Linked only from `doc-authority.md:205`, whose own note reads "likely partially actionable — flag before archiving". |
| `docs/options-cross-section-overview.md` | no longer true; its only live link is false | §1.1 documents `bs_solver.py`, which is not tracked (the solver is `PythonDataService/app/volatility/solver.py`). §6 lists `OptionsHistoryComponent` at `/options-history`; neither exists. Its one kept-doc reference, `docs/options-companion-format.md:189`, says the overview "tracks" the surface-IV cross-check, but the overview never mentions "surface". |
| `docs/runbooks/clerk-transaction-projection-rebuild.md` | retired procedure | No inbound refs. The procedure calls `rebuild_account_transaction_projection`, which is gone: `PythonDataService/app/services/clerk_transaction_projection.py:3-11` says the IBKR projection was retired in PR-A of #1813, and the module is now one exception class. It also cites `tests/services/test_clerk_transaction_projection.py` (`:96`), which is not tracked. |
| `docs/runbooks/alpaca-clerk-disposable-paper-clean-slate.md` | unlinked duplicate | No inbound refs. Its own line 3 claims `add-an-alpaca-account.md` §8 links here; that runbook has no such link. The body only defers to `alpaca-sqlite-clerk-recovery-and-cutover.md:92` ("Disposable Paper developer reset"), which holds the actual procedure. |
| `docs/runbooks/fleet-e-conformance-and-operator-acceptance.md` | unlinked | No inbound refs. It is a Delivery E acceptance template for `scripts.run_broker_fleet_conformance` (see the #2707 pointer below). |
| `TESTING.md` | unlinked | No tracked file mentions it. Its run commands repeat `.claude/CLAUDE.md` "Running Tests" and `.claude/rules/testing.md`. |
| `Frontend/POLYGON_INTEGRATION.md` | unlinked + no longer true | No inbound refs. Its project tree names `src/app/services/polygon.service.ts`, which is not tracked, and nothing under `Frontend/src` imports `@polygon.io/client-js`. |
| `PythonDataService/app/engine/live/README.md` | unlinked + retired | No inbound refs. It is titled "IBKR Paper Live Runtime", and the IBKR execution runtime was retired in #1583 (`CONTEXT.md:1100-1103`, ADR 0038). |
| `PythonDataService/app/volatility/docs/DASHBOARD_PLAN.md` | unlinked plan | No inbound refs. No route or static mount serves the dashboard it plans (`dashboard.html` and friends in the same folder); see the #2704 pointer. |
| `PythonDataService/app/research/options/README.md` | unlinked | No inbound refs. The content is accurate, so this cut rests on the link rule alone. See question 2 for the map. |

## Section cuts

| Path#section | Kind | Evidence |
|---|---|---|
| `docs/known-gaps.md` §2 (L69-80) | closed record | Says "No known-open items" (`:71`); git history is named as the record. |
| `docs/known-gaps.md` §3 (L82-91) | closed record | Says "No open broker gaps remain." (`:91`). |
| `docs/known-gaps.md` §4 (L93-107) | retired behavior | It defers decisions for the lsof/per-bot-socket session mirror. `lsof` appears in no tracked non-doc file except `CONTEXT.md`, and `CONTEXT.md:1100-1102` records socket enumeration and orphan remediation as retired in #1583. |
| `docs/known-gaps.md` §6, ML-V-002 sentence (L150-151) | fixed | The "missing" provenance blocks are now present: `app/research/parity/qc_reconciler.py:13-31` and `app/research/ml/artifact.py:10-22`. |
| `docs/known-gaps.md` §7 (L154-159) | closed record | Says "No known-open items" (`:156`). |
| `docs/known-gaps.md` §9 F16 (L265-278) | resolved + superseded | Its own text: "RESOLVED 2026-08-30 … Superseded 2026-09-29 (#2578): Retire and the symbol-validity store are removed". `app/broker/alpaca/symbol_validity.py` is not tracked. |
| `docs/known-gaps.md` §11 T1 (L454-457) | resolved + superseded | Its own text: "RESOLVED 2026-08-30 … Superseded 2026-09-29: Retire is removed (#2578)". |
| `docs/math-sources-of-truth.md` rows L191, L192, L193 | tombstone rows | Each says it was retired in PR-A/PR-B of #1813 and that "No canonical implementation remains". They also cite about 25 untracked files (`app/routers/broker_activity.py`, `app/engine/live/fleet.py`, `tests/services/test_clerk_transaction_projection.py`, …). |
| `docs/math-sources-of-truth.md` row L216 (SPY ORB) | tombstone row | Says "Removed 2026-08-28"; `app/engine/strategy/algorithms/spy_orb.py` is not tracked. |
| `docs/math-sources-of-truth.md` row L224 (offline-hours IBKR replay) | tombstone row | Says "retired composition; registered no math … No canonical implementation remains". |
| `docs/architecture-manual.md` §3.9 "Draining and handover … are not built" bullet (L456), diagram line L443, and §7 ④ (L691-696) | no longer true | Drain and lane quiet are built: `PythonDataService/app/broker/alpaca/clerk/sqlite/lane_quiet.py`, and ADR 0063 (`doc-authority.md:149`) says lane quiet has been answered on every heartbeat since 2026-09-22. **Served copy:** edit both files. |
| `docs/indicator-reliability-methodology.md` §5.2 "App shell" (L746-818) and the `app-sidebar` refs at L707, L766, L1165 | retired behavior | It describes `<app-sidebar />`. `Frontend/src/app/shell/` has no `app-sidebar.component.ts`, because the top-bar menubar replaced it (`263cda16`, #1853). **Served copy:** edit both files. |
| `docs/broker-clerk-fleet-authority.md` §10 "What this document replaced" (L478-497) | history of a deleted doc | `docs/broker-v2-operator-manual.md` is gone, and ADR 0041 is RETIRED (`doc-authority.md:127`). Git history is the archive. |
| `docs/broker-clerk-fleet-authority.md` §7 Half A first bullet (L295-300) and the §6 "Observability" row's "none renderable" (L235) | no longer true | It says 0 of 25 refusal codes appear in `Frontend/src/`. They now render through `Frontend/src/app/fleet/fleet-refusal-copy.ts` (#2067) and `fleet-refusal-vocabulary.snapshot.json`. The rest of §7 was not re-verified. |
| `docs/tv-polygon-validation-gotchas.md` L254-257 | unresolvable refs | They point at `~/Downloads/…` files, which are outside the repo. |
| `docs/doc-authority.md` rows for the whole-file cuts above (L57-58, L166, L168, L170, L204, L205) | orphaned index rows | They follow the cuts above. |
| `docs/doc-authority.md` prune-history prose (L7-13) and "Retired material" (L229-236) | history prose | Both only narrate past prunes; git history is the archive. |
| `docs/doc-authority.md` in-flight row L224 (`docs/prds/alpaca-account-clerk-sqlite-control-plane.md`, "Proposed … requires a follow-up ADR") | no longer true | ADRs 0035 and 0037 accepted it (`doc-authority.md:121`, `:123`). Whether the PRD itself goes is #2711's call. |
| `CONTEXT.md` "Binding authority" (L597-621) | historical, terms dead | `live_binding` and `evidence_binding` appear in no running code. The process registry it describes retired with IBKR bot control (ADR 0038). |
| `CONTEXT.md` "Destructive-action canonical render site" (L1035-1053) | historical, terms dead | `OperatorGate.suggested_action`, `focus_action` and `invoke_capability` appear in no running code. It describes cockpit tabs, which are retired (`app.routes.ts:57-58` redirect `broker/instances` away). |

## Kept, and why (no rows)

- **Linked by live sources, nothing disproved:**
  - `README.md`: the repo front page, protected.
  - `docs/doc-authority.md`: linked from `AGENTS.md:105`, and the checker's ADR-index check reads it (`check_documentation_contract.py:208-226`).
  - `docs/known-gaps.md`: linked from `AGENTS.md:103` and code.
  - `docs/math-sources-of-truth.md`: linked from rules, skills and code.
  - `docs/ibkr-integration-authority.md`: sacred, the IBKR feed. Its heading and topic sentence are pinned by `PythonDataService/tests/contracts/test_documentation_contract.py:100-115`.
  - `docs/broker-clerk-fleet-authority.md`: linked from runbooks and the manual.
  - `docs/architecture-manual.md`, `docs/indicator-reliability-methodology.md` and `docs/signal-engine-authority.md`: served in-app, plus code links.
  - `docs/options-companion-format.md`: linked from `.claude/rules/temporal-rigor.md:67` and code.
  - `docs/tv-polygon-validation-gotchas.md`: `app/research/divergence/preflight.py:98` and OpenAPI descriptions.
  - `docs/ml-predictions-authority.md`: `app/research/ml/artifact.py:20`.
  - `docs/math-rigor.md`: cited as rationale ("Upgrade 1/4") by `iv_builder.py:3-4`, `fred_service.py:4`, `iv30_health.py:4` and `surface.py:6`.
  - `docs/engine-persistence-authority.md`: linked from ADR 0058, and current since 2026-09-06.
  - `docs/spy-lean-output-report.md`: linked from `docs/spy-lean-output/source-map.md`, which `app/engine/results/lean_statistics.py:10` cites. This is math paperwork (#2714).
- **Runbooks with live links:**
  - `first-time-setup.md` and `windows-onboarding.md`: README.
  - `add-an-alpaca-account.md`: CLAUDE.md, AGENTS.md and UI.
  - `alpaca-sqlite-clerk-recovery-and-cutover.md`: ADR 0047, the setup scripts and a test.
  - `ibkr-setup-guide.md`: sacred; routed in-app.
  - `fleet-dev-two-lane-posture.md`: CI comment `.github/workflows/ci.yml:88`.
  - `fleet-directory-unavailable.md`: UI.
  - `migrate-installation.md`: `PythonDataService/scripts/migrate_installation.py:41`.
  - `ef-migrations-adoption.md`: `Backend/CLAUDE.md:68`.
  - `fleet-d-two-clerk-rollout.md`, `fleet-d-recovery-and-rollback.md`, `fleet-e-registry-recovery-exercise.md` and `fleet-e-compatibility-retirement.md`: linked from the fleet authority or ADR 0063, and their ceremonies still exist (`PythonDataService/scripts/manage_broker_fleet.py:722-767`). These are on the money path and I was unsure, so they stay.
- **Served copies:** `Frontend/src/assets/docs/*.md` follow their canonical files. The IBKR guide's served copy is sacred.
- **Sacred math paperwork:**
  - the golden `attribution.md` files under `PythonDataService/app/engine/tests/fixtures/golden/`;
  - `scripts/ibkr_snapshots/opt_ib_002/README.md` (OPT-IB-002 fixture; see the #2714 pointer).

## CONTEXT.md historical sections that stay for now

ADR 0040's rule is that nothing is archived while code that uses its terms still runs. These sections still have their terms named by running code, so they wait for the dead-code tickets.

| Section | Terms still named by code | Unblocks when |
|---|---|---|
| IBKR order-attribution ladder (L174-313) | `ADOPTED_BROKER_ORDER`, `LiveStateEnvelope`, `INTENT_NOT_ACCEPTED` in `app/engine/live/intent_ledger.py`, `live_state_sidecar.py`, `app/schemas/live_runs.py` | #2704 / #2702 cut `app/engine/live/*` |
| Control-surface scoping (L783-793) | `halt.flag`, `poisoned.flag` in `app/engine/live/halt.py`, `reconcile.py`, `app/operator/incidents/safety_halt_notices.py` | #2704 / #2705 |
| Daemon diagnostics (L1105-1228) | `HostRunnerHealth`, `LEASE_STALE`, `STALE_CODE` in `app/broker/ibkr/client.py`, `app/operator/notices/schema.py`, `app/services/sqlite_clerk_compat.py` | #2702 / #2703 |
| Account identity vs position contamination (L1054-1079) | `BROKER_ACCOUNT_MISMATCH` and friends still in code | #2702 |
| Broker Desk lenses (L1755-1799) | Kept only "as vocabulary for older ADRs and receipts" (`:1764`) | #2712 cuts the ADRs that say "lens" |
| Page-wide collapse rule (L954-1007) | It cites the deleted `docs/runbooks/broker-instance-operator-surface.md` (`:959`), but whether the v2 panel still follows the collapse principle was not verified | see Not reviewed |

Sections that are live and stay:
- Instance console mechanics: `BOT_COCKPIT_*_ANCHOR` in `Frontend/src/app/components/broker/v2-panel/bot-detail-banner/lifecycle-action.ts:3-4`.
- Live-instances intent endpoint: `MARK_POISONED`/`RECONCILE_NOW` in `app/broker/alpaca/clerk/sqlite/*`.
- Historical IBKR sizing authority: `LeanSetHoldingsSizing` in `app/engine/execution/sizing.py`.
- Broker session mirror: describes the surviving read-only IBKR health surface, `app/routers/broker.py:147`.

## What the cuts orphan

- **`scripts/check_documentation_contract.py`:**
  - The `EXACT_DOCUMENT_CLASSES` entries for `docs/CURRENT.md` and `docs/agent-start-here.md` (`:29-30`) become dead keys. They do not fail, because the checker only classifies files that exist.
  - The stale `docs/broker-v2-operator-manual.md` key (`:33`) is already dead.
- **`docs/doc-authority.md`:** the rows listed above. Also its canonical row L165, which still says engine runs persist "through `.NET`" while the doc's own header (`engine-persistence-authority.md:5`) says that endpoint is gone. That row needs rewording, not a cut.
- **`docs/options-companion-format.md:189`:** the false "tracked in `docs/options-cross-section-overview.md`" clause.
- **`docs/architecture/options-research.md:518`:** a related-docs link to the overview (#2712's file; supporting class, so the link checker does not fail on it).
- **Links between cut docs:** `feature-runner-authority.md` ↔ `indicator-reliability-authority.md`, and `portfolio-management.md:784` → overview. All of them go together.
- **Stale paths in kept rows (fix, not cut):**
  - `math-sources-of-truth.md:204-205` point at `clerk-position-drift-tolerance.md` and `clerk-fill-quantity-tolerance.md`, which were folded into `docs/references/clerk-invariants.md` on 2026-09-12 (`doc-authority.md:200`).
  - `signal-engine-authority.md:384` names `tests/test_graduation_stage0.py`; the tracked file is `tests/test_graduation.py`. This is a served copy, so fix both files.

## Hazards the cutting PR must carry

1. **Docs link contract** (`scripts/check_documentation_contract.py`, run by `PythonDataService/tests/contracts/test_documentation_contract.py::test_validate_repository_current_repository_passes`):
   - `_validate_local_links` (`:149-175`) resolves every markdown link in canonical, protected-canonical and in-flight docs and in **every** runbook. At this SHA, no such doc has a markdown link to any whole-file cut above; re-check at the cutting SHA.
   - `_validate_adr_index` needs `doc-authority.md` to keep one row per ADR file. If #2712 deletes ADRs, the same PR must drop their rows.
2. **Served byte copies** (`SERVED_DOCUMENT_COPIES`, `:58-64`):
   - `architecture-manual.md`, `indicator-reliability-methodology.md` and `signal-engine-authority.md` must change in the same commit as `Frontend/src/assets/docs/…`, or the check fails.
   - Their GitHub blob links are checked for existence (`:292-297`), so a served doc that links a cut file breaks too.
3. **Pinned IBKR text.** `test_documentation_contract.py:100-115` pins a heading and a verbatim sentence in `docs/ibkr-integration-authority.md`, and `:171-175` pins the served IBKR guide. This is the sacred feed: don't slim either file.
4. **Protected canonicals.** `doc-authority.md:53-64` says `CURRENT.md`, `agent-start-here.md`, `math-sources-of-truth.md` and `README.md` are "never edit without owner sign-off". That conflicts with the map's "items need no owner sign-off". See question 1.
5. **Known-gaps numbering.** Section numbers are cited from outside the file (`PythonDataService/scripts/bench_panel_read_latency.py:14` cites §9). Delete §2/§3/§4/§7 without renumbering the rest.
6. **CONTEXT.md.** 38 ADRs and 9 skills mention `CONTEXT.md`. No tracked file links an anchor into the two sections cut here; re-grep `CONTEXT.md#` at the cutting SHA.
7. **Kill lists age.** Re-run the inbound-link grep for each file at the cutting PR's own SHA before deleting it.

## Pointers outside this area (not rows)

- **#2716 (CI checks and hooks):**
  - `scripts/check_documentation_contract.py:33` still classifies the deleted `docs/broker-v2-operator-manual.md`.
  - Its `RETIRED_DOCUMENTS`/`FORBIDDEN_CURRENT_GUIDANCE` lists (`:40-53`) guard docs that are already gone.
  - That ticket decides whether the checker survives the gate bar.
- **#2709 (rest of the Frontend):** `@polygon.io/client-js` is listed in `Frontend/package.json` but imported nowhere in `Frontend/src`, so it is a dead dependency.
- **#2704 (engine and volatility):**
  - `PythonDataService/app/volatility/docs/` (`dashboard.html`, `dashboard.js`, `dashboard_panels.js`, `dashboard.css`, `index.html`, `sample_data.js`, `llms.txt`) is served by no route.
  - `app/engine/options/` (`chain_resolver.py`, `pricer.py`) has no importers (`known-gaps.md:163-178`).
  - `app/engine/__init__.py:6` and `app/engine/engine.py:8` cite the deleted `docs/lean-engine-implementation-plan.md`.
- **#2707 (scripts):**
  - `PythonDataService/scripts/run_broker_fleet_conformance.py` loses its only operator doc when `fleet-e-conformance-and-operator-acceptance.md` goes.
  - `scripts/capture_ibkr_snapshot.py` has never produced a committed snapshot (`scripts/ibkr_snapshots/opt_ib_002/README.md`, "none yet").
- **#2712 (ADRs and architecture docs):**
  - ADR 0018's lsof/API-event spine has no code (`lsof` appears in no non-doc file).
  - `docs/architecture/options-research.md:518` links the overview cut here.
- **#2714 (math reference notes):** `docs/spy-lean-output/` and `docs/references/golden-fixtures/options-pricing/OPT-IB-002.md` with its snapshot README.
- **#2715 (rules):**
  - `Backend/CLAUDE.md:68` is the only live link to `docs/runbooks/ef-migrations-adoption.md`, a one-time adoption procedure (migrations were adopted at `Backend/Migrations/20260519052007_InitialSchema.cs`). If that line goes, the runbook goes too.
  - `AGENTS.md:105-115` loads `CURRENT.md`-era routing.
- **No owning ticket:** `docs/process/*.md` (three files, listed at `doc-authority.md:206-208`) and `docs/domain/` fall under no child of #2700.

## Not reviewed

- **`docs/known-gaps.md` §§5, 8, 9 (F3-F14 except F16), 10, 11 (except T1), 12, 13, 13a, and the three dated tail sections:** not re-verified item by item. Only the closed or superseded items above were checked.
- **`docs/broker-clerk-fleet-authority.md` §§1-6, 8, 9 and the rest of §7:** not checked against code.
- **`docs/architecture-manual.md` and `docs/signal-engine-authority.md`:** only the drain claim and the dead-path scan were checked, not every chapter.
- **`docs/math-rigor.md`:** whether each of the ten "Upgrades" shipped. Upgrade 4's `fred_rates.py` and `bs_solver.py` (`:213-214`) do not exist, but code still cites the doc as rationale.
- **`docs/ml-predictions-authority.md` and `docs/feature-runner-authority.md`:** content not checked, since the latter is cut on links alone.
- **`CONTEXT.md` neutral and live sections, and the historical sections in the table above:** not judged beyond the term check. That includes the Page-wide collapse rule.
- **Runbook bodies** (`add-an-alpaca-account.md`, `first-time-setup.md`, `windows-onboarding.md`, `alpaca-sqlite-clerk-recovery-and-cutover.md`, `fleet-*`): only the dead-path scan, no step-by-step check against current scripts.

## Questions for the map (rule-level)

1. **Protected canonicals vs. no sign-off.** `doc-authority.md:53` marks four docs "never edit without owner sign-off", and this list cuts two of them and rows of a third. Does the map's "items need no owner sign-off" override that table? Recommendation: yes, and the rule rewrite drops the table.
2. **Directory READMEs.** Is a README inside a code folder "read" because GitHub shows it when you open that folder? Recommendation: no, so the link rule applies as written. Of this list, only `PythonDataService/app/research/options/README.md` depends on the answer, since the other two folder READMEs already fail on content.
3. **Index rows.** This list assumes a row in `doc-authority.md` does not count as a live link; otherwise every listed doc survives by construction. If the map disagrees, the five cuts that rest on links alone come back: `feature-runner-authority`, `indicator-reliability-authority`, `portfolio-management`, `portfolio-validation-plan`, and `research/options/README` (which also depends on question 2).
