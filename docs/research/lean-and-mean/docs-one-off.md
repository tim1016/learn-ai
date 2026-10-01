# Kill list — one-off docs: plans, PRDs, audits, research and design notes

Ticket: #2711 · Map: #2700 · Read at: `87b8e261021ec673c2e7c80448c0973bccd45378` (`origin/master`, the map's charting SHA).

**Area:** `docs/superpowers/`, `docs/prds/`, `docs/design/`, `docs/process/`, `docs/research/`,
`docs/audits/`, `docs/spy-lean-output/`, `docs/validation/`. At this SHA they hold 108 tracked files,
plus `docs/audits/auto-research/findings/.gitkeep`. Every file got a row below: in the kill list, in the
keep register, or in a pointer.

## How each file was judged

- **Inbound-reference scan.** For every file in the area, `git grep -F <basename>` across the whole tree,
  leaving out the file itself. For the generic names `inventory.json`, `state.json`, `verify.py` and
  `source-map.md`, the scan searched the full path and the hits were then checked by hand. Each referrer is
  bucketed as **live** (code, tests, scripts, `compose.yaml`, CI, `.claude/` skills and rules, `AGENTS.md`,
  `CONTEXT.md`, served assets), **outside doc** (a doc outside the area, owned by #2712, #2713 or #2714), or
  **in-area**. An in-area referrer counts only if that referrer is itself kept.
- **Open issues.** I pulled the bodies and comments of all 43 open issues and searched them for every
  filename. Only `docs/audits/live-ema-spy-missed-entry-2026-09-17.md` comes up (#2639), and it is kept anyway.
  For each research note and PRD, I checked whether its own issue or PR is still open.
- **Rules applied as locked.** A code comment counts as a live link. So does a mention from a kept doc, followed
  transitively.
- **One interpretation the map should confirm.** A row in the `docs/doc-authority.md` "Supporting docs" register
  is a register entry, not a reading link. I do not count it. If register rows counted, no doc in the register
  could ever become sediment. Three cuts below (`submit-to-custody…`, `agent-collaboration`,
  `pr-review-escalations`) rest on this reading. Flip them to keep if the map disagrees.
- **Retired behavior.** I judged it only where the evidence needed it. Linked docs were not read end to end; see
  **Not reviewed**.

## Kill list

| Path | Kind | Evidence |
|---|---|---|
| `docs/audits/alpaca-broker-execution-hardening-2026-08-03.md` | audit | No inbound reference in the tree or in any open issue. |
| `docs/audits/alpaca-sqlite-clerk-qualification-full.json` | qualification run snapshot | No inbound reference. It is an undated 2026-08-06 run (b0381638), superseded by the dated `…-full-2026-08-11.json` that the soak report cites. The runner takes a required `--json-output` (`PythonDataService/scripts/run_alpaca_sqlite_qualification.py:76`), so nothing writes here. |
| `docs/audits/alpaca-sqlite-clerk-qualification-smoke.json` | qualification run snapshot | No inbound reference. CI writes smoke output to `TestResults/` instead (`.github/workflows/ci.yml:523`). |
| `docs/audits/alpaca-sqlite-clerk-qualification-smoke.md` | qualification run snapshot | No inbound reference. CI writes it to `TestResults/` (`.github/workflows/ci.yml:524`). |
| `docs/audits/broker-configuration-integration-2026-09-10.md` | acceptance receipt (Package G) | No inbound reference. The decision lives in ADR 0060 and `docs/architecture/broker-configuration-profile-contract.md`. |
| `docs/audits/contract-surface-drift-2026-08-18.md` | audit | Its only referrer is `docs/audits/data-lab-session-consumers-2026-09-12.md:18`, which is also on this list. |
| `docs/audits/data-lab-session-consumers-2026-09-12.md` | audit | No inbound reference. |
| `docs/audits/execution-path-fail-open-2026-08-18.md` | audit | No inbound reference. |
| `docs/audits/ibkr-control-plane-decommission-retirement-receipt-2026-08-27.md` | retirement receipt | No inbound reference. Umbrella #1813 is closed. The decommission stays documented in the kept closeout plan and slice-0 design. |
| `docs/audits/non-numeric-operator-verdict-census-2026-08-18.md` | audit | No inbound reference. |
| `docs/audits/submit-to-custody-fail-open-sweep-2026-08-17.md` | audit | Its only referrer is the register row `docs/doc-authority.md:194`. That row itself says the confirmed seams were moved into `docs/known-gaps.md` via #1604, which is closed. |
| `docs/audits/validation_report_SPY_15m.pdf` | generated report | Nothing in the tree names the file. It is not a golden fixture or an attribution file. |
| `docs/prds/2026-09-18-resilient-bot-chart-history-and-controls.md` | PRD (shipped) | No inbound reference. PRD issue #2201 and slices #2202–#2206 are all closed. |
| `docs/process/agent-collaboration.md` | process note | Its only referrer is the register row `docs/doc-authority.md:206`. No CLAUDE.md, AGENTS.md, rule or skill names it. |
| `docs/process/pr-review-escalations.md` | process log | Its only referrer is the register row `docs/doc-authority.md:208`. It is a dated log of May PR escalations (PR #161 through PR #170). |
| `docs/research/adversarial-extended-hours-design-review-2026-09-04.md` | adversarial review | No inbound reference. |
| `docs/research/adversarial-prds-1925-1926-1927-2026-09-04.md` | adversarial review | No inbound reference. #1925, #1926 and #1927 are closed. |
| `docs/research/alpaca-overnight-trading-2026-08-25.md` | research note | No inbound reference. Overnight-exit PRD #2504 is closed. |
| `docs/research/extended-hours-bot-control-2026-09-04.md` | research note | No inbound reference. It studies IBKR extended-hours *orders*, and IBKR order actuation is retired (`docs/ibkr-integration-authority.md:34`). |
| `docs/research/pr-2495-retrospective-2026-09-25.md` | retrospective | No inbound reference. PR #2495 is merged. |
| `docs/research/prd2540-historical-evidence-inventory.md` | research note | No inbound reference. PRD #2540 is closed. |
| `docs/superpowers/plans/2026-08-26-ibkr-decommission-slice-0.md` | implementation plan | No inbound reference. #1813 is closed. Its design spec stays, because code cites it. |
| `docs/superpowers/plans/2026-09-01-strategy-lab-one-page.md` | implementation plan | No inbound reference. #1425 is closed. |
| `docs/superpowers/plans/2026-09-02-feed-reconnect-continuity-floor.md` | implementation plan | No inbound reference. #1921 is closed. The spec and ADR 0053 carry the decision. |
| `docs/superpowers/plans/2026-09-07-live-slice-1-worlds-and-identity.md` | implementation plan | No inbound reference. This ADR 0059 slice has shipped. |
| `docs/superpowers/plans/2026-09-07-live-slice-2-alpaca-fee-model.md` | implementation plan | No inbound reference. |
| `docs/superpowers/plans/2026-09-08-live-slice-3-extended-hours.md` | implementation plan | No inbound reference. |
| `docs/superpowers/plans/2026-09-08-live-slice-4-shadow-authority.md` | implementation plan | No inbound reference. #1702 is closed. |
| `docs/superpowers/plans/2026-09-09-live-slice-6-arming-ceremony.md` | implementation plan | No inbound reference. Its design spec is kept separately (see the keep register). |
| `docs/superpowers/plans/2026-09-09-live-slice-7-gate-remeaning.md` | implementation plan | No inbound reference. |
| `docs/superpowers/specs/2026-05-15-readme-auto-updater-design.md` | design spec (never built) | No inbound reference. A search for `auto-updater`/`readme_auto` matches only this file, so no updater exists. |
| `docs/superpowers/specs/2026-09-01-strategy-lab-one-page-design.md` | design spec | Its only referrer is its own plan (the row above). |

## What the cuts orphan

- **No docs, fixtures, images, helpers or config.** I re-searched every tracked file whose name appears inside a
  cut doc. The only ones referenced by nothing but cuts are code and test files, which stay reachable through
  imports. The others are ADRs 0027, 0040 and 0052 and the `OPT-IB-002` reference note, and each of those is
  cited elsewhere by number (`CONTEXT.md`, code, `tests/fixtures/golden/manifest.json`).
- **Register rows to delete in the same PR:** `docs/doc-authority.md:194`, `:206` and `:208`.
- **No kept doc links a cut.** Each cut's inbound referrers are other cuts or those register rows (written as
  inline code, not markdown links).

## Hazards the cutting PR must carry

1. **Documentation contract.** `_validate_local_links` (`scripts/check_documentation_contract.py:149`) checks
   links from canonical and protected-canonical docs, from `docs/prds/**` (classed "in-flight", line 111) and from
   runbooks. I ran the checker's own link walker against the cut list, and none of the cuts is a link target from
   those sources. After cutting, still run `python scripts/check_documentation_contract.py` and
   `pytest PythonDataService/tests/contracts/test_documentation_contract.py`, which call
   `validate_repository(REPOSITORY_ROOT)` at line 54.
2. **Kept docs that are contract-pinned.** Anyone who later cuts one of these (including follow-ons from
   #2712/#2713) must drop the inbound markdown link in the same PR, or the contract fails:
   - `docs/audits/alpaca-bot-control-panel-architecture-audit-2026-08-02.md`, linked from `docs/prds/alpaca-account-clerk-sqlite-control-plane.md:1009` (in-flight).
   - `docs/audits/alpaca-paper-live-workflow-2026-09-09.md` and `docs/audits/live-ema-spy-missed-entry-2026-09-17.md`, linked from `docs/known-gaps.md`.
   - `docs/audits/alpaca-sqlite-clerk-paper-soak-2026-08-07.md` and `docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md`, linked from ADR 0035.
   - `docs/audits/alpaca-sqlite-sole-authority-retirement-2026-08-19.md`, linked from ADR 0037.
   - `docs/audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json`, `docs/design/2026-09-13-clerk-fleet-delivery-review.md` and `docs/prds/2026-09-12-multi-broker-clerk-control-plane.md`, linked from ADR 0062. The PRD is also linked from five `docs/runbooks/fleet-*` runbooks.
   - `docs/design/user-owned-broker-configurations-plan-2026-09-10.md`, linked from ADR 0060.
   - `docs/design/fleet-d-runtime-ownership-matrix.md`, linked from `docs/runbooks/fleet-d-two-clerk-rollout.md` and `docs/runbooks/fleet-e-compatibility-retirement.md`.
3. **`docs/doc-authority.md` belongs to #2713.** Coordinate with it, or make the three register-row deletions in
   the same PR.
4. **`compose.yaml:316` mounts `docs/audits/alpaca-sqlite-clerk-synthetic-ui-evidence-2026-08-07.json`** into the
   `alpaca-clerk-qualification` service. Do not cut it while that service exists.
5. **Sealed artifact paths are not affected.** No area path appears in any JSON, YAML, SQL or fixture under
   `PythonDataService/tests/fixtures/`, so no golden receipt hash changes.
6. **`docs/research/` ends up empty on master.** The lean-and-mean lists live on throwaway branches.
   `check_documentation_contract.py:113-127` and `docs/doc-authority.md:28` still name the directory, which is
   harmless. `test_research_documents_are_supporting_evidence` builds its file in `tmp_path` and does not depend
   on the directory.
7. **Kill lists age.** Before deleting, re-run the inbound scan at the cutting PR's own SHA:
   `for f in <cuts>; do git grep -l -F "$(basename $f)" -- . ":(exclude)$f"; done` should list only other cuts
   and `docs/doc-authority.md`.

## Keep register (rest of the area)

**Kept by a live link** (code, test, script, skill, rule, `AGENTS.md`, compose):

| Path | Strongest live referrer |
|---|---|
| `docs/audits/auto-research/{baseline-math-rigor.md,build-alpha-functionality-validation.md,state.json}` (+ `findings/.gitkeep`) | `.claude/skills/auto-research-tick/SKILL.md:12` (pointer below) |
| `docs/audits/alpaca-sqlite-clerk-synthetic-ui-evidence-2026-08-07.json` | `compose.yaml:316` (pointer below) |
| `docs/audits/bar-timestamp-rigor-2026-06-12.md` | `PythonDataService/tests/services/test_bar_timestamp_rigor.py:4` |
| `docs/audits/bot-fleet-stress-2026-08-25.md` | `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_runtime.py:173` (+4 live, 6 docs) |
| `docs/audits/computational-fidelity-2026-04-22.md` | `.claude/rules/temporal-rigor.md:24` |
| `docs/audits/computational-fidelity-2026-04-22-addendum.md` | `AGENTS.md:59` |
| `docs/audits/live-ema-spy-missed-entry-2026-09-17.md` | `PythonDataService/scripts/measure_verdict_under_live_terms.py:103`; open #2639 |
| `docs/audits/open-pr-review-2026-08-05.md` | `PythonDataService/app/broker/alpaca/clerk/sqlite/commands.py:19` (+11 code/test files) |
| `docs/audits/read-latency-profile-live-2026-08-31.md` | `PythonDataService/scripts/bench_panel_read_latency.py:24` |
| `docs/audits/strategy-execution-research-directions-2026-08-24.md` | `PythonDataService/app/routers/run_replay.py:4` |
| `docs/design/bouchaud-farmer-lillo-2008-microstructure-implementation-design.md` | `PythonDataService/tests/fixtures/golden/microstructure/BFL-2008-MICRO-001/v1/attribution.md:11` (golden attribution) |
| `docs/design/fleet-b-route-inventory.md` | `PythonDataService/app/broker/alpaca/clerk/fleet_adapter.py:99` |
| `docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md` | `PythonDataService/app/broker/alpaca/clerk/sqlite/economic_projection.py:18` |
| `docs/prds/2026-08-12-broker-account-desk-lens-redesign.md` | `PythonDataService/app/services/account_pnl_reconciliation.py:8` |
| `docs/prds/2026-09-12-multi-broker-clerk-control-plane.md` | `CONTEXT.md:2077`; ADR 0062 |
| `docs/prds/alpaca-account-clerk-sqlite-control-plane.md` | `PythonDataService/app/broker/alpaca/clerk/sqlite/recovery_policy.py:918` |
| `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md` | `PythonDataService/app/broker/alpaca/clerk/sqlite/decision_receipts.py:41` (+6 live) |
| `docs/prds/strategy-lab-results-experience.md` | `PythonDataService/app/engine/results/equity_downsample.py:133` |
| `docs/process/autonomous-decisions.md` | `PythonDataService/app/main.py:1398` (cites D-010) |
| `docs/spy-lean-output/verify.py`, `docs/spy-lean-output/source-map.md` | `PythonDataService/app/engine/results/lean_statistics.py:10` |
| `docs/superpowers/plans/2026-08-05-alpaca-clerk-corrective-foundation-slice.md` | `PythonDataService/app/broker/alpaca/clerk/sqlite/repository.py:15` |
| `docs/superpowers/plans/2026-08-26-ibkr-decommission-closeout.md` | `PythonDataService/app/services/clerk_transaction_projection.py:6` |
| `docs/superpowers/plans/2026-09-14-fleet-lane-e-frontend-fence-and-refusals.md` | `PythonDataService/app/broker/fleet/refusal_vocabulary.py:15` (see "Unlock" below) |
| `docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md` | `PythonDataService/app/research/ml/artifact.py:19` |
| `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md` | `PythonDataService/app/research/ml/generators/quantconnect_fixture.py:22` |
| `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md` | `PythonDataService/app/research/parity/qc_reconciler.py:27` |
| `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` | `PythonDataService/app/lean_sidecar/parity_matrix/matrix.py:3` (+6 live) |
| `docs/superpowers/specs/2026-07-12-engine-lab-overhaul-design.md` | `PythonDataService/app/engine/results/equity_downsample.py:6` |
| `docs/superpowers/specs/2026-08-14-bot-gallery-redesign-design.md` | `Frontend/src/app/components/broker/v2-panel/gallery/lib/candle-renderer.ts:7` |
| `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | `PythonDataService/app/research/recency/stats.py:15` (+5 live) |
| `docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md` | `PythonDataService/tests/structural/test_ibkr_feed_boundary.py:11` (sacred IBKR feed boundary) |
| `docs/superpowers/specs/2026-09-02-feed-reconnect-continuity-design.md` | `PythonDataService/app/services/decision_clock.py:132` |
| `docs/validation/SPY_EMA_Crossover_RSI.pine`, `docs/validation/SPY_EMA_Crossover_Validation_Report.pdf` | `PythonDataService/app/engine/strategy/registry.py:444` (strategy provenance string) |

**Kept only through a doc outside the area.** Each becomes sediment if its owning ticket cuts that doc or link:

| Path | Kept by | Owner |
|---|---|---|
| `docs/audits/alpaca-paper-live-workflow-2026-09-09.md` | `docs/known-gaps.md:498`, `docs/architecture/alpaca-configuration-ownership-inventory.md:100` | #2713 / #2712 |
| `docs/audits/alpaca-sqlite-clerk-paper-soak-2026-08-07.md` | ADR 0035:13 | #2712 |
| `docs/audits/alpaca-sqlite-sole-authority-retirement-2026-08-19.md` | ADR 0037:115 | #2712 |
| `docs/audits/bot-fleet-stress-2026-08-26.md` | ADR 0050:4, ADR 0051:4, `docs/known-gaps.md:386` | #2712 / #2713 |
| `docs/audits/clerk-lineage-reachability-2026-08-17.md` | ADR 0037:8 | #2712 |
| `docs/audits/live-operator-surface-inventory-2026-08-18.md` | ADR 0041:23 | #2712 |
| `docs/audits/numeric-authority-census-2026-08-17.md` | ADR 0036:8 | #2712 |
| `docs/audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json` | ADR 0062:130 | #2712 |
| `docs/audits/strategy-lab-regression-investigation-2026-09-27.md` | `docs/references/lake-committed-admission.md:71` | #2714 |
| `docs/design/2026-09-13-clerk-fleet-delivery-review.md` | ADR 0062:139, `docs/broker-clerk-fleet-authority.md:37` | #2712 / #2713 |
| `docs/design/fleet-a2-lane-inventory.md` | `docs/broker-clerk-fleet-authority.md:509` | #2713 |
| `docs/design/fleet-d-runtime-ownership-matrix.md` | ADR 0063:336, two fleet runbooks | #2712 / #2713 |
| `docs/design/user-owned-broker-configurations-plan-2026-09-10.md` | ADR 0060:4 | #2712 |
| `docs/prds/2026-09-12-data-lab-workspace-redesign.md` | `docs/known-gaps.md:506`, `docs/math-sources-of-truth.md:140` | #2713 |
| `docs/superpowers/plans/2026-09-08-live-slice-5-risk-envelope.md` | `docs/architecture/alpaca-configuration-ownership-inventory.md:101` | #2712 |
| `docs/superpowers/plans/2026-09-15-fleet-lane-visibility-and-handover.md` | ADR 0063:4 | #2712 |
| `docs/superpowers/specs/2026-09-09-live-slice-6-arming-ceremony-design.md` | `docs/references/alpaca-live-arming.md:361` | #2714 |
| `docs/superpowers/specs/2026-09-09-live-slice-7-gate-remeaning-design.md` | `docs/references/alpaca-live-authority.md:267` | #2714 |

**Kept through a kept doc inside the area.** These go if their keeper goes:

| Path | Kept by |
|---|---|
| `docs/audits/alpaca-bot-control-panel-architecture-audit-2026-08-02.md` | `docs/prds/alpaca-account-clerk-sqlite-control-plane.md:1009` (contract-pinned) |
| `docs/audits/alpaca-sqlite-clerk-qualification-full-2026-08-11.{json,md}`, `…-synthetic-rehearsal-2026-08-07.json`, `…-s6-cutover-receipts-2026-08-11.json` | the paper-soak report (`:716`, `:718`, `:243`, `:705`), so they rest on ADR 0035 |
| `docs/audits/alpaca-sqlite-s6-broker-{pre-cutover,post-campaign}-proof-2026-08-11.json` | `…-s6-cutover-receipts-2026-08-11.json:58`, `:66` |
| `docs/audits/ibkr-control-plane-decommission-inventory-2026-08-26.md` | the slice-0 design and closeout plan, line 6 of each. It holds the feed-dependency envelope, so keep it while unsure (IBKR feed is sacred). |
| `docs/audits/plan-tournament-2026-09-12-data-lab-ui-ux.json` | `docs/prds/2026-09-12-data-lab-workspace-redesign.md:7` |
| `docs/audits/structural-integrity-2026-04-22.md` | `docs/audits/computational-fidelity-2026-04-22.md:8` ("Companion to") |
| `docs/design/alpaca-desk-account-selection-ux-2026-09-11.md` | `docs/prds/2026-09-12-multi-broker-clerk-control-plane.md:1040` |
| `docs/spy-lean-output/inventory.json` | `docs/spy-lean-output/source-map.md:5` |
| `docs/superpowers/plans/2026-09-10-shadow-to-live-operator-journey.md` | `docs/audits/alpaca-paper-live-workflow-2026-09-09.md:13` |
| `docs/superpowers/plans/2026-09-14-fleet-trust-register-{fixes,closeout}.md` and `fleet-lane-{a,b,c,d,f,g}-*.md` | lane E's `:11` → fixes `:15-21` (see "Unlock" below) |

### Unlock: keeps that rest on one code comment

These keeps are valid under the locked rule. Each could become a cut if the cutting PR were allowed to copy the
cited fact into the comment and drop the citation:

- **The fleet trust-register plan family (nine files).** The only live link is
  `PythonDataService/app/broker/fleet/refusal_vocabulary.py:15`, a pointer to lane E's "decision 9", and that
  comment already restates the decision on lines 16-18. Everything else reaches the family only through lane E's
  parent pointers.
- **Single-comment keeps:** `docs/superpowers/specs/2026-07-12-engine-lab-overhaul-design.md`
  (`equity_downsample.py:6`), `docs/superpowers/plans/2026-08-26-ibkr-decommission-closeout.md`
  (`clerk_transaction_projection.py:6`), `docs/process/autonomous-decisions.md` (`main.py:1398`) and
  `docs/audits/bar-timestamp-rigor-2026-06-12.md` (a test docstring).

## Pointers: cuttable things outside this area

- **#2715 (repo skills).** `.claude/skills/auto-research-tick/SKILL.md` is the only thing keeping
  `docs/audits/auto-research/` alive. `state.json` was last touched 2026-05-13 (1fc5dcb2) and sits at
  `"mode": "build-alpha-validation-complete-awaiting-review"`, which looks dormant. If #2715 cuts the skill, the
  three files and `findings/.gitkeep` go with it, along with the mention in `docs/known-gaps.md:9` (#2713).
- **#2707 (compose) / #2716 (CI).** The `alpaca-clerk-qualification` service (`compose.yaml:285`) and the
  `alpaca-sqlite-qualification-smoke` CI job (`.github/workflows/ci.yml:500`). If the service goes,
  `docs/audits/alpaca-sqlite-clerk-synthetic-ui-evidence-2026-08-07.json` goes with it.
- **#2713 (authority docs).**
  - The "Active / in-flight design" table in `docs/doc-authority.md` lists the SQLite control-plane and
    sealed-signal PRDs as in-flight, but both have shipped and their decisions are in ADRs 0035 and 0042/0043.
    The same doc's own rule says such PRDs are "pruned once shipped + ADR-captured". They stay here only
    because code cites them.
  - `docs/spy-lean-output-report.md` is the companion of the kept `docs/spy-lean-output/*`.
- **#2716 (CI checks).** `RETIRED_DOCUMENTS` (`scripts/check_documentation_contract.py:40-47`) names files that
  are already deleted. Once `docs/research/` empties, the directory set on lines 113-127 carries a dead entry.
- **#2715 (rule rewrite).** Sediment will grow back. The repo `research` skill and the superpowers
  `writing-plans`/brainstorming skills keep committing one-off notes into `docs/research/` and
  `docs/superpowers/`. Nothing live links those notes once their PR merges.

## Not reviewed

- **Retired behavior in the 76 kept files.** I judged them by inbound links and read only their titles and
  status lines. The exceptions were the IBKR inventory, the lane-E citation chain and the bot-control-panel
  audit, which I read where the evidence required it. A kept doc may still describe retired behavior, such as an
  older UI that a later redesign replaced. I did not read the kept PRDs and specs end to end to check this.
- **What shipped versus what the PRDs say.** For the PRDs kept by code links, I did not verify which sections
  still match the code.
