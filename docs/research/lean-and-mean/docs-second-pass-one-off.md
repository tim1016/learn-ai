# Kill list — second docs pass: kept plans, PRDs, audits and research notes

Ticket: #2740 · Map: #2700 · Read at: `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master` when this
branch was cut). The first pass (#2711) was read at `87b8e261`. All 63 docs below still exist unchanged in
name at this SHA.

**Area.** The 76 docs the [first pass](https://github.com/tim1016/learn-ai/blob/research/lean-docs-one-off/docs/research/lean-and-mean/docs-one-off.md)
kept, minus the 13 its **Unlock** section listed, which the owner's addendum on #2711 already cut: lane E,
the trust-register fixes and closeout, lanes A/B/C/D/F/G, the engine-lab overhaul spec, the IBKR
decommission closeout plan, `docs/process/autonomous-decisions.md` and `bar-timestamp-rigor-2026-06-12.md`.
That leaves **63 docs**.

## How each doc was judged

- **The bar** is the ☆ *code is the documentation* ruling. A doc stays only if it holds something code
  cannot hold:
  - a decision and its why (an ADR in force),
  - an operator procedure (a runbook),
  - an outside fact,
  - intent for work still being built (an **open** PRD or issue).

  Code comments, index rows and links from other docs never keep a doc alive.
- **Open work.** Only 7 non-wayfinder issues are open (#2599, #2639, #2660, #2686, #2689, #2694, #2696). Every
  PRD, plan and spec in the area belongs to a closed issue: #1086, #1723, #1731, #1813, #1921, #2201, #2468,
  #2540, #2560 and the others the first pass checked. Only #2639 names an area doc.
- **Where the why lives.** For each shipped plan, spec or PRD, I looked for its decisions in the ADR it
  produced (0022, 0034–0037, 0041–0043, 0053, 0057, 0059, 0060, 0062, 0063), in a `docs/references/` note, in
  `CONTEXT.md`, in `AGENTS.md`, or in code. If a why lives nowhere else, it is listed under **ADR owed**.
- **Method.** For every doc I read the headings, status lines and decision sections. I read in full only
  where the verdict hinged on it. An inbound scan of the whole tree, excluding the area, found 214 citing
  lines in 120 files; they drive the hazards below.
- **Volatility ruling (owner, 2026-09-30) checked.** No doc here is the only home of a volatility citation, a
  documented non-equivalence or a why. Those live in `app/volatility/vix_replication.py`, the golden
  attributions (`iv30/`, `IV-003`, `RV-004`) and `docs/references/golden-fixtures/`. The only mentions are
  Greeks-shape rows in the two 2026-04-22 audits and an F-0007 to-do in the auto-research baseline. The
  ruling changes no verdict below.

## Verdicts

**3 keep · 2 slim (merge into a kept home, then delete the doc) · 58 cut.**

| Path | Verdict | Kind | Evidence |
|---|---|---|---|
| `docs/audits/live-ema-spy-missed-entry-2026-09-17.md` | **keep** | investigation (outside fact) | Holds the live 09-17 run timing against the retained IBKR bars: the crossover closed 88 s before launch. Code cannot record that. It is the evidence cited by **open** PRD #2639. Cut when #2639 closes. (Its other referrer, `scripts/measure_verdict_under_live_terms.py:103`, is dead per #2707 row 7.) |
| `docs/validation/SPY_EMA_Crossover_RSI.pine` | **keep** | outside validation source | The TradingView Pine port used to cross-check the EMA strategy outside our engines. Named in the strategy's provenance at `app/engine/strategy/registry.py:444` and in `ema_crossover_signal.py:4`. |
| `docs/validation/SPY_EMA_Crossover_Validation_Report.pdf` | **keep** | outside validation result | TradingView's own run of the Pine port. That is an outside fact no repo code can produce. Same provenance strings. (The PDF was not opened; see Not reviewed.) |
| `docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` | **slim → merge** | design spec (shipped) | Keep only §"Tolerances and acceptance gates": why the gates run in order (observations, then state, then trades), and why each field is exact. Move it into the fixture's own paperwork, `tests/fixtures/golden/cross-engine-studies/README.md`, which already repeats §"Regeneration policy" (`README.md:13-20`). The rest restates code: matrix and run topology (`app/lean_sidecar/parity_matrix/matrix.py`, `cell_runner.py`), storage and manifest schema (`parity_matrix/manifest.py`), and the regen CLI (`scripts/regenerate_cross_engine_study.py`). Then delete the doc. |
| `docs/design/fleet-d-runtime-ownership-matrix.md` | **slim → merge** | delivery contract | §"Evidence classifications and Delivery D gates" (`:55-75`) and §"Compatibility observation begins before the cutover" (`:76-96`) are operator procedure: `docs/runbooks/fleet-d-two-clerk-rollout.md:3` says to mark a gate complete only with these fields. Merge them into that runbook, but only if #2713 keeps it. The role/ownership table (`:7-30`) restates compose roles and `FLEET_ROLE` mounting. Then delete the doc. |
| `docs/audits/auto-research/baseline-math-rigor.md` | cut | skill state | Exists only to feed `.claude/skills/auto-research-tick/SKILL.md`, which #2715 cuts as dead. It is a May-2026 math-rigor baseline whose findings were folded into `docs/math-sources-of-truth.md` (its own §"Recommendation plan" rows) and code provenance blocks. |
| `docs/audits/auto-research/build-alpha-functionality-validation.md` | cut | skill charter | Same: a one-shot charter for the same skill (`SKILL.md:341`). Its state is `…-complete-awaiting-review` since 2026-05-13. |
| `docs/audits/auto-research/state.json` (+ `findings/.gitkeep`) | cut | skill state | Same skill's mode file (`SKILL.md:12`). It goes with the skill. |
| `docs/audits/alpaca-sqlite-clerk-synthetic-ui-evidence-2026-08-07.json` | cut | config input | Its only live use is the mount at `compose.yaml:316` into the `alpaca-clerk-qualification` service, which #2707 (row 21) cuts as never run. Dead config follows dead code. |
| `docs/audits/bot-fleet-stress-2026-08-25.md` | cut | stress-run audit | Point-in-time run. Fixes shipped in #1772/#1777, and the open items became issues. The §7 "one tap fills, the other drains" frame is recorded in ADRs 0047, 0048 and 0051 and in `docs/known-gaps.md`. Its S1 fact (IBKR forces a weekly re-login) is already stated in `docs/runbooks/ibkr-setup-guide.md:44-50`. |
| `docs/audits/bot-fleet-stress-2026-08-26.md` | cut | stress-run audit | Same. T6/T7 fixes and lease revival are in ADRs 0050 and 0051. §7 "Ops lore" is stale: in-container pytest was replaced by the host `.venv` runner, and the podman restart notes duplicate `docs/runbooks/`. |
| `docs/audits/computational-fidelity-2026-04-22.md` | cut | audit (2026-04) | Its findings drove ADR 0022. `.claude/rules/temporal-rigor.md:24` restates the rationale (the four wire formats) inline. Every cited site (`/api/sanitize`, `_format_timestamp`, `utcnow`) has since been fixed or removed. |
| `docs/audits/computational-fidelity-2026-04-22-addendum.md` | cut | audit addendum | Its one decision, §5 Option A "Python owns all math", is `AGENTS.md:59` rule 5 and CLAUDE.md philosophy #5. |
| `docs/audits/structural-integrity-2026-04-22.md` | cut | audit (2026-04) | A baseline of violations that has since been fixed: golden fixtures exist, `docs/references/` is populated, and `[GraphQLName]` is in use. The first pass kept it only as the "companion" of the fidelity audit above. |
| `docs/audits/open-pr-review-2026-08-05.md` | cut | PR review | Reviews PRs #1385–#1387, all merged. Each of its 26 findings is fixed in `app/broker/alpaca/clerk/sqlite/*`. The ~25 citing comments already quote the finding title and state the reason inline, e.g. `commands.py:19`, `mirror.py:133` and `writes.py:52`. Tests are named per finding in `test_corrective_foundation.py`. |
| `docs/audits/read-latency-profile-live-2026-08-31.md` | cut | measurement audit | #1801 is closed. Its conclusions (not lock contention, leave `POLL_REQUEST_TIMEOUT_MS`, reduce per-read work) are carried in `docs/known-gaps.md` (:354, :405). The bench it supports is dead (#2707 row 4). |
| `docs/audits/strategy-execution-research-directions-2026-08-24.md` | cut | research brief | The directions became issues and ADRs 0045, 0050 and 0051. The router it is cited from, `app/routers/run_replay.py:4`, is cut by #2706. Its standing owner decision (`:8-12`, "Paper evidence-only override is permanent") is the override recorded in ADR 0034 (`:65-117`); see **ADR owed** #2 for the "permanent, for ease of testing" why. |
| `docs/audits/alpaca-paper-live-workflow-2026-09-09.md` | cut | workflow audit | Describes the shadow-receipt and arming journey that ADR 0059's 2026-09-27 amendment (PRD #2540, `ADR 0059:3-4`) replaced with bot budgets. F1–F9 were fixed or made moot. Its own header says it proposes no accepted decision. |
| `docs/audits/alpaca-sqlite-clerk-paper-soak-2026-08-07.md` | cut | qualification receipt | A one-time cutover receipt; ADR 0035 holds the decision. Its one durable why, that SQLite WAL over the macOS virtiofs bind mount corrupted the DB, is stated at `compose.yaml:127-132` and enforced at `repository_lifecycle.py:153`. See **ADR owed** #1. |
| `docs/audits/alpaca-sqlite-clerk-qualification-full-2026-08-11.json` | cut | run snapshot | Receipt cited only by the soak report above. The runner writes fresh output (`run_alpaca_sqlite_qualification.py`, `--json-output`). |
| `docs/audits/alpaca-sqlite-clerk-qualification-full-2026-08-11.md` | cut | run snapshot | Same. |
| `docs/audits/alpaca-sqlite-clerk-synthetic-rehearsal-2026-08-07.json` | cut | run snapshot | Same. The rehearsal mode that produced it is cut by #2707 (row 23). |
| `docs/audits/alpaca-sqlite-s6-cutover-receipts-2026-08-11.json` | cut | cutover receipt | A one-time 2026-08-11 cutover of `PA3KWXU1C4C3`, now history. Recovery procedure lives in `docs/runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md`. |
| `docs/audits/alpaca-sqlite-s6-broker-pre-cutover-proof-2026-08-11.json` | cut | broker proof | A flat-account snapshot at that instant, cited only by the cutover receipt. |
| `docs/audits/alpaca-sqlite-s6-broker-post-campaign-proof-2026-08-11.json` | cut | broker proof | Same. |
| `docs/audits/alpaca-sqlite-sole-authority-retirement-2026-08-19.md` | cut | retirement receipt | ADR 0037 holds the decision. Its procedure section (`:35-70`) documents `scripts/qualify_alpaca_activation_inventory.py`, which #2707 (row 13) cuts as never run. |
| `docs/audits/clerk-lineage-reachability-2026-08-17.md` | cut | reachability audit | Evidence for ADR 0037. The IBKR lineage it maps was removed by #1813, and `tests/structural/test_ibkr_feed_boundary.py` now proves the boundary. |
| `docs/audits/live-operator-surface-inventory-2026-08-18.md` | cut | UI inventory | A rendered-DOM snapshot behind ADR 0041. The pages it walks (Gallery, the old Broker Desk) were replaced: ADR 0064, #2560. |
| `docs/audits/numeric-authority-census-2026-08-17.md` | cut | census | Evidence for ADR 0036, which records the single-flatness decision. The census itself counts sites in code. |
| `docs/audits/strategy-lab-regression-investigation-2026-09-27.md` | cut | repair receipt | Its "Polygon data lineage" section restates `app/data_lake/polygon_fetcher.py`, `ensure_data.py` and `derived_quote.py`. The repair ships with regression tests (its §"Regression and validation evidence"). |
| `docs/audits/plan-tournament-2026-09-12-multi-broker-multi-clerk.json` | cut | plan-tournament receipt | A hash receipt of how the fleet PRD was chosen. ADR 0062 holds the outcome. |
| `docs/audits/plan-tournament-2026-09-12-data-lab-ui-ux.json` | cut | plan-tournament receipt | Same, for the Data Lab PRD (cut below). |
| `docs/audits/alpaca-bot-control-panel-architecture-audit-2026-08-02.md` | cut | architecture audit | Its P0/P1 findings on the 2026-08 panel were fixed by the clerk custody and immutable-instance work (ADRs 0034, 0035, 0037). The panel has since been redesigned (#2540, #2560). Its only keeper was the clerk PRD (cut below). |
| `docs/audits/ibkr-control-plane-decommission-inventory-2026-08-26.md` | cut | decommission inventory | Self-labelled "point-in-time evidence — not implementation authority" (`:3-6`). #1813 is closed. The feed boundary it scoped is enforced by `tests/structural/test_ibkr_feed_boundary.py:1-11`, and the provider decision is in ADR 0062 (`:19-44`). The sacred feed is the code and test, not this doc. Both its keepers are now cut. |
| `docs/design/bouchaud-farmer-lillo-2008-microstructure-implementation-design.md` | cut | build spec (unbuilt) | No code implements it (attribution says "Canonical implementation: planned", `BFL-2008-MICRO-001/v1/attribution.md:11`), and no open issue tracks it. Port scope, guidance and first ports live in `docs/references/bouchaud-farmer-lillo-2008-market-impact.md` (`:9-63`); the paper is vendored (`references/arxiv-0809.0822v1/`). |
| `docs/design/fleet-b-route-inventory.md` | cut | route inventory | Restates the catalog `ALPACA_OPERATIONS` (`app/broker/alpaca/clerk/fleet_adapter.py:95-101`) and the routers. Its "retained deliberately" mutations are judged by #2706 (the run-replay router goes). |
| `docs/design/fleet-a2-lane-inventory.md` | cut | role inventory | Writable roots and routers per role are code (`FLEET_ROLE` mounting). Its market-data decisions are ADR 0062 §"Retained market-data provider" (`:19-44`, one read-only Gateway connection per clerk). |
| `docs/design/2026-09-13-clerk-fleet-delivery-review.md` | cut | adversarial review | Its accepted findings are ADR 0062 §"Addendum — protocol hardening accepted 2026-09-13" (`:136`). Deliveries A–G have shipped. |
| `docs/design/user-owned-broker-configurations-plan-2026-09-10.md` | cut | delegation plan (shipped) | §0 owner decisions are ADR 0060. Profiles shipped (`docs/architecture/broker-configuration-profile-contract.md`). |
| `docs/design/alpaca-desk-account-selection-ux-2026-09-11.md` | cut | UX plan (shipped, superseded) | Superseded by account-first navigation (ADR 0064) and the money-map account workspace (#2560, closed). |
| `docs/prds/2026-08-10-sqlite-sole-authority-alpaca-execution.md` | cut | implementation plan (shipped) | Slices S0–S6 shipped. Decisions are in ADRs 0035 and 0037. The 5 citing code/test sites (e.g. `economic_projection.py:18`) state their rule inline. |
| `docs/prds/2026-08-12-broker-account-desk-lens-redesign.md` | cut | PRD (shipped, superseded) | PRD #1086 is closed. The desk it designs was replaced by the money-map workspace (#2560) and ADR 0064. |
| `docs/prds/2026-09-12-multi-broker-clerk-control-plane.md` | cut | PRD (shipped) | ADR 0062 and its addenda hold the decisions, and the `docs/runbooks/fleet-*` runbooks hold rollout and rollback. "Additional brokers" is open-ended future intent with no open issue. |
| `docs/prds/alpaca-account-clerk-sqlite-control-plane.md` | cut | PRD (shipped) | §2 Decisions say "see ADR 0035" (`:71`). The cutover section (§16) is the recovery-and-cutover runbook. |
| `docs/prds/sealed-signal-program-to-governed-alpaca-bot.md` | cut | PRD (shipped) | Its own status line says ADR 0042 captured the decision and slices 0–5 shipped. Slice 6 (#1731) is closed. ADR 0043 holds the build proof. |
| `docs/prds/strategy-lab-results-experience.md` | cut | PRD (shipped) | Shipped. Its authority rule (Python authors every number) is `AGENTS.md:59`. Equity downsampling is `app/engine/results/equity_downsample.py`. |
| `docs/prds/2026-09-12-data-lab-workspace-redesign.md` | cut | PRD (shipped) | `docs/known-gaps.md:504-508` records it as implemented and lists the open items itself. No open issue. |
| `docs/spy-lean-output/source-map.md` | cut | LEAN field→C# map | `app/engine/results/lean_statistics.py` cites the LEAN C# file and lines formula by formula (34 sites, e.g. `:606`, `:614`). The pinned oracle (`docs/references/lean-native-statistics-oracle-v1.md`, LEAN commit `261366a7`, golden `lean-statistics-oracle-v1/`) supersedes this unpinned study. |
| `docs/spy-lean-output/verify.py` | cut | study script | Unrunnable: its input path is a hard-coded `/sessions/…/Lean/Launcher/bin/Debug/` (`:28-30`). Its formulas are re-derived by `lean_statistics.py` and proven by the oracle fixture. |
| `docs/spy-lean-output/inventory.json` | cut | field dump | A mechanical catalogue of one run's output keys. It is kept only by `source-map.md`. |
| `docs/superpowers/plans/2026-08-05-alpaca-clerk-corrective-foundation-slice.md` | cut | plan (shipped) | Its 26-finding scope is the open-PR review above; the code it cites (`repository.py:14`) and the tests named per finding say what shipped. The contract text lives in `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md` (#2712). |
| `docs/superpowers/plans/2026-09-08-live-slice-5-risk-envelope.md` | cut | plan (shipped) | The cash bound and loss hold are ADR 0059 D4 and its amendments (`:14`, `:20`). |
| `docs/superpowers/plans/2026-09-15-fleet-lane-visibility-and-handover.md` | cut | plan (shipped) | D4 is ADR 0063. D1/D2 ("undetermined lane mode = assume real money") are in `CONTEXT.md`. D3 shipped as `docs/runbooks/add-an-alpaca-account.md`. D5's disposable-volume facts are in that runbook and `alpaca-clerk-disposable-paper-clean-slate.md`. |
| `docs/superpowers/plans/2026-09-10-shadow-to-live-operator-journey.md` | cut | plan (superseded) | A proposed plan for the shadow-receipt journey. That journey was retired by ADR 0059's 2026-09-27 budget amendment. |
| `docs/superpowers/specs/2026-05-09-ml-prediction-as-data-v05-design.md` | cut | design spec (shipped) | Shipped as `app/research/ml/`. Rationale and runbook live in `docs/references/quantconnect-precomputed-predictions.md` and `docs/ml-predictions-authority.md` (#2713). |
| `docs/superpowers/specs/2026-05-10-quantconnect-precomputed-predictions-parity.md` | cut | design spec (shipped) | Same. `docs/references/quantconnect-precomputed-predictions.md` is the reference note and runbook. |
| `docs/superpowers/specs/2026-05-11-phase3-pnl-parity-design.md` | cut | design spec (shipped) | The taxonomy is the `DivergenceCategory` enum (`app/research/parity/qc_reconciler.py`) mirrored in `.claude/rules/numerical-rigor.md`. Capture is `docs/references/qc-aapl-phase3-capture-runbook.md`, and the result is `docs/references/reconciliations/qc-aapl-phase3.md`. |
| `docs/superpowers/specs/2026-08-14-bot-gallery-redesign-design.md` | cut | design spec (retired) | Gallery is retired; #2708 cuts the leftover `candle-renderer.ts` and wall-store that cite it. |
| `docs/superpowers/specs/2026-08-16-recency-chart-design.md` | cut | design spec (shipped) | Table ownership is ADR 0057. Statistics are `app/research/recency/stats.py`. Its §10 "Resolved" items are implemented. |
| `docs/superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md` | cut | design spec (shipped) | The feed seam is enforced by `tests/structural/test_ibkr_feed_boundary.py`, whose docstring (`:3-11`) states the boundary and its now-empty exception list. The provider rule is ADR 0062. |
| `docs/superpowers/specs/2026-09-02-feed-reconnect-continuity-design.md` | cut | design spec (shipped) | ADR 0053 is "the standing record of every decision" (`:89`). `docs/references/feed-reconnect-continuity.md` carries the measurements (`:62-111`). The deferred slices 4–5 have no open issue. |
| `docs/superpowers/specs/2026-09-09-live-slice-6-arming-ceremony-design.md` | cut | design spec (retired) | R1–R13 restate `app/broker/alpaca/clerk/live_arming.py` and the CLI. The arming ceremony itself was superseded by ADR 0059's 2026-09-27 amendment (`:3-4`). |
| `docs/superpowers/specs/2026-09-09-live-slice-7-gate-remeaning-design.md` | cut | design spec (shipped) | Rulings were folded into ADR 0059 in place ("Amended 2026-09-09 … slice 7", `:12`). |

## ADR owed

A why worth keeping now lives only in a code comment or a cut doc. Per ☆ *comments point to ADRs*, it
needs an ADR home.

1. **Clerk SQLite must live on VM-local storage** (ADR 0035 consequence). SQLite WAL over the macOS
   `virtiofs` bind mount corrupted the clerk DB in the 2026-08-07 soak. The reason exists today only in
   `compose.yaml:127-132` and the guard at `app/broker/alpaca/clerk/sqlite/repository_lifecycle.py:153`. The
   cut soak report (§"Corruption mechanism", `:312-353`) has the incident and the SQLite-doc citations.
2. **"The Paper evidence-only override is permanent, kept for ease of testing"** (owner, 2026-08-24). ADR
   0034 records the override itself. I did not confirm it records the permanence and the why. If it does
   not, add one sentence to ADR 0034 before the research brief is cut.

Not owed: the "undetermined lane mode reads as real money" rule is already in `CONTEXT.md`. The
cross-engine gate-order why goes into the fixture README (slim row above), not an ADR.

## What the cuts orphan

- **`docs/spy-lean-output-report.md`** (#2713's area): all three files it links (`:8-10`) are cut, and its own
  register row is `docs/doc-authority.md:209`.
- **Empty directories on master:** `docs/prds/`, `docs/design/` (after the fleet-D merge),
  `docs/superpowers/`, `docs/spy-lean-output/`, `docs/process/` (already emptied by #2711), and
  `docs/audits/auto-research/`. `docs/audits/` keeps one file; `docs/validation/` keeps two.
- **No fixtures, helpers or config** beyond the `compose.yaml:316` mount, which goes with #2707's service
  cut.

## Hazards the cutting PR must carry

1. **Documentation contract.** `_validate_local_links` (`scripts/check_documentation_contract.py:149`) checks
   links from canonical docs, runbooks and `docs/prds/**`. Each of these must drop its link to a cut in the
   **same commit**:
   - `docs/known-gaps.md` (:9, :354, :382, :386, :405, :428, :498, :506)
   - ADRs 0018, 0035, 0036, 0037, 0041, 0043, 0045, 0046, 0047, 0048, 0050, 0051, 0052, 0053, 0054, 0060,
     0062, 0063, 0064, in their provenance and reference lines (#2712)
   - the five `docs/runbooks/fleet-*` runbooks that name the fleet PRD as "Authority" (re-point to ADR 0062),
     plus the fleet-D matrix links in `fleet-d-two-clerk-rollout.md:3` and
     `fleet-e-compatibility-retirement.md:10`
   - 16 `docs/references/` notes (#2714)
   - `docs/architecture/{alpaca-clerk-sqlite-pinned-contracts,alpaca-configuration-ownership-inventory,broker-configuration-profile-contract,engine-authority-map}.md`
   - `docs/broker-clerk-fleet-authority.md`, `docs/ml-predictions-authority.md`, `docs/math-sources-of-truth.md`
   - `CONTEXT.md:2077`, `AGENTS.md:59` and `.claude/rules/temporal-rigor.md:24` (#2715)

   Afterwards, run `python scripts/check_documentation_contract.py` and
   `pytest PythonDataService/tests/contracts`.
2. **`docs/doc-authority.md` rows** 188–193, 195, 210, 224 and 225 (plus 209 if #2713 cuts the orphaned report) go in the same commit (#2713 owns the
   file). `check_documentation_contract.py:111-125` keeps naming emptied directories, which is harmless;
   #2716 owns it.
3. **Served copy.** `docs/runbooks/ibkr-setup-guide.md:46` cites the 08-25 stress audit, and its served
   mirror `Frontend/src/assets/docs/ibkr-setup-guide.md:61` carries the same sentence. Drop the citation in
   both; keep the weekly re-login fact, which is IBKR feed operation.
4. **Sacred attribution files cite cut docs:**
   - `tests/fixtures/golden/microstructure/BFL-2008-MICRO-001/v1/attribution.md:11` and
     `references/arxiv-0809.0822v1/attribution.md:26` cite the BFL design. Re-point both to
     `docs/references/bouchaud-farmer-lillo-2008-market-impact.md`.
   - `tests/fixtures/golden/cross-engine-studies/README.md:5` calls the matrix spec "Authoritative design".
     That README becomes the merge target.

   `tests/fixtures/golden/manifest.json` hashes only `input`/`output` (`content_sha256`, `file_sha256`), not
   `attribution.md`, so these edits are hash-safe. Still run the golden-manifest tests.
5. **Citations inside SQL text.** `app/broker/alpaca/clerk/sqlite/schema.py:191`,
   `tests/fixtures/sqlite-clerk-v8-schema.sql:110` and `docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md:403`
   carry the same open-PR-review citation inside DDL. A schema-parity test reads the pinned-contracts doc, so
   edit all three together or leave all three.
6. **Ordering.**
   - `compose.yaml:316` goes only with #2707's `alpaca-clerk-qualification` cut (and its `BUNDLED_VOLUMES`
     entry).
   - The auto-research files go only with #2715's skill cut; `.claude/skills/` is write-protected in the
     agent sandbox.
   - The fleet-D merge waits on #2713's runbook verdict.
   - The live-EMA audit waits on #2639 closing.
7. **Kept validation files.** Do not move `docs/validation/*`. Their paths sit in the strategy registry's
   provenance strings (`registry.py:444-445`), and the map notes that strategy edits are hashed into Signal
   Program build proofs.
8. **Kill lists age.** Re-run the inbound scan at the cutting SHA:
   `git grep -n -F -f <cut basenames> -- . ':(exclude)docs/audits' …` should list only the referrers named
   here.

## Comment citations these cuts drop (for #2742 / #2743)

These code, test and script sites cite a doc cut here; the comment tickets judge each as drop, re-point or
ADR owed. Counts are citing lines:

| Doc | Sites |
|---|---|
| open-pr-review | 26 (`app/broker/alpaca/clerk/sqlite/*`, `routers/alpaca_clerk_sqlite.py:11`, the two SQL sites in hazard 5, tests) |
| sealed-signal PRD | 7 |
| recency-chart spec | 7 |
| cross-engine matrix spec | 7 (re-point to the fixture README) |
| sqlite-sole-authority plan | 5 |
| 08-25 stress audit | 5, incl. `scripts/dev/fleet/_api.py:8`, which #2707 cuts |
| phase-3 spec | 4 |
| slice-0 spec | 3 (`test_ibkr_feed_boundary.py:11`) |
| gallery spec | 3, which go with #2708 |
| research brief | 2 |
| fleet-B inventory | 2 |
| BFL design | 2, both attributions (hazard 4) |
| corrective-foundation plan | 2 |
| desk-lens PRD | 1 |
| clerk PRD | 1 |
| results PRD | 1 |
| `verify.py` | 1, `lean_statistics.py:10`; re-point to `docs/references/lean-native-statistics-oracle-v1.md` |
| read-latency | 1 |
| feed-continuity spec | 1 |
| ML v0.5 spec | 1 |
| QC parity spec | 1 |

## Pointers: cuttable things outside this area

- **#2714.** `docs/references/alpaca-live-arming.md` and `alpaca-live-authority.md` document the arming
  ceremony that ADR 0059's 2026-09-27 amendment retired. `docs/references/run-replay-proof.md:3` specs a
  feature whose router #2706 cuts. Six signal notes (`spy-strategy-{a,b,c}-signal`, `sma-crossover-signal`,
  `rsi-mean-reversion-signal`, `deployment-validation-signal`) cite the sealed-signal PRD.
- **#2713.** `docs/spy-lean-output-report.md` is orphaned (see above). The `docs/runbooks/fleet-d-*` and
  `fleet-e-*` runbooks may be sediment if the D/E rollout is complete; that decides whether the fleet-D slim
  has a home. The `docs/known-gaps.md` sections listed in hazard 1.
- **#2715.** Sediment regrows: the `research`, `writing-plans` and brainstorming skills keep committing
  one-off notes into `docs/research/` and `docs/superpowers/`. The rule rewrite should say a plan or spec is
  deleted when its PR merges, with its whys moved to an ADR first.

## Not reviewed

- **The two PDFs' contents** (`SPY_EMA_Crossover_Validation_Report.pdf`, plus the previously cut
  `validation_report_SPY_15m.pdf`). The keep rests on the Pine header and the registry provenance, not on
  reading the report.
- **Whole-document reads.** For the shipped plans and PRDs over ~1,000 lines (the slices, the clerk and
  sealed-signal PRDs, the fleet PRD), I read the headings, status lines and decision sections, not every
  paragraph. A why buried outside a "Decisions" section could be missed. The cutting PR should skim each
  doc's decision and ruling sections against its ADR before deleting.
- **ADR 0034's wording** on the permanence of the Paper override (ADR owed #2).
- **The 2026-08-02 panel audit's P2-2** ("trusted-local control channel, not user authorization") is a
  security-posture statement. I did not check whether an ADR records it; it reads as still true.
