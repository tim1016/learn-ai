# Comment citations — scripts, Backend, Frontend and tests (#2743)

Part of map #2700. This is a plan only. Nothing is edited or deleted from this branch.

- **Read at:** `6a4d7d396108ef16471d8df888b9ded74d3c2892` (`origin/master`, 2026-09-30). The map was
  charted at `87b8e261`. Every `file:line` below is at `6a4d7d39`.
- **Area:** doc citations in `scripts/`, `PythonDataService/scripts/`, `Backend/`, `Frontend/src/` (non-spec),
  `PythonDataService/tests/`, `Backend.Tests/` and Frontend `*.spec.ts`.
- **Found with:**
  `git grep -nE 'docs/[A-Za-z0-9_./-]+\.md' -- <paths> ':!*.md'`, which gives 84 code lines and 94 test lines.
  Each hit was read with its comment block. A second grep looked for `ADR 00NN` forms of the 14 ADRs that
  #2712 cuts (0003, 0005, 0006, 0007, 0009, 0010, 0013, 0016, 0017, 0019, 0024, 0025, 0028, 0041).
  `Backend.Tests/` has no hits.

## How each citation was judged

The rulings applied are ☆ **comments point to ADRs** and ☆ **code is the documentation**.

- **drop**: the comment already states the fact, the reason is weak (plan phase labels, slice history,
  "per the spec"), or the cited doc no longer exists.
- **re-point → ADR NNNN**: an ADR in force already holds the decision. Each target was checked against the
  ADR's own text.
- **ADR owed (A–D)**: a strong decision that no ADR records. These are grouped under "ADR owed" below.
- **goes with dead code / test cut / gate cut**: the file or test is on another kill list, so it was not judged.
- **no change**: the comment cites an ADR in force (ADR 0049).
- **keep** (two narrow kinds, raised for the map below). (1) The comment names the file the code *serves*
  or the runbook it *implements*. That is a pointer, not a reason. (2) The cited #2714-kept note is the
  *receipt* for numbers a test pins, and no fixture attribution holds it.

Lines that are not comments are listed under "Not comments" and get no verdict: generated types, fixture
attribution text, checker data, and test-data strings.

## Rows — `scripts/` and `PythonDataService/scripts/`

| file:line | cited doc | verdict | reason |
|---|---|---|---|
| `scripts/alpaca_onboarding_gates.py:2` | `runbooks/add-an-alpaca-account.md` | keep (implements runbook) | The script *is* the runbook's gates, and CLAUDE.md names that runbook. This is an operator procedure, not a reason. |
| `scripts/check_adr_status.py:59` | ADR 0041 (cut; in the list "ADRs 0036-0038, 0040, 0041") | goes with gate cut (#2716) | #2716 cuts the ADR-status prose lint. If the gate stays, swap the example for a kept ADR. |
| `scripts/dev/fleet/_api.py:8` | `audits/bot-fleet-stress-2026-08-25.md` | goes with dead code (#2707 row 19) | The `scripts/dev/fleet/` stress kit is cut. |
| `scripts/render_fleet_topology.py:14` | `runbooks/fleet-dev-two-lane-posture.md` | drop | The comment already gives the reason: the engines disagree on `!override` merge order, `deploy.resources` and `:z` suffixes. |
| `PythonDataService/scripts/bench_panel_read_latency.py:14` | `known-gaps.md` §9 | goes with dead code (#2707 row 4) | One-off bench for closed #1801. |
| `PythonDataService/scripts/bench_panel_read_latency.py:24` | `audits/read-latency-profile-live-2026-08-31.md` | goes with dead code (#2707 row 4) | Same file. |
| `PythonDataService/scripts/fixture_generators/alpaca_regulatory_fees.py:25` | `references/alpaca-regulatory-fees.md` | drop | The generator's emitted attribution (`:147`) and its manifest citation (`:194`) already name the note. This is a comment-only edit; the emitted bytes must stay identical (hazard 1). |
| `PythonDataService/scripts/fixture_generators/strategy_abc_self_equivalence.py:8` | `references/strategy-abc-self-equivalence.md` | drop | The docstring states the receipt kind (`internal_regression`), and `:180` emits the same citation into ENG-008. |
| `PythonDataService/scripts/generate_signal_program_trace_corpus.py:3` | `prds/sealed-signal-program-to-governed-alpaca-bot.md` Slice 5 | re-point → ADR 0043 | ADR 0043 holds the golden-trace qualification per Signal Program (`0043:117-122`). "Slice 5" is PRD history. |
| `PythonDataService/scripts/lean_sidecar_pin_image.py:12` | `architecture/lean-sidecar-lab.md` §"Runner choice" | drop | Step 2 has the operator copy the digest into a doc. The digest lives in `app/lean_sidecar/config.py`, so a doc copy is the duplication ☆ rules out. Coordinate with #2741 (hazard 7). |
| `PythonDataService/scripts/manage_broker_configuration.py:22` | `references/alpaca-credential-slots.md` | goes with dead code (#2707 row 11) | One-time ADR 0060 cutover tool. |
| `PythonDataService/scripts/measure_verdict_under_live_terms.py:48` | `references/ema-verdict-under-live-terms.md` | goes with dead code (#2707 row 7) | Research instrument for closed #2466. |
| `PythonDataService/scripts/measure_verdict_under_live_terms.py:103` | `audits/live-ema-spy-missed-entry-2026-09-17.md` | goes with dead code (#2707 row 7) | Same file. |
| `PythonDataService/scripts/migrate_installation.py:41` | `runbooks/migrate-installation.md` | keep (implements runbook) | "The procedure is …": the CLI is that runbook's tool. |
| `PythonDataService/scripts/regenerate_cross_engine_study.py:17` | `superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` | drop | The docstring lists the regeneration workflow itself (steps 1–6). |
| `PythonDataService/scripts/run_signal_program_build_qualification.py:3` | `prds/sealed-signal-program-to-governed-alpaca-bot.md` S11.4 | re-point → ADR 0043 | ADR 0043 decides that Start/Resume load the qualification receipt, which is minted only after the program's suite passes (`0043:117-122`). |

## Rows — `Backend/`

| file:line | cited doc | verdict | reason |
|---|---|---|---|
| `Backend/Configuration/IvRecorderOptions.cs:6` | `architecture/iv-ownership-research.md` §7.5/§7.6/§9 | ADR owed (B) | The rationale for a .NET-owned cron and its slot schedule is a real ownership decision. No ADR mentions the IV recorder. The recorder stays: it is opt-in, not dead (☆). |
| `Backend/Models/DTOs/PolygonResponses/PortfolioScenarioResponse.cs:6` | `architecture/numerical-authority-migration-plan.md` | drop | "Mirror of the Python response shape" is already stated. "Phase 2.1/2.2" is plan history. |
| `Backend/Models/MarketData/DataLakeArtifact.cs:10` | ADR 0049 §3.1 | no change | ADR in force. |
| `Backend/Services/Implementation/PortfolioRiskService.cs:280` | `numerical-authority-migration-plan.md` Phase 2.2 | ADR owed (A) | "Scenario math is now Python-canonical" is the math-authority decision, and no ADR holds it. |
| `Backend/Services/Implementation/PositionEngine.cs:26` | `math-sources-of-truth.md` § Portfolio, finding F-0010 | ADR owed (A) | This is the one named exception to "Python owns math": FIFO lots stay in .NET because the data lives in EF. The reason is strong, and only a registry row records it. |
| `Backend/Services/Interfaces/IPolygonService.cs:22` | `numerical-authority-migration-plan.md` Phase 1.1 | drop | The comment says the defaults preserve the old shape. The phase label is history. |
| `Backend/Services/Interfaces/IPolygonService.cs:222` | `numerical-authority-migration-plan.md` | drop | The comment already says the call goes to Python's `/api/portfolio/scenario`. |

## Rows — `Frontend/src/` (non-spec)

| file:line | cited doc | verdict | reason |
|---|---|---|---|
| `app/app.routes.ts:275` | `architecture-manual.md` | keep (names served file) | It names the canonical file the route serves. `check_documentation_contract.py:58-64` pins the pair. |
| `app/app.routes.ts:280` | `indicator-reliability-methodology.md` | keep (names served file) | Same. |
| `app/app.routes.ts:285` | `signal-engine-authority.md` | keep (names served file) | Same. |
| `app/app.routes.ts:290` | `runbooks/ibkr-setup-guide.md` | goes with dead code (#2709 B6) | The `/docs/ibkr-setup-guide` route has no link anywhere, so it is dead under ☆. This conflicts with #2713 (see "For the map"). |
| `app/components/broker/v2-panel/gallery/lib/candle-renderer.ts:7` | `superpowers/specs/2026-08-14-bot-gallery-redesign-design.md` | drop | "A future `candle-sparkline`" is a speculative-reuse reason, which is weak. The file survives #2708, which cuts only its interactive-tile paths. |
| `app/components/brokers/alpaca-desk/configuration/broker-configuration-refusal.ts:2` | `architecture/broker-configuration-profile-contract.md` §6 | drop | The comment states the whole refusal shape (`reason`, backend `message`, optional `next_step`). |
| `app/components/data-lab/past-chain-inspector/past-chain-inspector.component.scss:2` | `architecture/options-ux-design-prompt.md` (already pruned) | drop | The target is deleted. |
| `app/components/data-lab/past-chain-inspector/past-chain-inspector.component.ts:3` | `architecture/options-research.md` §5.3 | drop | "R1 of the options-routes cleanup" is history. |
| `app/components/data-lab/past-chain-inspector/past-chain-inspector.component.ts:5` | `options-ux-design-prompt.md` (pruned) | drop | The target is deleted. |
| `app/components/research-lab/indicator-reliability/doc-refs.ts:9` | `indicator-reliability-methodology.md` | keep (names served file) | The anchors are read from that doc's generated heading IDs. That is a coupling the code depends on, not a reason. |
| `app/components/research-lab/signal-report/signal-report.component.ts:183` | `signal-engine-authority.md` §6 | drop | The comment states the model: a 1-bar lag with close-to-close measurement. |
| `app/components/research-lab/strategy-runs/strategy-runs.component.ts:24` | `references/run-ledger.md` ("deferral rationale") | re-point → ADR 0031 | ADR 0031 sanctions Angular → FastAPI with generated types for Python-owned payloads (`0031:17`). |
| `app/components/strategy-builder/strategy-builder.component.scss:698` | `options-ux-design-prompt.md` (pruned) | drop | The target is deleted. |
| `app/components/strategy-builder/strategy-builder.component.ts:65` | `options-ux-design-prompt.md` (pruned) | drop | The target is deleted, and the comment states the two modes. |
| `app/graphql/types.ts:314` | `numerical-authority-migration-plan.md` | drop | The comment states the opt-in behaviour. "Phase 1.1" is history. |
| `app/models/data-policy.ts:10` | `superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md` (deleted) | drop | The target is deleted, and the comment states the contract. |
| `app/services/lean-sidecar.service.ts:70` | `handoffs/2026-05-18-design-p2-5-date-semantics-v2.md` (deleted) | re-point → ADR 0022 | It covers the trading date anchored at the 09:30 ET session open, with the calendar kept server-side. `app/lean_sidecar/trading_calendar.py` holds the repo's only `mcal.get_calendar`. |
| `app/services/past-chain.service.ts:6` | `architecture/options-research.md` §5.3 | drop | R1 cleanup history. |
| `app/services/versioned-snapshot-stream.ts:25` | ADR-0028 (Retired, cut) | ADR owed (D) | Live code, imported by `bot-panel-live-store.service.ts`. It cites a "never adopted" ADR for a rule it still runs (see D). |
| `app/shared/errors/error-catalog.ts:14` | `math-sources-of-truth.md` ("resolver guidance") | drop | The doc has no `mathRef` guidance (no hit), and the comment states the rule. |
| `app/styles/_tokens.scss:209` | `architecture/design-handoff-engine-lab-2026-04-26.md` (deleted) | drop | The target is deleted. |
| `app/styles/_tokens.scss:267` | `architecture/design-handoff-edge-2026-04-25.md` (deleted) | drop | The target is deleted. |
| `app/utils/black-scholes.ts:7` | `numerical-authority-migration-plan.md` Phase 1.3 | ADR owed (A) | "This module is **not a math authority**; canonical lives in Python" is the math-authority decision. Today it lives only in the plan, CLAUDE.md #5 and AGENTS.md. |
| `app/utils/occ-ticker.ts:7` | `architecture/options-research.md` (R5) | drop | The comment states the rule: every caller round-trips through this module. |

## Rows — test suites

| file:line | cited doc | verdict | reason |
|---|---|---|---|
| `Frontend/src/app/utils/black-scholes.parity.spec.ts:3` | `architecture/iv-ownership-research.md` §6 | drop | The spec states its own tolerance (`atol=1e-4`) and why (`:14`). |
| `tests/broker/alpaca/clerk/sqlite/test_corrective_foundation.py:4` | `superpowers/plans/2026-08-05-alpaca-clerk-corrective-foundation-slice.md` | drop | A "required-test matrix" of a shipped slice is history. |
| `tests/broker/alpaca/clerk/sqlite/test_enter.py:978` | `references/clerk-invariants.md` §2 | drop | The docstring states the delta-pricing rule and the arithmetic. |
| `tests/broker/alpaca/clerk/sqlite/test_exit_send_session.py:15` | `references/codex-review-2419.md` (does not exist) | drop | Dead link. The docstring already states finding R1. |
| `tests/broker/alpaca/clerk/sqlite/test_repository.py:838` | `references/clerk-invariants.md` §3 | drop | `abs=1e-9` is `POSITION_QTY_EPSILON` and the strict-float default (§3 says it re-states existing policy). |
| `tests/broker/alpaca/clerk/sqlite/test_runtime.py:173` | `audits/bot-fleet-stress-2026-08-25.md` | drop | The docstring itself says this is "cited history … not a threshold". |
| `tests/broker/alpaca/clerk/sqlite/test_two_bots_one_symbol.py:5` | `references/two-bots-one-symbol-2469.md` | drop | Research findings, not a decision; the tests pin current behaviour. This is the note's last live link (hazard 3). |
| `tests/broker/alpaca/test_capabilities.py:83` | `references/alpaca-extended-hours.md` | drop | The comment already names the primary source (Alpaca "Orders at Alpaca" § Extended Hours). |
| `tests/broker/v2panel/conftest.py:237` | `references/deployment-validation-consecutive-green.md` | drop | The comment states #1672 and its hash consequence. |
| `tests/broker/v2panel/test_panel_projection.py:2120` | `references/broker-v2-readiness-summary.md` | drop | The docstring states the rule: counted once, exactly, and rendered verbatim. This is the note's last live link (hazard 3). |
| `tests/broker/v2panel/test_sqlite_action_fence.py:4` | `audits/bot-fleet-stress-2026-08-25.md` S16 | drop | The docstring explains the bug in full. |
| `tests/broker/v2panel/test_vocabulary_snapshot.py:171` | ADR 0041 Decision 6 (Retired, cut) | drop | Reword so the Literal ↔ collection rule stands on its own (#2712 hazard 6). The test and its CI job enforce the rule. |
| `tests/broker/v2panel/test_vocabulary_snapshot.py:202` | ADR 0041 Decision 6 | drop | Same. |
| `tests/broker_configuration/test_legacy_environment.py:8` | `architecture/alpaca-configuration-ownership-inventory.md` §F | re-point → ADR 0060 | ADR 0060 owns credential-slot references and the refuse-retired-settings ruling (`0060:46,157`). |
| `tests/contracts/test_unscoped_bot_mutation_retirement.py:8` | `design/fleet-b-route-inventory.md` | drop | The docstring names the two retained routes and why they are retained. |
| `tests/engine/live/test_deployment_validation_deploy_artifacts.py:46` | `references/deployment-validation-consecutive-green.md` | re-point → ADR 0022 | It covers calendar-derived session boundaries so that half-days keep a real barrier. That is ADR 0022's no-hardcoded-session-times rule. |
| `tests/engine/live/test_intent_ledger.py:158` | ADR 0009 (Superseded, cut) | goes with dead code (#2704) | The intent ledger is cut. |
| `tests/engine/live/test_reconcile.py:4` | `superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md` (deleted) | goes with dead code (#2704) | The engine reconcile pipeline is cut. This is not the clerk's `test_reconcile.py`. |
| `tests/engine/test_sizing.py:5` | `references/lean-set-holdings.md` | drop | The golden fixture's attribution names the pinned LEAN run. |
| `tests/fixtures/test_strategy_metric_help_golden.py:34` | `references/strategy-metric-help.md` | keep (math receipt) | This is the `Reference:` line of a provenance block (learn-ai-validation). The note holds the v2 derivation, and nothing else cites it. |
| `tests/integration/data_lake/test_ensure_data_all_kinds.py:22` | ADR 0049 §4.5/4.6 | no change | ADR in force. |
| `tests/integration/data_lake/test_gate_chain_convergence.py:17` | `superpowers/specs/2026-08-29-data-lake-issue-closure-plan.md` (deleted) | re-point → ADR 0049 | Concurrent `ensure_data` calls converge on one fetch through the catalog state machine (`0049:90`). |
| `tests/integration/reconciliation/test_rsi_mean_reversion_lean_golden.py:4` | `references/reconciliations/rsi-mean-reversion-lean-2026-09-01.md` | drop | The ENG-009 manifest citation (`manifest.json:1541`) and its hashed attribution already name the report. |
| `tests/integration/reconciliation/test_spy_vwap_reversion_qc.py:53` | `references/reconciliations/spy-vwap-reversion.md` | goes with dead code (#2704, conditional) | It goes only if #2706 cuts the VWAP routes. The comment already explains the accepted $0.30 floor inline. |
| `tests/lean_sidecar/test_data_folder_fidelity.py:3` | `architecture/lean-sidecar-lab.md` "non-negotiable #9" | drop | The docstring states the round-trip property it proves. |
| `tests/lean_sidecar/test_determinism_gate.py:76` | `architecture/lean-sidecar-mission-critical.md` §D2 | goes with dead code (#2706 item 8) | It proves determinism only through dead routes. |
| `tests/lean_sidecar/test_ema_crossover_template.py:148` | `superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` | drop | The docstring states the header and names the sync target (`observations_parity.py`). |
| `tests/lean_sidecar/test_router_lean_sidecar.py:184` | `handoffs/2026-05-18-design-p2-5-date-semantics-v2.md` (deleted) | re-point → ADR 0022 | Exclusive `end_ms_utc` is the next trading day's 09:30 ET open, which is the trading-date anchor. |
| `tests/lean_sidecar/test_router_lean_sidecar.py:284` | same | re-point → ADR 0022 | The class tests the live `calendar/next-trading-day-open` route. It is not among #2706's 51 cut tests. |
| `tests/lean_sidecar/test_runner_e2e.py:3` | `architecture/lean-sidecar-lab.md` §"Phase sequencing" | drop | "Phase 1 (g)" is plan history. |
| `tests/lean_sidecar/test_security_flags.py:3` | `architecture/lean-sidecar-lab.md` §"Container execution boundary" | ADR owed (C) | The docstring says "the results land in the ADR", but no LEAN-sidecar ADR exists. |
| `tests/lean_sidecar/test_trading_calendar.py:3` | `handoffs/2026-05-18-design-p2-5-date-semantics-v2.md` (deleted) | re-point → ADR 0022 | One calendar source of truth is ADR 0022's calendar authority. |
| `tests/research/artifact/test_baselines_byte_equivalence.py:3` | `architecture/research-artifact-seam.md` § "Per-PR acceptance bar" | drop | A strangler-PR acceptance bar is history, and the docstring states the bar. |
| `tests/research/artifact/test_monte_carlo_byte_equivalence.py:3` | same | drop | Same. |
| `tests/research/artifact/test_runs_byte_equivalence.py:3` | same | drop | Same. |
| `tests/research/artifact/test_walk_forward_byte_equivalence.py:3` | same | drop | Same. |
| `tests/research/divergence/test_dividend_adjustment.py:10` | `tv-polygon-validation-gotchas.md` §1 | drop | Inline the one outside fact ("Polygon `adjusted=true` adjusts for splits only"). The OpenAPI field descriptions already say it. |
| `tests/research/parity/test_cross_engine_study.py:16` | `superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md` | drop | A bare "Reference:" adds nothing. The docstring explains the skip state. |
| `tests/research/parity/test_qc_aapl_phase3_trade_parity.py:19` | `ml-predictions-authority.md` §3 | drop | The docstring states what is reconciled and how. |
| `tests/research/parity/test_qc_aapl_phase3_trade_parity.py:20` | `references/reconciliations/qc-aapl-phase3.md` | drop | `app/research/parity/qc_reconciler.py` keeps the report's live link. Both tolerances are explained at `:227-235`. |
| `tests/research/parity/test_qc_aapl_phase3_trade_parity.py:238` | same | drop | Same; the reasons sit directly above. |
| `tests/research/parity/test_qc_fixture_smoke.py:8` | `superpowers/specs/2026-05-11-phase3-pnl-parity-design.md` §2.1.2 | drop | The docstring states what both tests check. |
| `tests/research/recency/test_stats.py:7` | `math-sources-of-truth.md` | drop | "Every formula is defined here", which is the module itself. |
| `tests/routers/test_edge_recorder_fallback.py:5` | `architecture/iv-ownership-research.md` §4.7/§8.1.1 | ADR owed (B) | The recorder fallback and the imputed health prior are a signal-attenuation policy (see `:321-324`). `realized-vs-iv/series` is live (#2706). |
| `tests/routers/test_edge_recorder_fallback.py:321` | `architecture/iv-research-chat-notes.md` (pruned) | drop | The target is deleted, and the docstring gives the why. |
| `tests/routers/test_strategy_validation.py:105` | `references/deployment-validation-consecutive-green.md` | drop | The comment states #1672 and why the flag-write refuses. |
| `tests/services/test_bar_timestamp_rigor.py:4` | `audits/bar-timestamp-rigor-2026-06-12.md` | re-point → ADR 0022 | The docstring lists the int64-ms-UTC invariants, which are ADR 0022's. This is the audit's only inbound link (hazard 3; #2711 "unlock"). |
| `tests/services/test_bs_cross_engine_parity.py:14` | `numerical-authority-migration-plan.md` Phase 1.4 | ADR owed (A) | The parity test exists because a non-canonical copy exists. The why is the math-authority decision. |
| `tests/services/test_bs_greeks.py:39` | `references/reconciliations/data-lab-spy-2026-04-17-to-2026-04-24.md` Finding 3.1 | keep (math receipt) | The pinned values come from that report's manual spot-check, with no golden fixture. #2714 keeps it as Data Lab's only acceptance record. |
| `tests/services/test_candidate_uncaptured_at_crash.py:2` | `prds/sealed-signal-program-to-governed-alpaca-bot.md` §13.3/13.4 | drop | The docstring states FR-016's behaviour, and #1728 stays as the trail. |
| `tests/services/test_data_lab_chart_indicator_warmup.py:710` | `references/data-lab-indicator-warmup.md` | keep (math receipt) | It holds the per-indicator measurements behind an accepted tolerance (#2611), which a test docstring cannot hold. |
| `tests/services/test_portfolio_scenario.py:21` | `numerical-authority-migration-plan.md` Phase 2.1 | drop | A phase label. The docstring names the math tests. |
| `tests/services/test_strategy_validation_manifest.py:774` | `references/deployment-validation-consecutive-green.md` | drop | The comment states #1672 and why the hash is stale. |
| `tests/services/test_surface_hub.py:1` | ADR-0028 (Retired, cut) | ADR owed (D) | The hub is live (`routers/broker_v2_panel.py:119`, `live_projection.py:11`). |
| `tests/structural/test_ibkr_feed_boundary.py:11` | `superpowers/specs/2026-08-26-ibkr-decommission-slice-0-design.md` | re-point → ADR 0062 § Retained market-data provider | The test confines IBKR to the read-only feed, and ADR 0062 records that decision. Edit the docstring only; this is sacred (hazard 4). |
| `tests/test_strategy_engine_phase1_1.py:3` | `numerical-authority-migration-plan.md` Phase 1.1 | drop | The docstring states the move (opt-in fields, so the TS copy can stop). |
| `tests/unit/data_lake/test_atomic.py:3` | ADR 0049 §5.2 | no change | ADR in force. |
| `tests/unit/data_lake/test_lean_metadata.py:12` | ADR 0049 §4.5 | no change | ADR in force. |
| `tests/unit/data_lake/test_no_lean_paths_outside_policy.py:7` | ADR 0049 §5.3 | no change | ADR in force. |
| `tests/unit/data_lake/test_path_policy.py:3` | ADR 0049 §5.3 | no change | ADR in force. |
| `tests/unit/data_lake/test_types.py:3` | ADR 0049 §4.1/§4.2 | no change | ADR in force. |
| `tests/unit/lean_sidecar/test_bars_spec.py:6` | `superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md` (deleted) | goes with test cut (#2729 row 21) | The whole file goes. |
| `tests/volatility/test_solver.py:260` | `references/reconciliations/data-lab-spy-…md` Finding 3.1 | drop | The docstring gives the why: a coarser floor kills 0DTE recovery. `app/volatility/solver.py` keeps the report link (#2742). The test stays (◇). |
| `tests/volatility/test_vix_replication.py:595` | `architecture/iv-research-chat-notes.md` (pruned) | drop | The target is deleted, and the docstring states the gate. The test stays (◇). |
| `scripts/test_check_adr_status.py:58` | ADR 0041 | goes with gate cut (#2716) | It tests the ADR-status lint that #2716 cuts. |

`tests/` paths above are under `PythonDataService/`.

## Not comments (no verdict; owner named)

- **Generated OpenAPI types.**
  - Lines: `Frontend/src/app/api/broker.types.ts:13767, :13938, :20575, :21009, :21045`.
  - Sources: Pydantic descriptions and docstrings in `app/models/requests.py:264,300,350,507` and `app/schemas/signal_program_seal.py:228`.
  - These follow #2742's verdicts through export and codegen (hazard 2).
- **Fixture attribution and manifest text.** These are sacred math paperwork; never edit (hazard 1).
  - Generator string literals: `alpaca_regulatory_fees.py:147,194`, `realized_equity_staircase.py:141` and `strategy_abc_self_equivalence.py:180`.
  - Generator string literals in `scripts/fixture_generators/`: `ibkr_iv.py:136` and `volatility.py:1152` (◇ RV-003).
  - `tests/fixtures/golden/manifest.json:1453,1497,1541,1585,1719`.
  - The `_comment` key in `tests/research/ml/fixtures/qc_known_hashes.json:2`.
- **Checker data.** These belong to #2716 and the docs-cut hazards already on the map.
  - `scripts/check_documentation_contract.py:29-63` (the class map, the retired list, the served-copy map).
  - `scripts/check_documentation_contract.py:209,211`.
- **Code values.**
  - `src` paths in `app.routes.ts:278,283,288,294` and `markdown-drawer.model.ts:31`.
  - `test_strategy_validation_admission.py:46` (a field value).
- **Spec test data.**
  - `alpaca-deploy-workflow.component.spec.ts:69` (a canned string; #2714 cuts the note it names, and that is harmless).
  - `markdown-doc-page.component.spec.ts:15-43`.
  - `markdown-viewer.component.spec.ts:8`.
  - `error-catalog.spec.ts:48-91`.
  - `page-error.component.spec.ts:25,33` (dead with #2709 A21).
- **Doc pinning.** `PythonDataService/tests/contracts/test_documentation_contract.py` reads real docs at `:107` and `:173` and writes temporary docs. It belongs to #2730 and #2716.
- **UI copy.** `signal-report.component.html:720` renders `docs/signal-engine-authority.md` to users. If #2741 renames or cuts that doc, this text changes.

## ADR owed (proposed; each groups the rows above)

- **(A) Math authority: Python owns canonical math.**
  - Copies in .NET or Angular are labelled non-authoritative and carry a parity test against the Python canonical.
  - The .NET FIFO position engine is the one named exception, because its data lives in EF.
  - Today the decision lives only in CLAUDE.md #5, AGENTS.md "Python owns all math", `numerical-authority-migration-plan.md` and the F-0010 row in `math-sources-of-truth.md`.
  - Rows: `PortfolioRiskService.cs:280`, `PositionEngine.cs:26`, `black-scholes.ts:7`, `test_bs_cross_engine_parity.py:14`.
  - #2742 will meet the same plan in `app/models/portfolio.py`, `app/models/strategy.py`, `app/routers/portfolio.py` and `app/services/portfolio_scenario.py`.
- **(B) IV ownership and recording.**
  - Python computes IV.
  - The opt-in .NET recorder cron owns the capture schedule (its slots).
  - Edge reads fall back to recorded IV under an imputed health prior.
  - Rows: `IvRecorderOptions.cs:6`, `test_edge_recorder_fallback.py:5`.
  - App side (#2742): `services/iv_recorder.py`, `routers/edge.py`, `engine/edge/confidence.py`, `broker/ibkr/models.py`.
- **(C) LEAN sidecar execution boundary.**
  - The sidecar runs a pinned image digest committed in `config.py`.
  - It runs in a hardened container that keeps only the security flags that survive, and runs trusted algorithms only.
  - Row: `test_security_flags.py:3`.
  - App side (#2742): about 15 live files cite `lean-sidecar-lab.md`.
- **(D) Live panel and fleet snapshots are producer-owned, versioned and latest-wins.**
  - A new `stream_epoch` replaces the snapshot; same-epoch versions advance.
  - ADR 0028 is `Retired` ("never adopted"), yet this part of it runs today in `app/services/surface_hub.py` and `versioned-snapshot-stream.ts`. This is the same status-versus-body mismatch as ADR 0041.
  - Rows: `versioned-snapshot-stream.ts:25`, `test_surface_hub.py:1`.

## Hazards the cutting PR must carry

1. **Sealed fixture text.**
   - Never edit generator string literals, `manifest.json` rows, attribution files or `qc_known_hashes.json`.
   - The two comment-only generator edits (`alpaca_regulatory_fees.py:25`, `strategy_abc_self_equivalence.py:8`) must leave emitted bytes identical. Regenerate and diff to prove it.
2. **Generated types.**
   - `broker.types.ts` is never hand-edited. It changes only after #2742 edits the Python descriptions, through `export_openapi_contract.py` plus `npm run codegen:openapi`.
   - That changes the OpenAPI snapshot, so the PR merges serially.
3. **Last live links.** After these drops and re-points, these docs have no live inbound link:
   - `docs/audits/bar-timestamp-rigor-2026-06-12.md`: none at all.
   - `docs/references/two-bots-one-symbol-2469.md`: only cut ADR 0009.
   - `docs/references/broker-v2-readiness-summary.md`: only a `math-sources-of-truth.md` row. #2714 kept it on the money path without diffing it.
   - `docs/architecture/options-research.md`: only `known-gaps.md`.
   - `docs/superpowers/specs/2026-08-14-bot-gallery-redesign-design.md`: none.
   - `docs/architecture/lean-sidecar-mission-critical.md`: its one test is dead.

   Under ☆ that is allowed. #2740 and #2741 should judge each one on its content in the same pass, so the docs
   checker never sees a kept doc linking a cut one.
4. **IBKR feed boundary (sacred).**
   - In `test_ibkr_feed_boundary.py`, change only the docstring and touch no assertion.
   - The slice-0 spec stays linked from `app/broker/ibkr/bar_models.py` and `config.py`, so this re-point does not orphan it or the decommission inventory behind it.
5. **ADR 0041.** The `test_vocabulary_snapshot.py` rewording rides with the ADR's deletion (#2712 hazard 6).
6. **ADR 0028.** Land ADR (D), or reword `versioned-snapshot-stream.ts:25` and `test_surface_hub.py:1` in the same PR. Otherwise the live stream contract loses its only written why.
7. **`lean_sidecar_pin_image.py` step 2.**
   - Removing "update `lean-sidecar-lab.md` with the same digest" needs #2741 to drop the digest copy from that doc in the same pass.
   - No check compares the two, so the doc would drift silently.
8. **Signal Program build proofs.**
   - No row here edits a strategy or indicator module.
   - Before editing the docstrings of `generate_signal_program_trace_corpus.py` or `run_signal_program_build_qualification.py`, confirm that ADR 0043's decision closure does not hash those scripts.
9. **Kill lists age.** Re-run both greps at the cutting SHA. Comment-only edits need only lint, plus the touched suites to collect.

## For the map

- **Two narrow "keep" kinds that the three verdicts do not cover.**
  - **Pointer to the served or implemented file** (6 rows): `app.routes.ts:275/280/285`, `doc-refs.ts:9`, `alpaca_onboarding_gates.py:2`, `migrate_installation.py:41`.
  - **Math receipt** (3 rows): `test_bs_greeks.py:39`, `test_data_lab_chart_indicator_warmup.py:710`, `test_strategy_metric_help_golden.py:34`.
  - Neither is a reason that wants an ADR. Suggestion for the rule rewrite (#2739): a comment may cite an ADR, a runbook the code implements, the canonical file of a served copy, or the kept math receipt behind a number with no fixture.
- **Conflict on the IBKR setup guide.**
  - #2709 B6 calls the `/docs/ibkr-setup-guide` route dead, which ☆ "pages with a route but no link" supports.
  - #2713 calls the served copy (`Frontend/src/assets/docs/ibkr-setup-guide.md`) sacred.
  - `app.routes.ts:290` and `test_documentation_contract.py:173` follow whichever wins.
- **ADR 0028 has the same status/content mismatch as 0041.** It is "Retired, never adopted", but its latest-wins snapshot contract runs. ADR (D) resolves it.
- **The canonical calendar is under `lean_sidecar/`.** `app/lean_sidecar/trading_calendar.py` holds the repo's only `mcal.get_calendar`. The ADR 0022 re-points rely on that, and the location is surprising for the canonical calendar module.

## Not reviewed

- **Citations not shaped `docs/…md`:**
  - `AGENTS.md` (e.g. `black-scholes.ts:8`).
  - `CONTEXT.md`.
  - `.claude/rules/*.md` named bare (e.g. "numerical-rigor.md" in `test_spy_vwap_reversion_qc.py:55`).
  - `PythonDataService/CLAUDE.md` and `Backend/CLAUDE.md`.
- **ADR citations to ADRs in force** were not checked for accuracy, only for pointing at a cut ADR. Re-point targets were checked at line level (0022, 0031, 0043, 0049, 0060, 0062), not clause by clause.
- **Files outside the stated area:**
  - `Frontend/scripts/` (e.g. `generate-bs-parity-fixture.py:4` cites `iv-ownership-research.md` §6).
  - `Frontend/e2e/`.
  - `.github/` (e.g. `ci.yml:88`).
  - `deploy/`, `contracts/`, `references/`.
- **Whether `test_cross_engine_study.py` ever runs a cell.** Its docstring says it skips until fixtures are pinned. That is a test-ticket question (#2726), not a citation question.
